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

"""Identity types and AuthIdentityProvider protocol (SEC-01)."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class AuthenticationError(Exception):
    """Raised when credentials are missing, invalid, or the provider is disabled."""


class AuthenticatedUser(BaseModel):
    """Trusted identity produced only by an AuthIdentityProvider."""

    user_id: str = Field(description="Internal stable user primary key")
    auth_subject: str | None = Field(
        default=None,
        description="External IdP subject (e.g. Firebase UID) when applicable",
    )
    auth_method: str = Field(
        description="How identity was established (e.g. local_dev, firebase)"
    )


@runtime_checkable
class AuthIdentityProvider(Protocol):
    """Replaceable authentication boundary.

    Production must use a provider that cryptographically verifies credentials
    (Firebase ID token signature, issuer, audience, and expiration). Do not
    ship unverified stub providers as production authentication.
    """

    def authenticate(self, *, headers: Mapping[str, str]) -> AuthenticatedUser:
        """Resolve a trusted AuthenticatedUser from request headers.

        Raises:
            AuthenticationError: If identity cannot be established.
        """
        ...
