"""SEC-01 Phase A: membership, session ownership, and fail-closed local auth."""

import sqlite3

import pytest

from app import db
from app.auth import (
    DEV_USER_HEADER,
    AuthenticatedUser,
    AuthenticationError,
    AuthorizationError,
    LocalDevIdentityProvider,
    is_insecure_identity_allowed,
    require_household_access,
    require_session_owner,
)


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    """Isolate SQLite per test and initialize schema."""
    test_db_file = tmp_path / "test_auth_phase_a.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    yield


@pytest.fixture
def two_households():
    """Seed two households with distinct members."""
    db.create_user("user_a", display_name="Alice")
    db.create_user("user_b", display_name="Bob")
    db.create_user("user_c", display_name="Carol")
    db.create_household("hh_1", name="Household One")
    db.create_household("hh_2", name="Household Two")
    db.add_household_member("hh_1", "user_a")
    db.add_household_member("hh_1", "user_b")
    db.add_household_member("hh_2", "user_c")
    return {
        "user_a": AuthenticatedUser(user_id="user_a", auth_method="test"),
        "user_b": AuthenticatedUser(user_id="user_b", auth_method="test"),
        "user_c": AuthenticatedUser(user_id="user_c", auth_method="test"),
    }


# --- Schema / membership ---


def test_identity_tables_created():
    with db.get_connection() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "users" in tables
    assert "households" in tables
    assert "household_members" in tables
    assert "conversation_sessions" in tables


def test_foreign_keys_enforced_on_connection():
    with db.get_connection() as conn:
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert enabled == 1

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO household_members (household_id, user_id) "
                "VALUES ('missing_hh', 'missing_user')"
            )


def test_user_belongs_to_household(two_households):
    assert db.user_belongs_to_household("user_a", "hh_1") is True
    assert db.user_belongs_to_household("user_c", "hh_1") is False
    assert db.user_belongs_to_household("user_a", "hh_2") is False
    assert db.list_user_households("user_a") == ["hh_1"]


def test_multi_household_membership_supported():
    db.create_user("multi_user")
    db.create_household("hh_x")
    db.create_household("hh_y")
    db.add_household_member("hh_x", "multi_user")
    db.add_household_member("hh_y", "multi_user")
    assert set(db.list_user_households("multi_user")) == {"hh_x", "hh_y"}


# --- Authorization ---


def test_require_household_access_allows_member(two_households):
    require_household_access(two_households["user_a"], "hh_1")


def test_require_household_access_denies_non_member(two_households):
    with pytest.raises(AuthorizationError):
        require_household_access(two_households["user_c"], "hh_1")


def test_require_household_access_denies_empty_household(two_households):
    with pytest.raises(AuthorizationError):
        require_household_access(two_households["user_a"], "")


# --- Session ownership ---


def test_create_conversation_session_and_owner_check(two_households):
    binding = db.create_conversation_session("sess_a1", "user_a", "hh_1", channel="web")
    assert binding["user_id"] == "user_a"
    assert binding["household_id"] == "hh_1"
    assert db.session_owned_by_user("sess_a1", "user_a") is True
    assert db.session_owned_by_user("sess_a1", "user_b") is False

    require_session_owner(two_households["user_a"], "sess_a1")


def test_session_id_reuse_across_users_rejected(two_households):
    db.create_conversation_session("sess_shared", "user_a", "hh_1", channel="web")
    with pytest.raises(db.SessionBindingConflictError):
        db.create_conversation_session("sess_shared", "user_b", "hh_1", channel="web")


def test_session_id_reuse_across_households_rejected(two_households):
    db.create_user("user_ab")
    db.add_household_member("hh_1", "user_ab")
    db.add_household_member("hh_2", "user_ab")
    db.create_conversation_session("sess_hh", "user_ab", "hh_1", channel="web")
    with pytest.raises(db.SessionBindingConflictError):
        db.create_conversation_session("sess_hh", "user_ab", "hh_2", channel="web")


