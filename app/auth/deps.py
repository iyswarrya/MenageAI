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

"""FastAPI authentication dependencies (SEC-01 Phase B1)."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app import db
from app.auth.authorization import AuthorizationError
from app.auth.firebase import UnregisteredUserError
from app.auth.identity import AuthenticatedUser, AuthenticationError
from app.auth.provider import get_identity_provider


def get_current_user(request: Request) -> AuthenticatedUser:
    """Authenticate the request and require a registered MenageAI user."""
    provider = get_identity_provider()
    try:
        user = provider.authenticate(headers=request.headers)
    except UnregisteredUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Identity is not registered with MenageAI",
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        ) from exc

    # Require a registered users row for every auth method (incl. local-dev).
    if not _user_exists(user.user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Identity is not registered with MenageAI",
        )
    return user


def raise_authorization_http(exc: AuthorizationError) -> None:
    """Convert AuthorizationError to a non-disclosing HTTP 403."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access denied",
    ) from exc


def _user_exists(user_id: str) -> bool:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None
