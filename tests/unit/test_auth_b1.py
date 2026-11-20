"""SEC-01 Phase B1: Firebase auth, household resolution, API protection, ADK routes."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth import (
    AuthenticatedUser,
    AuthenticationError,
    AuthorizationError,
    FirebaseAuthIdentityProvider,
    UnregisteredUserError,
    is_insecure_identity_allowed,
    resolve_authorized_household,
    set_identity_provider_override,
)
from app.auth.firebase import reset_firebase_app_for_tests
from app.fast_api_app import app


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    test_db_file = tmp_path / "test_auth_b1.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    set_identity_provider_override(None)
    reset_firebase_app_for_tests()
    yield
    set_identity_provider_override(None)
    reset_firebase_app_for_tests()


class StubProvider:
    def __init__(
        self, user: AuthenticatedUser | None = None, error: Exception | None = None
    ):
        self.user = user
        self.error = error

    def authenticate(self, *, headers: Mapping[str, str]) -> AuthenticatedUser:
        if self.error:
            raise self.error
        assert self.user is not None
        return self.user


def _seed_user(
    user_id: str,
    *,
    auth_subject: str | None = None,
    households: list[str] | None = None,
):
    db.create_user(user_id, display_name=user_id, auth_subject=auth_subject)
    for hh in households or []:
        with db.get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM households WHERE household_id = ?", (hh,)
            ).fetchone()
        if not exists:
            db.create_household(hh)
        if not db.user_belongs_to_household(user_id, hh):
            db.add_household_member(hh, user_id)


# --- resolve_authorized_household ---


def test_resolve_household_zero_memberships():
    db.create_user("lonely")
    user = AuthenticatedUser(user_id="lonely", auth_method="test")
    with pytest.raises(AuthorizationError):
        resolve_authorized_household(user, None)


def test_resolve_household_single_auto():
    _seed_user("u1", households=["hh_only"])
    user = AuthenticatedUser(user_id="u1", auth_method="test")
    assert resolve_authorized_household(user, None) == "hh_only"
    assert resolve_authorized_household(user, "hh_only") == "hh_only"


def test_resolve_household_multiple_requires_explicit():
    _seed_user("u2", households=["hh_a", "hh_b"])
    user = AuthenticatedUser(user_id="u2", auth_method="test")
    with pytest.raises(AuthorizationError, match="household_id is required"):
        resolve_authorized_household(user, None)
    assert resolve_authorized_household(user, "hh_b") == "hh_b"
    with pytest.raises(AuthorizationError):
        resolve_authorized_household(user, "hh_other")


# --- Firebase provider ---


def test_firebase_missing_bearer():
    provider = FirebaseAuthIdentityProvider()
    with pytest.raises(AuthenticationError):
        provider.authenticate(headers={})


def test_firebase_invalid_token(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "test-project")
    reset_firebase_app_for_tests()

    def _boom(*_a, **_k):
        raise ValueError("bad token")

    monkeypatch.setattr("app.auth.firebase._ensure_firebase_app", lambda: None)
    import firebase_admin.auth as firebase_auth

    monkeypatch.setattr(firebase_auth, "verify_id_token", _boom)
    provider = FirebaseAuthIdentityProvider()
    with pytest.raises(AuthenticationError):
        provider.authenticate(headers={"Authorization": "Bearer fake"})


def test_firebase_valid_registered(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "test-project")
    reset_firebase_app_for_tests()
    _seed_user("internal_1", auth_subject="firebase_uid_1", households=["hh_1"])

    monkeypatch.setattr("app.auth.firebase._ensure_firebase_app", lambda: None)

    def _ok(token, check_revoked=False):
        assert token == "good-token"
        return {"uid": "firebase_uid_1"}

    import firebase_admin.auth as firebase_auth

    monkeypatch.setattr(firebase_auth, "verify_id_token", _ok)
    provider = FirebaseAuthIdentityProvider()
    user = provider.authenticate(headers={"Authorization": "Bearer good-token"})
    assert user.user_id == "internal_1"
    assert user.auth_subject == "firebase_uid_1"
    assert user.auth_method == "firebase"


def test_firebase_valid_unregistered(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "test-project")
    reset_firebase_app_for_tests()
    monkeypatch.setattr("app.auth.firebase._ensure_firebase_app", lambda: None)

    import firebase_admin.auth as firebase_auth

    monkeypatch.setattr(
        firebase_auth, "verify_id_token", lambda *a, **k: {"uid": "unknown_uid"}
    )
    provider = FirebaseAuthIdentityProvider()
    with pytest.raises(UnregisteredUserError):
        provider.authenticate(headers={"Authorization": "Bearer good-token"})


# --- API entry points ---


def test_purchases_requires_auth():
    with TestClient(app) as client:
        res = client.get("/purchases")
        assert res.status_code == 401


def test_purchases_member_ok_non_member_forbidden():
    _seed_user("alice", households=["hh_1"])
    _seed_user("carol", households=["hh_2"])
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="alice", auth_method="test"))
    )
    with TestClient(app) as client:
        db.save_receipt_and_items(
            "Safeway", "2026-07-01", 1.0, [("Milk", 1.0)], household_id="hh_1"
        )
        ok = client.get("/purchases", params={"household_id": "hh_1"})
        assert ok.status_code == 200
        assert len(ok.json()) == 1

        denied = client.get("/purchases", params={"household_id": "hh_2"})
        assert denied.status_code == 403


def test_forged_body_user_id_ignored(monkeypatch):
    _seed_user("alice", households=["hh_1"])
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="alice", auth_method="test"))
    )

    async def fake_collect(content, user_id, session_id):
        assert user_id == "alice"
        return "ok"

    monkeypatch.setattr("app.fast_api_app._collect_agent_response", fake_collect)
    with TestClient(app) as client:
        # Body user_id tries to impersonate carol; auth still alice.
        res = client.post(
            "/agent/message",
            json={
                "message": "hello",
                "user_id": "carol",
                "household_id": "hh_1",
            },
        )
        assert res.status_code == 200
        assert res.json()["response"] == "ok"


def test_unregistered_identity_forbidden():
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="ghost", auth_method="test"))
    )
    with TestClient(app) as client:
        res = client.get("/purchases")
        assert res.status_code == 403


def test_firebase_unregistered_maps_to_403():
    set_identity_provider_override(StubProvider(error=UnregisteredUserError("nope")))
    with TestClient(app) as client:
        res = client.get("/purchases")
        assert res.status_code == 403


def test_invalid_credentials_map_to_401():
    set_identity_provider_override(StubProvider(error=AuthenticationError("bad")))
    with TestClient(app) as client:
        res = client.get("/purchases")
        assert res.status_code == 401


def test_health_and_ready_public():
    with TestClient(app) as client:
        assert client.get("/health").status_code in (200, 500)
        assert client.get("/ready").status_code in (200, 500)


def test_adk_and_a2a_routes_absent():
    with TestClient(app) as client:
        paths = {getattr(r, "path", "") for r in app.routes}
        for banned in ("/run", "/run_sse", "/list-apps", "/dev-ui"):
            assert banned not in paths
        assert not any(p.startswith("/apps/") for p in paths if p)
        assert not any(p.startswith("/a2a") for p in paths if p)
        # Custom protected routes remain
        assert "/agent/message" in paths
        assert "/purchases" in paths
        assert "/twilio/webhook" in paths
        # Probe a former ADK path
        assert client.post("/run").status_code == 404
        assert client.get("/apps/app/users/x/sessions").status_code == 404


def test_local_dev_fail_closed_on_cloud_run(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ALLOW_INSECURE_IDENTITY", "true")
    monkeypatch.setenv("K_SERVICE", "menageai")
    assert is_insecure_identity_allowed() is False


def test_unknown_session_id_not_silently_created():
    _seed_user("alice", households=["hh_1"])
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="alice", auth_method="test"))
    )
    with TestClient(app) as client:
        res = client.post(
            "/agent/message",
            json={
                "message": "hi",
                "household_id": "hh_1",
                "session_id": "does-not-exist",
            },
        )
        assert res.status_code == 403


def test_auto_household_resolution_on_purchases():
    _seed_user("solo", households=["hh_solo"])
    set_identity_provider_override(
        StubProvider(AuthenticatedUser(user_id="solo", auth_method="test"))
    )
    with TestClient(app) as client:
        res = client.get("/purchases")
        assert res.status_code == 200
        assert res.json() == []
