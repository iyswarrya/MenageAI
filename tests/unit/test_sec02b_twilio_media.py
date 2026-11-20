"""SEC-02B: Twilio media URL checks, bounded downloads, and receipt broadcast."""

from __future__ import annotations

import base64
import logging
import struct
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth.twilio_media import MediaFetchError, download_twilio_media
from app.fast_api_app import app, process_webhook_async, twilio_send_message
from app.interfaces import (
    ReceiptData,
    ReceiptItem,
    SavedReceiptResult,
    registry,
)

_READ_TIMEOUT = object()

MEDIA_URL = "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM1/Media/ME1"
ACCOUNT_SID = "ACtest-account"
AUTH_TOKEN = "token-do-not-leak"


class ScriptedSocket:
    def __init__(self, payload: bytes, *, recv_error: BaseException | None = None):
        self._payload = payload
        self.sent = b""
        self.closed = False
        self.recv_error = recv_error

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        if self.recv_error is not None:
            raise self.recv_error
        if not self._payload:
            return b""
        chunk = self._payload[:_size]
        self._payload = self._payload[len(chunk) :]
        return chunk

    def close(self) -> None:
        self.closed = True

    def settimeout(self, _value: float) -> None:
        return None


def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def _jpeg() -> bytes:
    sof_payload = bytes([8, 0, 1, 0, 1, 1, 1, 0x11, 0])
    sof = b"\xff\xc0" + struct.pack(">H", 2 + len(sof_payload)) + sof_payload
    sos_payload = bytes([1, 1, 0, 0, 63, 0])
    sos = b"\xff\xda" + struct.pack(">H", 2 + len(sos_payload)) + sos_payload
    return b"\xff\xd8" + sof + sos + b"\x00\xff\xd9"


def _webp() -> bytes:
    payload = b"VP8 " + struct.pack("<I", 4) + b"test"
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WEBP" + payload


def _http(status: int, headers: dict[str, str], body: bytes = b"") -> bytes:
    lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return f"HTTP/1.1 {status} TEST\r\n{lines}\r\n".encode() + body


def _image_response(
    content_type: str, body: bytes, *, length: int | None = None
) -> bytes:
    headers = {"Content-Type": content_type}
    if length is not None:
        headers["Content-Length"] = str(length)
    elif length is False:
        pass
    else:
        headers["Content-Length"] = str(len(body))
    return _http(200, headers, body)


@pytest.fixture
def media_env(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)


def _fetch(
    url: str,
    *,
    ips: list[str] | None = None,
    payload: bytes = b"",
    payloads: list[bytes] | None = None,
    host_ips: dict[str, list[str]] | None = None,
    error=None,
):
    calls: dict = {"open": [], "socks": [], "resolves": []}
    queue = list(payloads) if payloads is not None else [payload]

    def resolver(host: str, port: int) -> list[str]:
        calls["resolve"] = (host, port)
        calls["resolves"].append((host, port))
        if host_ips is not None and host in host_ips:
            return host_ips[host]
        return ["8.8.8.8"] if ips is None else ips

    def opener(ip, port, host, _connect_timeout, _read_timeout):
        calls["open"].append((ip, port, host))
        if error is _READ_TIMEOUT:
            failed = ScriptedSocket(b"", recv_error=TimeoutError("slow"))
            calls["socks"].append(failed)
            return failed
        if error is not None:
            raise error
        data = queue.pop(0) if queue else b""
        sock = ScriptedSocket(data)
        calls["socks"].append(sock)
        return sock

    try:
        result = download_twilio_media(url, resolver=resolver, opener=opener)
    except MediaFetchError as exc:
        result = exc
    sock = calls["socks"][-1] if calls["socks"] else ScriptedSocket(b"")
    return result, calls, sock


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("image/png", _png()), ("image/jpeg", _jpeg()), ("image/webp", _webp())],
)
def test_approved_twilio_media_url(media_env, content_type, body):
    result, calls, sock = _fetch(MEDIA_URL, payload=_image_response(content_type, body))
    assert getattr(result, "content_type", None) == content_type
    assert result.data == body
    assert calls["open"] == [("8.8.8.8", 443, "api.twilio.com")]
    assert b"Host: api.twilio.com\r\n" in sock.sent
    assert b"Authorization: Basic " in sock.sent
    assert b"8.8.8.8" not in sock.sent


