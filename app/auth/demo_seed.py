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

"""Idempotent local-development demo identity seed (SEC-01 Phase B2)."""

from __future__ import annotations

import logging

from app import db
from app.auth.local_dev import is_insecure_identity_allowed

logger = logging.getLogger(__name__)

DEMO_USER_ID = "demo_user"
DEMO_HOUSEHOLD_ID = "demo_household"
DEMO_DISPLAY_NAME = "Demo User"


def seed_local_demo_identity() -> bool:
    """Create demo user/household/membership when insecure local identity is allowed.

    Returns True if seeding ran (or was already present), False if skipped.
    Never seeds in production / Cloud Run (gated by is_insecure_identity_allowed).
    """
    if not is_insecure_identity_allowed():
        return False

    if db.get_user(DEMO_USER_ID) is None:
        db.create_user(DEMO_USER_ID, display_name=DEMO_DISPLAY_NAME)

    with db.get_connection() as conn:
        hh = conn.execute(
            "SELECT 1 FROM households WHERE household_id = ?",
            (DEMO_HOUSEHOLD_ID,),
        ).fetchone()
    if hh is None:
        db.create_household(DEMO_HOUSEHOLD_ID, name="Demo Family")

    if not db.user_belongs_to_household(DEMO_USER_ID, DEMO_HOUSEHOLD_ID):
        db.add_household_member(DEMO_HOUSEHOLD_ID, DEMO_USER_ID)

    logger.info(
        "Local demo identity ready: user_id=%s household_id=%s",
        DEMO_USER_ID,
        DEMO_HOUSEHOLD_ID,
    )
    return True
