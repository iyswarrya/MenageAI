# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Twilio WhatsApp identity resolution and env provisioning (SEC-01 Phase C)."""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass

from app import db
from app.auth.identity import AuthenticatedUser

logger = logging.getLogger(__name__)

TWILIO_CHANNEL = "twilio"
DEFAULT_TWILIO_HOUSEHOLD_ID = "twilio_household"
_E164_DIGITS = re.compile(r"^\+?[0-9]{8,15}$")


@dataclass(frozen=True)
class TwilioSenderContext:
    """Resolved, authorized WhatsApp sender."""

    user: AuthenticatedUser
    household_id: str
    whatsapp_e164: str


def normalize_whatsapp_address(raw: str) -> str | None:
    """Normalize Twilio From/To-style addresses to `whatsapp:+E164`.

    Accepts:
    - whatsapp:+15551234567
    - whatsapp:15551234567
    - +15551234567
    - 15551234567
    """
    if not raw or not str(raw).strip():
        return None
    value = str(raw).strip()
    if value.lower().startswith("whatsapp:"):
        value = value.split(":", 1)[1].strip()
    digits = value.replace(" ", "").replace("-", "")
    if not digits:
        return None
    if not digits.startswith("+"):
        # Twilio often omits +; require bare digits then prefix +.
        if not digits.isdigit() or not (8 <= len(digits) <= 15):
            return None
        digits = f"+{digits}"
    elif not _E164_DIGITS.match(digits):
        return None
    return f"whatsapp:{digits}"


def parse_family_whatsapp_numbers(raw: str | None = None) -> list[str]:
    """Parse FAMILY_WHATSAPP_NUMBERS into normalized unique addresses."""
    source = raw if raw is not None else os.environ.get("FAMILY_WHATSAPP_NUMBERS", "")
    result: list[str] = []
    seen: set[str] = set()
    for part in source.split(","):
        normalized = normalize_whatsapp_address(part)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def provision_twilio_household_from_env(
    *,
    household_id: str = DEFAULT_TWILIO_HOUSEHOLD_ID,
    household_name: str = "WhatsApp Family",
) -> int:
    """Idempotently create users/memberships from FAMILY_WHATSAPP_NUMBERS.

    Env is provisioning-only. Runtime authorization and broadcast use DB membership.
    Returns the number of newly created user rows.

    Never calls restore_household_member: revoked memberships stay revoked across
    restarts/redeploys. Reactivation requires an explicit authorized admin action.
    """
    numbers = parse_family_whatsapp_numbers()
    if not numbers:
        return 0

    with db.get_connection() as conn:
        hh = conn.execute(
            "SELECT 1 FROM households WHERE household_id = ?",
            (household_id,),
        ).fetchone()
    if hh is None:
        db.create_household(household_id, name=household_name)

    created = 0
    for address in numbers:
        existing = db.get_user_by_whatsapp(address)
        if existing is None:
            user_id = f"wa_{uuid.uuid4().hex[:12]}"
            db.create_user(
                user_id,
                display_name=address,
                whatsapp_e164=address,
            )
            created += 1
            existing = db.get_user(user_id)
        assert existing is not None
        membership = db.get_household_membership(household_id, existing["user_id"])
        if membership is None:
            # Never-provisioned for this household — bootstrap membership.
            db.add_household_member(household_id, existing["user_id"])
        elif membership.get("revoked_at") is not None:
            # Intentionally revoked — never call restore_household_member here.
            logger.info(
                "Skipping env re-provision for revoked member user_id=%s household=%s",
                existing["user_id"],
                household_id,
            )
        # else: already an active member — idempotent no-op

    if created:
        logger.info(
            "Provisioned %s WhatsApp user(s) into household %s",
            created,
            household_id,
        )
    return created


def resolve_twilio_sender(from_address: str) -> TwilioSenderContext | None:
    """Resolve a WhatsApp sender to a registered household member.

    Returns None for unknown/unauthorized senders (caller should silently reject).
    If the user has multiple households, uses the earliest membership (Phase C
    assumes one household per WhatsApp user).
    """
    normalized = normalize_whatsapp_address(from_address)
    if not normalized:
        return None

    user_row = db.get_user_by_whatsapp(normalized)
    if user_row is None:
        return None

    memberships = db.list_user_households(user_row["user_id"])
    if not memberships:
        return None

    household_id = memberships[0]
    user = AuthenticatedUser(
        user_id=user_row["user_id"],
        auth_subject=user_row.get("auth_subject"),
        auth_method="twilio_whatsapp",
    )
    return TwilioSenderContext(
        user=user,
        household_id=household_id,
        whatsapp_e164=normalized,
    )


def list_broadcast_whatsapp_recipients(
    household_id: str, *, exclude_whatsapp: str | None = None
) -> list[str]:
    """Return household members' WhatsApp addresses for receipt broadcast."""
    exclude = normalize_whatsapp_address(exclude_whatsapp) if exclude_whatsapp else None
    recipients = db.list_household_whatsapp_addresses(household_id)
    if exclude:
        return [r for r in recipients if r != exclude]
    return recipients


def get_or_create_twilio_session_binding(user_id: str, household_id: str) -> dict:
    """Return an existing Twilio channel binding or create a new server-owned one."""
    existing = db.get_conversation_session_for_channel(
        user_id=user_id, household_id=household_id, channel=TWILIO_CHANNEL
    )
    if existing:
        # Touch last_seen via idempotent create
        return db.create_conversation_session(
            existing["session_id"],
            user_id,
            household_id,
            channel=TWILIO_CHANNEL,
        )
    session_id = str(uuid.uuid4())
    return db.create_conversation_session(
        session_id, user_id, household_id, channel=TWILIO_CHANNEL
    )
