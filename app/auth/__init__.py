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

"""Authentication and household authorization primitives (SEC-01 Phase A)."""

from app.auth.authorization import (
    AuthorizationError,
    require_household_access,
    require_session_owner,
)
from app.auth.identity import (
    AuthenticatedUser,
    AuthenticationError,
    AuthIdentityProvider,
)
from app.auth.local_dev import (
    DEV_USER_HEADER,
    LocalDevIdentityProvider,
    is_insecure_identity_allowed,
)

__all__ = [
    "DEV_USER_HEADER",
    "AuthIdentityProvider",
    "AuthenticatedUser",
    "AuthenticationError",
    "AuthorizationError",
    "LocalDevIdentityProvider",
    "is_insecure_identity_allowed",
    "require_household_access",
    "require_session_owner",
]
