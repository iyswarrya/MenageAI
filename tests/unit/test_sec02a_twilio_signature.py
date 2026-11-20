"""SEC-02A: Twilio webhook signature verification. Media hardening is SEC-02B."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import db
from app.auth.twilio_signature import (
    load_twilio_webhook_config,
    verify_twilio_webhook,
)
from app.fast_api_app import app, twilio_webhook

TOKEN = "test-twilio-auth-token"
PUBLIC_URL = "https://menage.example.com/twilio/webhook"
OTHER_URL = "https://menage.example.com/twilio/webhook?x=1"


def _sign(url: str, params: dict[str, str], token: str = TOKEN) -> str:
    """Independent HMAC-SHA1 of the exact URL and form fields."""
    payload = url
    for key in sorted(params):
        payload += key + params[key]
    digest = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(digest.digest()).decode("utf-8")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    test_db_file = tmp_path / "test_sec02a.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    yield


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_WEBHOOK_PUBLIC_URL", PUBLIC_URL)


def _params(**overrides: str) -> dict[str, str]:
    params = {
        "From": "whatsapp:+15551111111",
        "To": "whatsapp:+15550000000",
        "Body": "When did we buy milk?",
        "NumMedia": "0",
    }
    params.update(overrides)
    return params


def _post(client: TestClient, params: dict[str, str], signature: str | None):
    headers = {
        "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-Proto": "http",
    }
    if signature is not None:
        headers["X-Twilio-Signature"] = signature
    return client.post("/twilio/webhook", data=params, headers=headers)


def test_validator_does_not_read_forwarded_headers():
    for obj in (load_twilio_webhook_config, verify_twilio_webhook, twilio_webhook):
        source = inspect.getsource(obj)
        assert "X-Forwarded" not in source
        assert "request.url" not in source


def test_valid_signature_schedules_original_fields(webhook_env):
    params = _params(
        From="whatsapp:15551111111", MediaUrl0="https://api.twilio.com/media/0"
    )
    with (
        TestClient(app) as client,
        patch("app.fast_api_app.process_webhook_async") as process,
    ):
        res = _post(client, params, _sign(PUBLIC_URL, params))
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/xml")
        assert "<Response></Response>" in res.text
        process.assert_called_once_with(
            "whatsapp:15551111111",
            "whatsapp:+15550000000",
            "When did we buy milk?",
            "https://api.twilio.com/media/0",
        )


def test_valid_signature_with_query_string(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("TWILIO_WEBHOOK_PUBLIC_URL", OTHER_URL)
    params = _params()
    with (
        TestClient(app) as client,
        patch("app.fast_api_app.process_webhook_async") as process,
    ):
        res = _post(client, params, _sign(OTHER_URL, params))
        assert res.status_code == 200
        process.assert_called_once()


@pytest.mark.parametrize(
    "signature_for",
    [
        "missing",
        "invalid",
        "tampered_body",
        "added_field",
        "wrong_public_url",
        "inbound_url",
    ],
)
def test_rejected_signatures_do_no_work(webhook_env, signature_for):
    params = _params()
    posted = dict(params)
    signature: str | None
    if signature_for == "missing":
        signature = None
    elif signature_for == "invalid":
        signature = "not-a-valid-signature"
    elif signature_for == "tampered_body":
        signature = _sign(PUBLIC_URL, params)
        posted["Body"] = "tampered"
    elif signature_for == "added_field":
        signature = _sign(PUBLIC_URL, params)
        posted["ProfileName"] = "Mallory"
    elif signature_for == "wrong_public_url":
        signature = _sign(OTHER_URL, params)
    else:
        signature = _sign("http://testserver/twilio/webhook", params)

    with TestClient(app) as client:
        runner = MagicMock()
        runner.run_async = AsyncMock()
        client.app.state.runner = runner
        with (
            patch("app.fast_api_app.process_webhook_async") as process,
            patch("app.fast_api_app.resolve_twilio_sender") as resolve_sender,
            patch("app.db.get_user_by_whatsapp") as lookup_user,
            patch("app.fast_api_app.twilio_send_message") as send_message,
            patch("requests.get") as download,
        ):
            res = _post(client, posted, signature)

        assert res.status_code == 403
        assert res.json()["detail"] == "Forbidden"
        assert "15551111111" not in res.text
        process.assert_not_called()
        resolve_sender.assert_not_called()
        lookup_user.assert_not_called()
        send_message.assert_not_called()
        download.assert_not_called()
        runner.run_async.assert_not_called()


@pytest.mark.parametrize(
    ("token", "public_url"),
    [
        ("", PUBLIC_URL),
        ("   ", PUBLIC_URL),
        (TOKEN, ""),
        (TOKEN, "   "),
        (TOKEN, "http://menage.example.com/twilio/webhook"),
        (TOKEN, "https://"),
        (TOKEN, "not a url"),
        (TOKEN, "https://user:secret@menage.example.com/twilio/webhook"),
    ],
)
def test_missing_or_invalid_config_rejects_without_work(monkeypatch, token, public_url):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", token)
    monkeypatch.setenv("TWILIO_WEBHOOK_PUBLIC_URL", public_url)
    params = _params()
    signature = _sign(PUBLIC_URL, params)
    with (
        TestClient(app) as client,
        patch("app.fast_api_app.process_webhook_async") as process,
        patch("app.fast_api_app.resolve_twilio_sender") as resolve_sender,
        patch("app.db.get_user_by_whatsapp") as lookup_user,
    ):
        res = _post(client, params, signature)
        assert res.status_code == 403
        process.assert_not_called()
        resolve_sender.assert_not_called()
        lookup_user.assert_not_called()


def test_absent_env_vars_reject(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_WEBHOOK_PUBLIC_URL", raising=False)
    params = _params()
    with (
        TestClient(app) as client,
        patch("app.fast_api_app.process_webhook_async") as process,
    ):
        res = _post(client, params, _sign(PUBLIC_URL, params))
        assert res.status_code == 403
        process.assert_not_called()


def test_repeated_from_is_not_dispatched(webhook_env):
    """A valid signature still must not choose between repeated sender fields."""
    items = [
        ("From", "whatsapp:+15551111111"),
        ("From", "whatsapp:+15552222222"),
        ("To", "whatsapp:+15550000000"),
        ("Body", "hi"),
    ]

    class _Multi:
        def __iter__(self):
            seen: list[str] = []
            for key, _value in items:
                if key not in seen:
                    seen.append(key)
            return iter(seen)

        def getlist(self, key: str) -> list[str]:
            return [value for name, value in items if name == key]

    signature = RequestValidator(TOKEN).compute_signature(PUBLIC_URL, _Multi())
    with (
        TestClient(app) as client,
        patch("app.fast_api_app.process_webhook_async") as process,
        patch("app.fast_api_app.resolve_twilio_sender") as resolve_sender,
    ):
        res = client.post(
            "/twilio/webhook",
            content=urlencode(items),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signature,
            },
        )
        assert res.status_code == 200
        process.assert_not_called()
        resolve_sender.assert_not_called()


def test_accepted_webhook_logs_omit_phone_numbers_and_body(webhook_env, caplog):
    phone = "whatsapp:+15557654321"
    other = "whatsapp:+15559870000"
    body = "UNIQUE-BODY-do-not-log"
    params = _params(Body=body, From=phone, To=other)
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(app) as client,
        patch("app.fast_api_app.process_webhook_async"),
    ):
        res = _post(client, params, _sign(PUBLIC_URL, params))
    assert res.status_code == 200
    logged = caplog.text
    assert body not in logged
    assert phone not in logged
    assert other not in logged
    assert "15557654321" not in logged
    assert "15559870000" not in logged


def test_rejected_signature_logs_no_secrets_or_phone_numbers(webhook_env, caplog):
    body = "UNIQUE-BODY-do-not-log"
    phone = "whatsapp:+15557654321"
    params = _params(Body=body, From=phone, To="whatsapp:+15559870000")
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        res = _post(client, params, "not-the-signature")
    assert res.status_code == 403
    logged = caplog.text
    assert body not in logged
    assert TOKEN not in logged
    assert phone not in logged
    assert "15557654321" not in logged
    assert "15559870000" not in logged