@pytest.mark.parametrize(
    "url",
    [
        "https://mms.twiliocdn.com/ACtest/file",
        "https://api.twilio.com.evil.example/media",
        "https://evilapi.twilio.com/media",
        "https://api.twilio.co/media",
        "https://not-twilio.example/api.twilio.com",
        "http://api.twilio.com/media",
        "https://user:secret@api.twilio.com/media",
        "https://api.twilio.com:8443/media",
        "https://",
        "not a url",
        "https://api.twilio.com/media\r\nHost: evil.example",
    ],
)
def test_rejects_unapproved_or_malformed_urls_without_connecting(media_env, url):
    result, calls, sock = _fetch(url, payload=_image_response("image/png", _png()))
    assert isinstance(result, MediaFetchError)
    assert result.reason in {"unapproved_host", "malformed_url", "unexpected_port"}
    assert calls["open"] == []
    assert sock.sent == b""
    assert AUTH_TOKEN.encode() not in sock.sent


@pytest.mark.parametrize(
    "ips",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["192.168.1.10"],
        ["172.16.0.4"],
        ["169.254.169.254"],
        ["0.0.0.0"],
        ["100.64.0.1"],
        ["::1"],
        ["fd00:ec2::254"],
        ["8.8.8.8", "127.0.0.1"],
        ["::ffff:169.254.169.254"],
    ],
)
def test_rejects_private_and_mixed_dns_answers(media_env, ips):
    result, calls, _sock = _fetch(
        MEDIA_URL, ips=ips, payload=_image_response("image/png", _png())
    )
    assert isinstance(result, MediaFetchError)
    assert result.reason == "unsafe_address"
    assert calls["open"] == []


def test_explicit_https_port_is_allowed(media_env):
    body = _png()
    result, calls, _sock = _fetch(
        "https://API.Twilio.COM:443/media",
        payload=_image_response("image/png", body),
    )
    assert result.data == body
    assert calls["resolve"] == ("api.twilio.com", 443)


def test_redirect_is_not_followed(media_env):
    payload = _http(
        307,
        {"Location": "http://169.254.169.254/latest/meta-data", "Content-Length": "0"},
    )
    result, calls, _sock = _fetch(MEDIA_URL, payload=payload)
    assert isinstance(result, MediaFetchError)
    assert result.reason == "redirect_rejected"
    assert len(calls["open"]) == 1
    assert calls["open"][0][0] == "8.8.8.8"
    assert calls["resolves"] == [("api.twilio.com", 443)]


def test_follows_documented_twilio_cdn_without_forwarding_credentials(media_env):
    body = _png()
    cdn = "https://mms.twiliocdn.com/ACtest/ME123"
    result, calls, _sock = _fetch(
        MEDIA_URL,
        payloads=[
            _http(307, {"Location": cdn, "Content-Length": "0"}),
            _image_response("image/png", body),
        ],
    )
    assert getattr(result, "data", None) == body
    assert [host for _ip, _port, host in calls["open"]] == [
        "api.twilio.com",
        "mms.twiliocdn.com",
    ]
    assert b"Authorization: Basic " in calls["socks"][0].sent
    assert (
        base64.b64decode(
            calls["socks"][0]
            .sent.split(b"Authorization: Basic ", 1)[1]
            .split(b"\r\n", 1)[0]
        )
        == f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()
    )
    assert b"Authorization:" not in calls["socks"][1].sent
    assert AUTH_TOKEN.encode() not in calls["socks"][1].sent
    assert b"Host: mms.twiliocdn.com\r\n" in calls["socks"][1].sent


