"""SEC-01 Phase C: Twilio membership identity, private sessions, DB broadcast."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth import (
    AUTHORIZED_HOUSEHOLD_STATE_KEY,
    DEFAULT_TWILIO_HOUSEHOLD_ID,
    get_or_create_twilio_session_binding,
    list_broadcast_whatsapp_recipients,
    normalize_whatsapp_address,
    parse_family_whatsapp_numbers,
    provision_twilio_household_from_env,
    resolve_twilio_sender,
)
from app.fast_api_app import app, process_webhook_async


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    test_db_file = tmp_path / "test_twilio_phase_c.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    yield


# --- normalize / provision ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("whatsapp:+15551234567", "whatsapp:+15551234567"),
        ("whatsapp:15551234567", "whatsapp:+15551234567"),
        ("+15551234567", "whatsapp:+15551234567"),
        ("15551234567", "whatsapp:+15551234567"),
        ("", None),
        ("whatsapp:not-a-number", None),
    ],
)
def test_normalize_whatsapp_address(raw, expected):
    assert normalize_whatsapp_address(raw) == expected


def test_parse_and_provision_family_numbers(monkeypatch):
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111, +15552222222, whatsapp:+15551111111",
    )
    numbers = parse_family_whatsapp_numbers()
    assert numbers == ["whatsapp:+15551111111", "whatsapp:+15552222222"]

    created = provision_twilio_household_from_env()
    assert created == 2
    assert db.get_user_by_whatsapp("whatsapp:+15551111111") is not None
    assert db.get_user_by_whatsapp("whatsapp:+15552222222") is not None
    u1 = db.get_user_by_whatsapp("whatsapp:+15551111111")
    assert db.user_belongs_to_household(u1["user_id"], DEFAULT_TWILIO_HOUSEHOLD_ID)

    # Idempotent
    assert provision_twilio_household_from_env() == 0


def test_resolve_unknown_sender_returns_none(monkeypatch):
    monkeypatch.setenv("FAMILY_WHATSAPP_NUMBERS", "whatsapp:+15551111111")
    provision_twilio_household_from_env()
    assert resolve_twilio_sender("whatsapp:+19999999999") is None
    assert resolve_twilio_sender("garbage") is None


def test_resolve_registered_sender(monkeypatch):
    monkeypatch.setenv("FAMILY_WHATSAPP_NUMBERS", "whatsapp:+15551111111")
    provision_twilio_household_from_env()
    ctx = resolve_twilio_sender("whatsapp:15551111111")
    assert ctx is not None
    assert ctx.whatsapp_e164 == "whatsapp:+15551111111"
    assert ctx.household_id == DEFAULT_TWILIO_HOUSEHOLD_ID
    assert ctx.user.auth_method == "twilio_whatsapp"


def test_broadcast_recipients_from_db_not_env(monkeypatch):
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111,whatsapp:+15552222222",
    )
    provision_twilio_household_from_env()
    # Env may still list a third number that was never provisioned into a second run —
    # broadcast must come from DB membership only.
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111,whatsapp:+15552222222,whatsapp:+15553333333",
    )
    recipients = list_broadcast_whatsapp_recipients(
        DEFAULT_TWILIO_HOUSEHOLD_ID, exclude_whatsapp="whatsapp:+15551111111"
    )
    assert recipients == ["whatsapp:+15552222222"]
    assert "whatsapp:+15553333333" not in recipients


def test_twilio_session_private_per_user(monkeypatch):
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111,whatsapp:+15552222222",
    )
    provision_twilio_household_from_env()
    a = resolve_twilio_sender("whatsapp:+15551111111")
    b = resolve_twilio_sender("whatsapp:+15552222222")
    assert a and b
    sess_a = get_or_create_twilio_session_binding(a.user.user_id, a.household_id)
    sess_b = get_or_create_twilio_session_binding(b.user.user_id, b.household_id)
    assert sess_a["session_id"] != sess_b["session_id"]
    assert (
        sess_a["household_id"] == sess_b["household_id"] == DEFAULT_TWILIO_HOUSEHOLD_ID
    )
    # Reuse same binding
    again = get_or_create_twilio_session_binding(a.user.user_id, a.household_id)
    assert again["session_id"] == sess_a["session_id"]


@pytest.mark.asyncio
async def test_unknown_sender_does_not_invoke_agent(monkeypatch):
    monkeypatch.setenv("FAMILY_WHATSAPP_NUMBERS", "whatsapp:+15551111111")
    provision_twilio_household_from_env()

    with TestClient(app) as client:
        runner = MagicMock()
        runner.run_async = AsyncMock()
        runner.session_service = MagicMock()
        client.app.state.runner = runner
        client.app.state.agent_app_name = "app"

        await process_webhook_async(
            From="whatsapp:+19999999999",
            To="whatsapp:+15550000000",
            Body="When did we buy milk?",
            MediaUrl0=None,
        )
        runner.run_async.assert_not_called()


@pytest.mark.asyncio
async def test_registered_query_replies_without_broadcast(monkeypatch):
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111,whatsapp:+15552222222",
    )
    provision_twilio_household_from_env()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")

    async def fake_events(*_a, **_k):
        evt = MagicMock()
        evt.node_info = MagicMock()
        evt.node_info.name = "query_agent"
        evt.content = MagicMock()
        part = MagicMock()
        part.text = "You bought milk yesterday"
        evt.content.parts = [part]
        evt.output = None
        yield evt

    sent: list[tuple[str, str]] = []

    def fake_send(_sid, _tok, _frm, to, body):
        sent.append((to, body))

    with TestClient(app) as client:
        runner = MagicMock()
        runner.run_async = fake_events
        session = MagicMock()
        session.id = "sess-q"
        session.state = {}
        runner.session_service.get_session = AsyncMock(return_value=session)
        runner.session_service.create_session = AsyncMock(return_value=session)
        client.app.state.runner = runner
        client.app.state.agent_app_name = "app"

        with patch("app.fast_api_app.twilio_send_message", side_effect=fake_send):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="When did we buy milk?",
                MediaUrl0=None,
            )

        assert (
            session.state[AUTHORIZED_HOUSEHOLD_STATE_KEY] == DEFAULT_TWILIO_HOUSEHOLD_ID
        )
        assert len(sent) == 1
        assert sent[0][0] == "whatsapp:+15551111111"
        assert "milk" in sent[0][1].lower()


@pytest.mark.asyncio
async def test_registered_receipt_broadcasts_to_other_members(monkeypatch):
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111,whatsapp:+15552222222",
    )
    provision_twilio_household_from_env()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")

    async def fake_events(*_a, **_k):
        intent_evt = MagicMock()
        intent_evt.node_info = MagicMock()
        intent_evt.node_info.name = "intent_classifier"
        intent_evt.output = MagicMock()
        intent_evt.output.intent = "receipt"
        intent_evt.content = None
        yield intent_evt

        resp_evt = MagicMock()
        resp_evt.node_info = MagicMock()
        resp_evt.node_info.name = "response_agent"
        resp_evt.content = MagicMock()
        part = MagicMock()
        part.text = "Logged Safeway coffee $9.99"
        resp_evt.content.parts = [part]
        resp_evt.output = None
        yield resp_evt

    sent: list[str] = []

    def fake_send(_sid, _tok, _frm, to, body):
        sent.append(to)

    with TestClient(app) as client:
        runner = MagicMock()
        runner.run_async = fake_events
        session = MagicMock()
        session.id = "sess-r"
        session.state = {}
        runner.session_service.get_session = AsyncMock(return_value=session)
        runner.session_service.create_session = AsyncMock(return_value=session)
        client.app.state.runner = runner
        client.app.state.agent_app_name = "app"

        with patch("app.fast_api_app.twilio_send_message", side_effect=fake_send):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="Safeway coffee 9.99",
                MediaUrl0=None,
            )

        assert "whatsapp:+15551111111" in sent
        assert "whatsapp:+15552222222" in sent
        assert len(sent) == 2


@pytest.mark.asyncio
async def test_failed_receipt_not_broadcast(monkeypatch):
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111,whatsapp:+15552222222",
    )
    provision_twilio_household_from_env()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")

    async def fake_events(*_a, **_k):
        intent_evt = MagicMock()
        intent_evt.node_info = MagicMock()
        intent_evt.node_info.name = "intent_classifier"
        intent_evt.output = MagicMock()
        intent_evt.output.intent = "receipt"
        intent_evt.content = None
        yield intent_evt

        fail_evt = MagicMock()
        fail_evt.node_info = MagicMock()
        fail_evt.node_info.name = "invalid_receipt_responder"
        fail_evt.output = (
            "I could not reliably read this receipt. Please upload a clearer image."
        )
        fail_evt.content = None
        yield fail_evt

    sent: list[str] = []

    def fake_send(_sid, _tok, _frm, to, body):
        sent.append(to)

    with TestClient(app) as client:
        runner = MagicMock()
        runner.run_async = fake_events
        session = MagicMock()
        session.id = "sess-f"
        session.state = {}
        runner.session_service.get_session = AsyncMock(return_value=session)
        runner.session_service.create_session = AsyncMock(return_value=session)
        client.app.state.runner = runner
        client.app.state.agent_app_name = "app"

        with patch("app.fast_api_app.twilio_send_message", side_effect=fake_send):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="blurry receipt",
                MediaUrl0=None,
            )

        assert sent == ["whatsapp:+15551111111"]


def test_webhook_endpoint_still_public():
    with TestClient(app) as client:
        res = client.post(
            "/twilio/webhook",
            data={
                "From": "whatsapp:+19999999999",
                "To": "whatsapp:+15550000000",
                "Body": "hi",
            },
        )
        assert res.status_code == 200
        assert "Response" in res.text
