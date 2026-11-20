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

"""Identity provider factory (SEC-01 Phase B1)."""

from __future__ import annotations

from app.auth.firebase import FirebaseAuthIdentityProvider
from app.auth.identity import AuthIdentityProvider
from app.auth.local_dev import LocalDevIdentityProvider, is_insecure_identity_allowed

_override_provider: AuthIdentityProvider | None = None


def set_identity_provider_override(provider: AuthIdentityProvider | None) -> None:
    """Override the process provider (tests only)."""
    global _override_provider
    _override_provider = provider


def get_identity_provider() -> AuthIdentityProvider:
    """Return the configured trusted identity provider.

    Local-dev is used only when all insecure-identity gates pass.
    Otherwise Firebase is required (fail closed if misconfigured at auth time).
    """
    if _override_provider is not None:
        return _override_provider
    if is_insecure_identity_allowed():
        return LocalDevIdentityProvider()
    return FirebaseAuthIdentityProvider()
