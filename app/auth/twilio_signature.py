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

"""Twilio webhook signature verification (SEC-02A).

The signed URL is the configured public webhook URL. This module does not
read Host, X-Forwarded-*, or the incoming request URL.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlsplit

from twilio.request_validator import RequestValidator


class TwilioWebhookRejected(Exception):
    """Fail-closed rejection of a Twilio webhook request."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TwilioFormParams:
    """Original application/x-www-form-urlencoded fields, including repeats."""

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def __iter__(self) -> Iterator[str]:
        seen: list[str] = []
        for key, _value in self._items:
            if key not in seen:
                seen.append(key)
        return iter(seen)

    def getlist(self, key: str) -> list[str]:
        return [value for name, value in self._items if name == key]


def load_twilio_webhook_config() -> tuple[str, str]:
    """Return (auth token, exact public webhook URL) or reject.

    Both values are required. There is no unsigned-request bypass.
    """
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    public_url = os.environ.get("TWILIO_WEBHOOK_PUBLIC_URL", "").strip()
    if not token or not _is_https_webhook_url(public_url):
        raise TwilioWebhookRejected("missing_or_invalid_config")
    return token, public_url


def parse_twilio_form(form) -> TwilioFormParams:
    """Copy every text field. Non-text parts fail closed."""
    items: list[tuple[str, str]] = []
    try:
        keys = list(form.keys())
    except Exception as exc:
        raise TwilioWebhookRejected("invalid_form") from exc

    for key in keys:
        if not isinstance(key, str) or not key:
            raise TwilioWebhookRejected("invalid_form")
        try:
            values = form.getlist(key)
        except Exception as exc:
            raise TwilioWebhookRejected("invalid_form") from exc
        if not values:
            raise TwilioWebhookRejected("invalid_form")
        for value in values:
            if not isinstance(value, str):
                raise TwilioWebhookRejected("invalid_form")
            items.append((key, value))
    return TwilioFormParams(items)


def verify_twilio_webhook(params: TwilioFormParams, signature: str | None) -> None:
    """Validate X-Twilio-Signature over the configured URL and all form fields."""
    token, public_url = load_twilio_webhook_config()
    if not isinstance(signature, str) or not signature.strip():
        raise TwilioWebhookRejected("missing_signature")

    validator = RequestValidator(token)
    try:
        valid = validator.validate(public_url, params, signature.strip())
    except Exception as exc:
        raise TwilioWebhookRejected("invalid_signature") from exc
    if not valid:
        raise TwilioWebhookRejected("invalid_signature")


def webhook_dispatch_fields(
    params: TwilioFormParams,
) -> tuple[str, str, str | None, str | None] | None:
    """Return original From, To, Body, and MediaUrl0 after a valid signature.

    Repeated From, To, Body, or MediaUrl0 values are ambiguous, so the caller
    must not start background processing. This does not normalize phone numbers.
    """
    sender = _single(params, "From")
    recipient = _single(params, "To")
    if (
        sender is None
        or recipient is None
        or not sender.strip()
        or not recipient.strip()
    ):
        return None

    body_ok, body = _optional_single(params, "Body")
    media_ok, media = _optional_single(params, "MediaUrl0")
    if not body_ok or not media_ok:
        return None
    return sender, recipient, body, media


def _single(params: TwilioFormParams, key: str) -> str | None:
    values = params.getlist(key)
    if len(values) != 1:
        return None
    return values[0]


def _optional_single(params: TwilioFormParams, key: str) -> tuple[bool, str | None]:
    values = params.getlist(key)
    if not values:
        return True, None
    if len(values) != 1:
        return False, None
    return True, values[0]


def _is_https_webhook_url(url: str) -> bool:
    if not url or any(char.isspace() for char in url):
        return False
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    return True