@pytest.mark.parametrize(
    "location",
    [
        "https://s3-external-1.amazonaws.com/media.twiliocdn.com/ACtest/file",
        "https://mms.twiliocdn.com.evil.example/file",
        "https://evil.mms.twiliocdn.com/file",
        "http://mms.twiliocdn.com/file",
        "https://user:secret@mms.twiliocdn.com/file",
        "https://mms.twiliocdn.com:8443/file",
        "https://media.twiliocdn.com/file",
    ],
)
def test_unapproved_redirect_does_not_connect(media_env, location):
    result, calls, sock = _fetch(
        MEDIA_URL,
        payload=_http(307, {"Location": location, "Content-Length": "0"}),
    )
    assert isinstance(result, MediaFetchError)
    assert result.reason == "redirect_rejected"
    assert calls["open"] == [("8.8.8.8", 443, "api.twilio.com")]
    assert calls["resolves"] == [("api.twilio.com", 443)]
    assert location.encode().split(b"/")[2] not in sock.sent


def test_cdn_private_address_is_not_contacted(media_env):
    result, calls, _sock = _fetch(
        MEDIA_URL,
        payloads=[
            _http(
                307,
                {
                    "Location": "https://mms.twiliocdn.com/ACtest/ME123",
                    "Content-Length": "0",
                },
            )
        ],
        host_ips={"mms.twiliocdn.com": ["169.254.169.254"]},
    )
    assert isinstance(result, MediaFetchError)
    assert result.reason == "unsafe_address"
    assert calls["open"] == [("8.8.8.8", 443, "api.twilio.com")]


def test_two_redirects_keep_credentials_on_the_api_host(media_env):
    body = _jpeg()
    result, calls, _sock = _fetch(
        MEDIA_URL,
        payloads=[
            _http(
                307,
                {
                    "Location": "https://api.twilio.com/2010-04-01/Accounts/AC/Media/ME",
                    "Content-Length": "0",
                },
            ),
            _http(
                302,
                {
                    "Location": "https://mms.twiliocdn.com/ACtest/final",
                    "Content-Length": "0",
                },
            ),
            _image_response("image/jpeg", body),
        ],
    )
    assert getattr(result, "data", None) == body
    assert [host for _ip, _port, host in calls["open"]] == [
        "api.twilio.com",
        "api.twilio.com",
        "mms.twiliocdn.com",
    ]
    assert b"Authorization: Basic " in calls["socks"][0].sent
    assert b"Authorization: Basic " in calls["socks"][1].sent
    assert b"Authorization:" not in calls["socks"][2].sent


def test_third_redirect_is_not_followed(media_env):
    result, calls, _sock = _fetch(
        MEDIA_URL,
        payloads=[
            _http(
                307,
                {"Location": "https://mms.twiliocdn.com/a", "Content-Length": "0"},
            ),
            _http(
                307,
                {"Location": "https://mms.twiliocdn.com/b", "Content-Length": "0"},
            ),
            _http(
                307,
                {"Location": "https://mms.twiliocdn.com/c", "Content-Length": "0"},
            ),
        ],
    )
    assert isinstance(result, MediaFetchError)
    assert result.reason == "redirect_rejected"
    assert len(calls["open"]) == 3
    assert all(
        host == "mms.twiliocdn.com" or host == "api.twilio.com"
        for *_x, host in calls["open"]
    )


