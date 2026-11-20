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

"""Authentication and household authorization primitives (SEC-01)."""

from app.auth.authorization import (
    AUTHORIZED_HOUSEHOLD_STATE_KEY,
    AuthorizationError,
    require_household_access,
    require_session_for_household,
    require_session_owner,
    resolve_authorized_household,
)
from app.auth.demo_seed import (
    DEMO_HOUSEHOLD_ID,
    DEMO_USER_ID,
    seed_local_demo_identity,
)
from app.auth.deps import get_current_user, raise_authorization_http
from app.auth.firebase import (
    FirebaseAuthIdentityProvider,
    UnregisteredUserError,
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
from app.auth.provider import get_identity_provider, set_identity_provider_override
from app.auth.twilio_identity import (
    DEFAULT_TWILIO_HOUSEHOLD_ID,
    TwilioSenderContext,
    get_or_create_twilio_session_binding,
    list_broadcast_whatsapp_recipients,
    normalize_whatsapp_address,
    parse_family_whatsapp_numbers,
    provision_twilio_household_from_env,
    resolve_twilio_sender,
)

__all__ = [
    "AUTHORIZED_HOUSEHOLD_STATE_KEY",
    "DEFAULT_TWILIO_HOUSEHOLD_ID",
    "DEMO_HOUSEHOLD_ID",
    "DEMO_USER_ID",
    "DEV_USER_HEADER",
    "AuthIdentityProvider",
    "AuthenticatedUser",
    "AuthenticationError",
    "AuthorizationError",
    "FirebaseAuthIdentityProvider",
    "LocalDevIdentityProvider",
    "TwilioSenderContext",
    "UnregisteredUserError",
    "get_current_user",
    "get_identity_provider",
    "get_or_create_twilio_session_binding",
    "is_insecure_identity_allowed",
    "list_broadcast_whatsapp_recipients",
    "normalize_whatsapp_address",
    "parse_family_whatsapp_numbers",
    "provision_twilio_household_from_env",
    "raise_authorization_http",
    "require_household_access",
    "require_session_for_household",
    "require_session_owner",
    "resolve_authorized_household",
    "resolve_twilio_sender",
    "seed_local_demo_identity",
    "set_identity_provider_override",
]
