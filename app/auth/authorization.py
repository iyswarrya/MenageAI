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

"""Central household and session authorization (SEC-01)."""

from __future__ import annotations

from typing import Any

from app import db
from app.auth.identity import AuthenticatedUser

# Session state key written only by authenticated API/Twilio entrypoints.
AUTHORIZED_HOUSEHOLD_STATE_KEY = "authorized_household_id"


class AuthorizationError(Exception):
    """Raised when an authenticated user lacks access to a resource."""


def require_household_access(user: AuthenticatedUser, household_id: str) -> None:
    """Ensure the authenticated user is a member of household_id.

    Raises:
        AuthorizationError: If the user is not a household member.
    """
    if not household_id or not household_id.strip():
        raise AuthorizationError("household_id is required")

    if not db.user_belongs_to_household(user.user_id, household_id):
        raise AuthorizationError("User is not authorized to access this household")


def resolve_authorized_household(
    user: AuthenticatedUser, requested_household_id: str | None
) -> str:
    """Resolve the household for a request using membership rules.

    - 0 memberships: deny
    - 1 membership: use it when household_id omitted; else membership-check explicit id
    - 2+ memberships: require explicit household_id and membership-check it
    """
    memberships = db.list_user_households(user.user_id)
    requested = (requested_household_id or "").strip() or None

    if not memberships:
        raise AuthorizationError("User has no household memberships")

    if requested:
        require_household_access(user, requested)
        return requested

    if len(memberships) == 1:
        return memberships[0]

    raise AuthorizationError(
        "household_id is required when the user belongs to multiple households"
    )


def require_session_owner(user: AuthenticatedUser, session_id: str) -> dict[str, Any]:
    """Ensure session_id exists and is owned by the authenticated user.

    Also verifies household membership for the session's household_id so a
    revoked member cannot resume a previously bound session.

    Returns:
        The conversation_sessions binding row.

    Raises:
        AuthorizationError: If the session is missing, owned by another user,
            or the user is no longer a household member.
    """
    if not session_id or not session_id.strip():
        raise AuthorizationError("session_id is required")

    binding = db.get_conversation_session(session_id)
    if binding is None:
        raise AuthorizationError("Conversation session not found")

    if binding["user_id"] != user.user_id:
        raise AuthorizationError("Conversation session is not owned by this user")

    require_household_access(user, binding["household_id"])
    return binding


def require_session_for_household(
    user: AuthenticatedUser, session_id: str, household_id: str
) -> dict[str, Any]:
    """Resume a session only if owned by user and bound to the resolved household."""
    binding = require_session_owner(user, session_id)
    if binding["household_id"] != household_id:
        raise AuthorizationError(
            "Conversation session is not bound to the requested household"
        )
    return binding