def test_missing_credentials_do_not_connect(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    result, calls, sock = _fetch(
        MEDIA_URL, payload=_image_response("image/png", _png())
    )
    assert isinstance(result, MediaFetchError)
    assert result.reason == "missing_credentials"
    assert "resolve" not in calls
    assert calls["open"] == []
    assert AUTH_TOKEN.encode() not in sock.sent


def test_oversized_content_length_is_rejected_before_the_body(media_env, monkeypatch):
    monkeypatch.setenv("TWILIO_MEDIA_MAX_BYTES", "32")
    payload = _http(
        200,
        {"Content-Type": "image/png", "Content-Length": "1000"},
        b"x" * 1000,
    )
    result, _calls, sock = _fetch(MEDIA_URL, payload=payload)
    assert isinstance(result, MediaFetchError)
    assert result.reason == "too_large"
    assert sock.closed is True


def test_oversized_stream_without_content_length(media_env, monkeypatch):
    monkeypatch.setenv("TWILIO_MEDIA_MAX_BYTES", "32")
    payload = _http(200, {"Content-Type": "image/png"}, b"x" * 100)
    result, _calls, _sock = _fetch(MEDIA_URL, payload=payload)
    assert isinstance(result, MediaFetchError)
    assert result.reason == "too_large"


def test_unsupported_mime_and_invalid_image(media_env):
    gif = _image_response("image/gif", b"GIF89a")
    result, _calls, _sock = _fetch(MEDIA_URL, payload=gif)
    assert isinstance(result, MediaFetchError)
    assert result.reason == "unsupported_type"

    corrupt = _image_response("image/jpeg", b"\xff\xd8\xff\xd9not-an-image")
    result, _calls, _sock = _fetch(MEDIA_URL, payload=corrupt)
    assert isinstance(result, MediaFetchError)
    assert result.reason == "invalid_image"

    mismatched = _image_response("image/jpeg", _png())
    result, _calls, _sock = _fetch(MEDIA_URL, payload=mismatched)
    assert isinstance(result, MediaFetchError)
    assert result.reason == "invalid_image"


def test_outbound_logs_omit_phone_numbers_and_message_body(caplog):
    phone = "whatsapp:+15557654321"
    sender = "whatsapp:+15550001111"
    body = "UNIQUE-RECEIPT-BODY-do-not-log"
    response = MagicMock(status_code=500, text=f"{phone} {sender} {body}")
    with caplog.at_level(logging.DEBUG, logger="twilio_integration"):
        with patch("requests.post", return_value=response):
            twilio_send_message("ACtest", "token-do-not-leak", sender, phone, body)
        with patch("requests.post", side_effect=RuntimeError(f"{phone} {body}")):
            twilio_send_message("ACtest", "token-do-not-leak", sender, phone, body)
    logged = caplog.text
    assert phone not in logged
    assert sender not in logged
    assert "15557654321" not in logged
    assert "15550001111" not in logged
    assert body not in logged
    assert "token-do-not-leak" not in logged


def test_timeout_and_connection_error(media_env):
    result, calls, _sock = _fetch(MEDIA_URL, error=TimeoutError())
    assert isinstance(result, MediaFetchError)
    assert result.reason == "timeout"
    assert len(calls["open"]) == 1

    result, calls, _sock = _fetch(MEDIA_URL, error=ConnectionError("refused"))
    assert result.reason == "connection_error"

    result, _calls, _sock = _fetch(MEDIA_URL, error=_READ_TIMEOUT)
    assert isinstance(result, MediaFetchError)
    assert result.reason == "timeout"


def test_memory_engine_reports_only_a_successful_save(monkeypatch):
    from app.agent import memory_engine

    saved: list[SavedReceiptResult] = []

    class Repo:
        def save_receipt(self, receipt, household_id="default", invocation_id=None):
            result = saved[-1]
            return result

        def find_duplicates(self, items, store, date, household_id):
            return []

    receipt = ReceiptData(
        store="Safeway",
        date="2026-07-02",
        items=[ReceiptItem(name="Coffee", price=9.99)],
        prices=[9.99],
        total=9.99,
    )
    original = registry.memory_repo
    registry.memory_repo = Repo()
    monkeypatch.setattr("app.agent.db.check_duplicate_receipt", lambda *a, **k: False)
    try:
        saved.append(SavedReceiptResult(receipt_id=15, success=True))
        assert memory_engine(receipt).persisted_receipt_id == 15

        saved.append(SavedReceiptResult(receipt_id=-1, success=False))
        assert memory_engine(receipt).persisted_receipt_id is None

        monkeypatch.setattr(
            "app.agent.db.check_duplicate_receipt", lambda *a, **k: True
        )
        saved.append(SavedReceiptResult(receipt_id=99, success=True))
        duplicate = memory_engine(receipt)
        assert duplicate.persisted_receipt_id is None
        assert duplicate.is_duplicate_receipt is True
    finally:
        registry.memory_repo = original


@pytest.fixture
def household(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "sec02b.db"))
    db.init_db()
    monkeypatch.setenv(
        "FAMILY_WHATSAPP_NUMBERS",
        "whatsapp:+15551111111,whatsapp:+15552222222",
    )
    from app.auth import provision_twilio_household_from_env

    provision_twilio_household_from_env()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)


