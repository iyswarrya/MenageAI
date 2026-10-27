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

"""Central household and session authorization (SEC-01 Phase A)."""

from __future__ import annotations

from typing import Any

from app import db
from app.auth.identity import AuthenticatedUser


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
