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

"""Tightly gated local-development identity provider (SEC-01 Phase A).

This provider is intentionally insecure and must never activate on Cloud Run
or any non-local environment. Production identity belongs in a separate
Firebase ID token verifier (Phase B+), not a stub here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from app.auth.identity import AuthenticatedUser, AuthenticationError

DEV_USER_HEADER = "X-Menage-Dev-User"

_ALLOWED_APP_ENVS = frozenset({"local", "development", "test"})


def is_insecure_identity_allowed() -> bool:
    """Return True only when all local-dev identity gates pass.

    Fail closed when:
    - Running on Cloud Run (K_SERVICE or CLOUD_RUN_JOB set)
    - APP_ENV is missing or not local/development/test
    - ALLOW_INSECURE_IDENTITY is not exactly "true"
    """
    if os.environ.get("K_SERVICE"):
        return False
    if os.environ.get("CLOUD_RUN_JOB"):
        return False

    app_env = os.environ.get("APP_ENV", "").strip().lower()
    if app_env not in _ALLOWED_APP_ENVS:
        return False

    if os.environ.get("ALLOW_INSECURE_IDENTITY", "").strip().lower() != "true":
        return False

    return True


class LocalDevIdentityProvider:
    """Maps X-Menage-Dev-User to AuthenticatedUser when insecure identity is allowed.

    Never use this class as production authentication.
    """

    def authenticate(self, *, headers: Mapping[str, str]) -> AuthenticatedUser:
        if not is_insecure_identity_allowed():
            raise AuthenticationError(
                "Local development identity is disabled in this environment"
            )

        user_id = _header_value(headers, DEV_USER_HEADER)
        if not user_id:
            raise AuthenticationError(
                f"Missing or empty {DEV_USER_HEADER} header for local development identity"
            )

        return AuthenticatedUser(
            user_id=user_id,
            auth_subject=None,
            auth_method="local_dev",
        )


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup; returns stripped value or None."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            stripped = (value or "").strip()
            return stripped or None
    return None
