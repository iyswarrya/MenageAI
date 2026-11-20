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

"""Firebase Authentication ID token provider (SEC-01 Phase B1)."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping

from app import db
from app.auth.identity import AuthenticatedUser, AuthenticationError

logger = logging.getLogger(__name__)


class UnregisteredUserError(Exception):
    """Valid Firebase identity that is not registered in MenageAI."""


_firebase_init_lock = threading.Lock()
_firebase_app_initialized = False


def _ensure_firebase_app() -> None:
    """Initialize firebase-admin once per process. Fail closed if misconfigured."""
    global _firebase_app_initialized
    if _firebase_app_initialized:
        return

    with _firebase_init_lock:
        if _firebase_app_initialized:
            return

        project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
        if not project_id:
            raise AuthenticationError(
                "Firebase authentication is not configured (FIREBASE_PROJECT_ID missing)"
            )

        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError as exc:
            raise AuthenticationError(
                "Firebase authentication is unavailable (firebase-admin not installed)"
            ) from exc

        if not firebase_admin._apps:
            cred_path = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
            if cred_path:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred, {"projectId": project_id})
            else:
                # Application Default Credentials (ADC)
                firebase_admin.initialize_app(options={"projectId": project_id})

        _firebase_app_initialized = True


def reset_firebase_app_for_tests() -> None:
    """Reset init flag for unit tests. Does not delete firebase apps."""
    global _firebase_app_initialized
    _firebase_app_initialized = False


class FirebaseAuthIdentityProvider:
    """Verifies Firebase ID tokens and maps UID to a registered MenageAI user."""

    def authenticate(self, *, headers: Mapping[str, str]) -> AuthenticatedUser:
        token = _bearer_token(headers)
        if not token:
            raise AuthenticationError("Missing or invalid Authorization Bearer token")

        _ensure_firebase_app()

        try:
            from firebase_admin import auth as firebase_auth

            decoded = firebase_auth.verify_id_token(token, check_revoked=False)
        except AuthenticationError:
            raise
        except Exception:
            # Do not log the token or decoded claims.
            logger.info("Firebase ID token verification failed")
            raise AuthenticationError("Invalid or expired Firebase ID token") from None

        firebase_uid = decoded.get("uid") or decoded.get("sub")
        if not firebase_uid or not isinstance(firebase_uid, str):
            raise AuthenticationError("Firebase token missing subject")

        user_row = db.get_user_by_auth_subject(firebase_uid)
        if user_row is None:
            raise UnregisteredUserError(
                "Firebase identity is not registered with MenageAI"
            )

        return AuthenticatedUser(
            user_id=user_row["user_id"],
            auth_subject=firebase_uid,
            auth_method="firebase",
        )


def _bearer_token(headers: Mapping[str, str]) -> str | None:
    auth_value = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            auth_value = (value or "").strip()
            break
    if not auth_value:
        return None
    parts = auth_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None