def test_idempotent_same_session_binding(two_households):
    first = db.create_conversation_session("sess_idem", "user_a", "hh_1", channel="web")
    second = db.create_conversation_session(
        "sess_idem", "user_a", "hh_1", channel="web"
    )
    assert first["session_id"] == second["session_id"]
    assert second["user_id"] == "user_a"


def test_require_session_owner_denies_other_user(two_households):
    db.create_conversation_session("sess_a2", "user_a", "hh_1", channel="web")
    with pytest.raises(AuthorizationError):
        require_session_owner(two_households["user_b"], "sess_a2")


def test_require_session_owner_denies_missing_session(two_households):
    with pytest.raises(AuthorizationError):
        require_session_owner(two_households["user_a"], "no_such_session")


def test_require_session_owner_denies_after_membership_revoked(two_households):
    db.create_conversation_session("sess_revoked", "user_a", "hh_1", channel="web")
    with db.get_connection() as conn:
        conn.execute(
            "DELETE FROM household_members WHERE user_id = ? AND household_id = ?",
            ("user_a", "hh_1"),
        )
        conn.commit()
    with pytest.raises(AuthorizationError):
        require_session_owner(two_households["user_a"], "sess_revoked")


# --- Local development identity (fail closed) ---


def _enable_local_dev(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ALLOW_INSECURE_IDENTITY", "true")


def test_is_insecure_identity_allowed_when_gated(monkeypatch):
    _enable_local_dev(monkeypatch)
    assert is_insecure_identity_allowed() is True


def test_insecure_identity_denied_without_app_env(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ALLOW_INSECURE_IDENTITY", "true")
    assert is_insecure_identity_allowed() is False


def test_insecure_identity_denied_without_allow_flag(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("ALLOW_INSECURE_IDENTITY", raising=False)
    assert is_insecure_identity_allowed() is False


def test_insecure_identity_denied_on_cloud_run(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ALLOW_INSECURE_IDENTITY", "true")
    monkeypatch.setenv("K_SERVICE", "family-receipt-agent")
    assert is_insecure_identity_allowed() is False

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("CLOUD_RUN_JOB", "some-job")
    assert is_insecure_identity_allowed() is False


def test_insecure_identity_denied_in_production_app_env(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_INSECURE_IDENTITY", "true")
    assert is_insecure_identity_allowed() is False


def test_local_dev_provider_authenticates_when_allowed(monkeypatch):
    _enable_local_dev(monkeypatch)
    provider = LocalDevIdentityProvider()
    user = provider.authenticate(headers={DEV_USER_HEADER: "dev_user_1"})
    assert user.user_id == "dev_user_1"
    assert user.auth_method == "local_dev"


def test_local_dev_provider_header_case_insensitive(monkeypatch):
    _enable_local_dev(monkeypatch)
    provider = LocalDevIdentityProvider()
    user = provider.authenticate(headers={"x-menage-dev-user": "dev_user_2"})
    assert user.user_id == "dev_user_2"


def test_local_dev_provider_fail_closed_on_cloud_run(monkeypatch):
    _enable_local_dev(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "menageai")
    provider = LocalDevIdentityProvider()
    with pytest.raises(AuthenticationError):
        provider.authenticate(headers={DEV_USER_HEADER: "dev_user_1"})


def test_local_dev_provider_requires_header(monkeypatch):
    _enable_local_dev(monkeypatch)
    provider = LocalDevIdentityProvider()
    with pytest.raises(AuthenticationError):
        provider.authenticate(headers={})


def test_local_dev_provider_rejects_empty_header(monkeypatch):
    _enable_local_dev(monkeypatch)
    provider = LocalDevIdentityProvider()
    with pytest.raises(AuthenticationError):
        provider.authenticate(headers={DEV_USER_HEADER: "   "})