class _Node:
    def __init__(self, name: str):
        self.name = name


class _Event:
    def __init__(self, name: str):
        self.node_info = _Node(name)
        self.invocation_id = ""
        self.output = None
        self.content = None
        self._bind_run = None


def _runner(client, events):
    async def fake_events(**kwargs):
        run_id = kwargs.get("invocation_id")
        for event in events:
            if event._bind_run is not None:
                event._bind_run(run_id)
            yield event

    runner = MagicMock()
    runner.run_async = fake_events
    session = MagicMock()
    session.id = "sess"
    session.state = {"persisted_receipt_id": 99, "household_id": "other-household"}
    runner.session_service.get_session = AsyncMock(return_value=session)
    runner.session_service.create_session = AsyncMock(return_value=session)
    client.app.state.runner = runner
    client.app.state.agent_app_name = "app"
    return runner


def _response_event(text: str):
    event = _Event("response_agent")

    class _Part:
        def __init__(self):
            self.text = text

    class _Content:
        def __init__(self):
            self.parts = [_Part()]

    event.content = _Content()
    return event


def _saved_event(
    receipt_id: int | None, *, invocation_id: str = "", household_id: str = ""
):
    event = _Event("memory_engine")
    event.invocation_id = invocation_id
    event.output = {
        "persisted_receipt_id": receipt_id,
        "persisted_household_id": household_id,
        "persisted_invocation_id": invocation_id,
    }
    return event


def _insert_receipt(household_id: str, invocation_id: str) -> int:
    return db.save_receipt_and_items(
        "Safeway",
        "2026-07-02",
        9.99,
        [("Coffee", 9.99)],
        household_id=household_id,
        invocation_id=invocation_id,
    )


@pytest.mark.asyncio
async def test_failed_media_does_not_broadcast_or_run_agent(household):
    with TestClient(app) as client:
        runner = _runner(client, [])
        runner.run_async = AsyncMock()
        sent: list[str] = []
        with (
            patch(
                "app.fast_api_app.download_twilio_media",
                side_effect=MediaFetchError("unsafe_address"),
            ),
            patch(
                "app.fast_api_app.twilio_send_message",
                side_effect=lambda *_a, **_k: sent.append(_a[3]),
            ),
        ):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body=None,
                MediaUrl0="https://169.254.169.254/latest/meta-data",
            )
        runner.run_async.assert_not_called()
        assert sent == ["whatsapp:+15551111111"]


@pytest.mark.asyncio
async def test_receipt_text_without_persistence_stays_private(household):
    with TestClient(app) as client:
        _runner(client, [_response_event("Logged Safeway coffee $9.99")])
        sent: list[str] = []
        with patch(
            "app.fast_api_app.twilio_send_message",
            side_effect=lambda *_a, **_k: sent.append(_a[3]),
        ):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="Safeway coffee 9.99",
                MediaUrl0=None,
            )
        assert sent == ["whatsapp:+15551111111"]


@pytest.mark.asyncio
async def test_failed_media_with_text_does_not_broadcast(household):
    with TestClient(app) as client:
        _runner(
            client,
            [_saved_event(None), _response_event("Logged Safeway coffee $9.99")],
        )
        sent: list[str] = []
        with (
            patch(
                "app.fast_api_app.download_twilio_media",
                side_effect=MediaFetchError("invalid_image"),
            ),
            patch(
                "app.fast_api_app.twilio_send_message",
                side_effect=lambda *_a, **_k: sent.append(_a[3]),
            ),
        ):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="Safeway coffee 9.99",
                MediaUrl0=MEDIA_URL,
            )
        assert sent == ["whatsapp:+15551111111"]


