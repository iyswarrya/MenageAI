"""SEC-01 Phase B2: trusted household propagation and session isolation."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.agent import AgentRunLoggerPlugin, _trusted_household_id
from app.auth import (
    AUTHORIZED_HOUSEHOLD_STATE_KEY,
    DEMO_HOUSEHOLD_ID,
    DEMO_USER_ID,
    AuthenticatedUser,
    seed_local_demo_identity,
    set_identity_provider_override,
)
from app.fast_api_app import app


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    test_db_file = tmp_path / "test_auth_b2.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    set_identity_provider_override(None)
    yield
    set_identity_provider_override(None)


class StubProvider:
    def __init__(self, user: AuthenticatedUser):
        self.user = user

    def authenticate(self, *, headers: Mapping[str, str]) -> AuthenticatedUser:
        return self.user


def _seed_member(user_id: str, household_id: str):
    if db.get_user(user_id) is None:
        db.create_user(user_id)
    with db.get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM households WHERE household_id = ?", (household_id,)
        ).fetchone()
    if not exists:
        db.create_household(household_id)
    if not db.user_belongs_to_household(user_id, household_id):
        db.add_household_member(household_id, user_id)


@pytest.mark.asyncio
async def test_plugin_uses_authorized_household_not_session_id():
    plugin = AgentRunLoggerPlugin(name="test")
    session = SimpleNamespace(
        id="session-should-not-be-household",
        state={AUTHORIZED_HOUSEHOLD_STATE_KEY: "hh_trusted"},
    )
    ctx = SimpleNamespace(
        session=session,
        user_id="user_a",
        invocation_id="inv-1",
        _get_events=lambda **k: [],
    )
    await plugin.before_run_callback(invocation_context=ctx)
    assert session.state["current_run"]["household_id"] == "hh_trusted"
    assert session.state[AUTHORIZED_HOUSEHOLD_STATE_KEY] == "hh_trusted"


@pytest.mark.asyncio
async def test_plugin_fail_closed_without_authorized_household():
    plugin = AgentRunLoggerPlugin(name="test")
    session = SimpleNamespace(id="sess", state={})
    ctx = SimpleNamespace(
        session=session,
        user_id="user_a",
        invocation_id="inv-2",
        _get_events=lambda **k: [],
    )
    await plugin.before_run_callback(invocation_context=ctx)
    assert session.state["current_run"]["household_id"] == "unauthorized"


def test_trusted_household_prefers_state_key_over_current_run():
    ctx = SimpleNamespace(
        state={
            AUTHORIZED_HOUSEHOLD_STATE_KEY: "hh_real",
            "current_run": {"household_id": "hh_forged"},
        }
    )
    assert _trusted_household_id(ctx) == "hh_real"
    assert ctx.state["current_run"]["household_id"] == "hh_real"


def test_shared_ledger_private_sessions_and_cross_household():
    _seed_member("user_a", "hh_h")
    _seed_member("user_b", "hh_h")
    _seed_member("user_c", "hh_other")

    db.save_receipt_and_items(
        "Target", "2026-07-02", 5.0, [("Eggs", 5.0)], household_id="hh_h"
    )

    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="user_b", auth_method="test"))
    )
    with TestClient(app) as client:
        # B can read shared ledger
        purchases = client.get("/purchases", params={"household_id": "hh_h"})
        assert purchases.status_code == 200
        assert any(p["item_name"] == "Eggs" for p in purchases.json())

        # C cannot
        set_identity_provider_override(
            StubProvider(AuthenticatedUser(user_id="user_c", auth_method="test"))
        )
        denied = client.get("/purchases", params={"household_id": "hh_h"})
        assert denied.status_code == 403

    # Bind a private session for A
    db.create_conversation_session("sess_a", "user_a", "hh_h", channel="web")
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="user_b", auth_method="test"))
    )
    with TestClient(app) as client:
        stolen = client.post(
            "/agent/message",
            json={
                "message": "hi",
                "household_id": "hh_h",
                "session_id": "sess_a",
            },
        )
        assert stolen.status_code == 403


def test_revoked_membership_blocks_session_resume():
    _seed_member("user_a", "hh_h")
    db.create_conversation_session("sess_rev", "user_a", "hh_h", channel="web")
    with db.get_connection() as conn:
        conn.execute(
            "DELETE FROM household_members WHERE user_id = ? AND household_id = ?",
            ("user_a", "hh_h"),
        )
        conn.commit()

    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="user_a", auth_method="test"))
    )
    with TestClient(app) as client:
        res = client.post(
            "/agent/message",
            json={
                "message": "hi",
                "household_id": "hh_h",
                "session_id": "sess_rev",
            },
        )
        assert res.status_code == 403


def test_new_session_is_server_generated(monkeypatch):
    _seed_member("user_a", "hh_h")
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="user_a", auth_method="test"))
    )

    async def fake_collect(*_a, **_k):
        return "ok"

    monkeypatch.setattr("app.fast_api_app._collect_agent_response", fake_collect)

    with TestClient(app) as client:
        res = client.post(
            "/agent/message",
            json={"message": "hi", "household_id": "hh_h"},
        )
        assert res.status_code == 200
        sid = res.json()["session_id"]
        assert sid
        binding = db.get_conversation_session(sid)
        assert binding is not None
        assert binding["user_id"] == "user_a"
        assert binding["household_id"] == "hh_h"


def test_agent_run_logs_authorized_household(monkeypatch):
    _seed_member("user_a", "hh_h")
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="user_a", auth_method="test"))
    )

    async def fake_collect(content, user_id, session_id):
        db.log_agent_run(
            request_id="req",
            household_id="hh_h",
            user_id=user_id,
            intent="query",
            route_taken="query",
            tools_called=[],
            mcp_success=True,
            pii_redacted_count=0,
        )
        return "answer"

    monkeypatch.setattr("app.fast_api_app._collect_agent_response", fake_collect)

    with TestClient(app) as client:
        res = client.post(
            "/agent/message",
            json={"message": "when coffee?", "household_id": "hh_h"},
        )
        assert res.status_code == 200
        runs = client.get("/agent/runs", params={"household_id": "hh_h"})
        assert runs.status_code == 200
        assert runs.json()[0]["household_id"] == "hh_h"
        assert runs.json()[0]["user_id"] == "user_a"


def test_demo_seed_only_when_local_insecure(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_INSECURE_IDENTITY", "true")
    assert seed_local_demo_identity() is False
    assert db.get_user(DEMO_USER_ID) is None

    monkeypatch.setenv("APP_ENV", "local")
    assert seed_local_demo_identity() is True
    assert db.get_user(DEMO_USER_ID) is not None
    assert db.user_belongs_to_household(DEMO_USER_ID, DEMO_HOUSEHOLD_ID)


def test_missing_adk_session_with_db_binding_denied(monkeypatch):
    _seed_member("user_a", "hh_h")
    db.create_conversation_session("orphan_sess", "user_a", "hh_h", channel="web")
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="user_a", auth_method="test"))
    )

    with TestClient(app) as client:
        # Binding exists in SQLite but ADK in-memory session service has no session.
        res = client.post(
            "/agent/message",
            json={
                "message": "hi",
                "household_id": "hh_h",
                "session_id": "orphan_sess",
            },
        )
        assert res.status_code == 403