@pytest.mark.asyncio
async def test_persisted_receipt_still_broadcasts(household):
    from app.auth import DEFAULT_TWILIO_HOUSEHOLD_ID

    saved = _Event("memory_engine")

    def bind(run_id: str) -> None:
        receipt_id = _insert_receipt(DEFAULT_TWILIO_HOUSEHOLD_ID, run_id)
        saved.invocation_id = run_id
        saved.output = {
            "persisted_receipt_id": receipt_id,
            "persisted_household_id": DEFAULT_TWILIO_HOUSEHOLD_ID,
            "persisted_invocation_id": run_id,
        }

    saved._bind_run = bind
    with TestClient(app) as client:
        _runner(client, [saved, _response_event("Logged Safeway coffee $9.99")])
        sent: list[str] = []
        with patch(
            "app.fast_api_app.twilio_send_message",
            side_effect=lambda *_a, **_k: sent.append(_a[3]),
        ):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="Safeway coffee 9.99",
                MediaUrl0=None,
            )
        assert sent == ["whatsapp:+15551111111", "whatsapp:+15552222222"]


@pytest.mark.asyncio
async def test_stale_and_cross_household_events_stay_private(household):
    from app.auth import DEFAULT_TWILIO_HOUSEHOLD_ID

    other_household = "household-b"
    db.create_household(other_household, name="Other")
    stale_id = _insert_receipt(DEFAULT_TWILIO_HOUSEHOLD_ID, "old-run")
    foreign_id = _insert_receipt(other_household, "current-looking")

    stale = _saved_event(
        stale_id,
        invocation_id="old-run",
        household_id=DEFAULT_TWILIO_HOUSEHOLD_ID,
    )
    foreign = _Event("memory_engine")

    def bind(run_id: str) -> None:
        foreign.invocation_id = run_id
        foreign.output = {
            "persisted_receipt_id": foreign_id,
            "persisted_household_id": DEFAULT_TWILIO_HOUSEHOLD_ID,
            "persisted_invocation_id": run_id,
        }

    foreign._bind_run = bind
    with TestClient(app) as client:
        _runner(
            client,
            [stale, foreign, _response_event("Logged Safeway coffee $9.99")],
        )
        sent: list[str] = []
        with patch(
            "app.fast_api_app.twilio_send_message",
            side_effect=lambda *_a, **_k: sent.append(_a[3]),
        ):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="Safeway coffee 9.99",
                MediaUrl0=None,
            )
        assert sent == ["whatsapp:+15551111111"]


@pytest.mark.asyncio
async def test_duplicate_event_does_not_broadcast_previous_receipt(household):
    from app.auth import DEFAULT_TWILIO_HOUSEHOLD_ID

    previous_id = _insert_receipt(DEFAULT_TWILIO_HOUSEHOLD_ID, "previous-run")
    duplicate = _Event("memory_engine")

    def bind(run_id: str) -> None:
        duplicate.invocation_id = run_id
        duplicate.output = {
            "persisted_receipt_id": previous_id,
            "persisted_household_id": DEFAULT_TWILIO_HOUSEHOLD_ID,
            "persisted_invocation_id": run_id,
            "is_duplicate_receipt": True,
        }

    duplicate._bind_run = bind
    with TestClient(app) as client:
        _runner(client, [duplicate, _response_event("Already logged that receipt")])
        sent: list[str] = []
        with patch(
            "app.fast_api_app.twilio_send_message",
            side_effect=lambda *_a, **_k: sent.append(_a[3]),
        ):
            await process_webhook_async(
                From="whatsapp:+15551111111",
                To="whatsapp:+15550000000",
                Body="Safeway coffee 9.99",
                MediaUrl0=None,
            )
        assert sent == ["whatsapp:+15551111111"]
