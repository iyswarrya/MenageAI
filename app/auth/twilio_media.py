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

"""SEC-02B: fetch Twilio media without SSRF or unbounded reads.

Twilio's Help Center article 223183748 says a secured media fetch redirects to
a short-lived URL on the exact host ``mms.twiliocdn.com``. WhatsApp ``MediaUrl0``
itself stays on ``api.twilio.com``. This client follows at most two redirects,
and only to those two hosts. The legacy shared host ``s3-external-1.amazonaws.com``
is not followed. Basic credentials are sent only to ``api.twilio.com``.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import socket
import ssl
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

DEFAULT_MAX_MEDIA_BYTES = 10 * 1024 * 1024
_MAX_HEADER_BYTES = 8192
_CONNECT_TIMEOUT_SECONDS = 5.0
_READ_TIMEOUT_SECONDS = 15.0
_API_MEDIA_HOST = "api.twilio.com"
_CDN_MEDIA_HOST = "mms.twiliocdn.com"
_INITIAL_MEDIA_HOSTS = frozenset({_API_MEDIA_HOST})
_REDIRECT_MEDIA_HOSTS = frozenset({_API_MEDIA_HOST, _CDN_MEDIA_HOST})
_MAX_REDIRECTS = 2
_ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_JPEG_SOF = frozenset(
    [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF]
)

_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
        "fd00:ec2::/32",
    )
)

Resolver = Callable[[str, int], list[str]]
Opener = Callable[[str, int, str, float, float], socket.socket]


class MediaFetchError(Exception):
    """Safe failure. ``reason`` is a short code, not request or file contents."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedMediaUrl:
    host: str
    port: int
    target: str


@dataclass(frozen=True)
class MediaDownload:
    content_type: str
    data: bytes


def download_twilio_media(
    url: str,
    *,
    resolver: Resolver | None = None,
    opener: Opener | None = None,
) -> MediaDownload:
    """Download one approved Twilio media object over pinned TLS connections."""
    account_sid, auth_token = _twilio_media_credentials()
    resolve = resolver or _resolve_public_ips
    open_tls = opener or _open_pinned_tls
    connect_timeout = _timeout_setting(
        "TWILIO_MEDIA_CONNECT_TIMEOUT_SECONDS", _CONNECT_TIMEOUT_SECONDS, 30.0
    )
    read_timeout = _timeout_setting(
        "TWILIO_MEDIA_READ_TIMEOUT_SECONDS", _READ_TIMEOUT_SECONDS, 60.0
    )
    max_bytes = _max_media_bytes()

    current = url
    followed = 0
    while True:
        validated = validate_twilio_media_url(current, allow_cdn=followed > 0)
        ips = _require_public_ips(resolve(validated.host, validated.port))
        try:
            sock = open_tls(
                ips[0], validated.port, validated.host, connect_timeout, read_timeout
            )
        except MediaFetchError:
            raise
        except TimeoutError:
            raise MediaFetchError("timeout") from None
        except OSError:
            raise MediaFetchError("connection_error") from None

        try:
            send_credentials = validated.host == _API_MEDIA_HOST
            _send_get(
                sock,
                validated.host,
                validated.target,
                account_sid if send_credentials else None,
                auth_token if send_credentials else None,
            )
            status, headers, body = _read_http_response(sock, max_bytes)
        except MediaFetchError:
            raise
        except TimeoutError:
            raise MediaFetchError("timeout") from None
        except OSError:
            raise MediaFetchError("connection_error") from None
        finally:
            sock.close()

        if status in _REDIRECT_STATUSES:
            if followed >= _MAX_REDIRECTS:
                raise MediaFetchError("redirect_rejected")
            nxt = _redirect_target(current, headers.get("location", ""))
            try:
                validate_twilio_media_url(nxt, allow_cdn=True)
            except MediaFetchError:
                raise MediaFetchError("redirect_rejected") from None
            current = nxt
            followed += 1
            continue
        if status != 200:
            raise MediaFetchError("http_status")

        content_type = _content_type(headers.get("content-type"))
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise MediaFetchError("unsupported_type")
        if not _image_matches(content_type, body):
            raise MediaFetchError("invalid_image")
        return MediaDownload(content_type=content_type, data=body)


def validate_twilio_media_url(
    url: str, *, allow_cdn: bool = False
) -> ValidatedMediaUrl:
    """Accept only HTTPS on an exact Twilio media host and port 443."""
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        raise MediaFetchError("malformed_url")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise MediaFetchError("malformed_url")
    netloc = parsed.netloc
    if (
        not netloc
        or parsed.username is not None
        or parsed.password is not None
        or "@" in netloc
        or "%" in netloc
        or "\\" in netloc
    ):
        raise MediaFetchError("malformed_url")
    host = parsed.hostname
    allowed = _REDIRECT_MEDIA_HOSTS if allow_cdn else _INITIAL_MEDIA_HOSTS
    if not host or host not in allowed:
        raise MediaFetchError("unapproved_host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise MediaFetchError("unapproved_host")
    if parsed.port not in (None, 443):
        raise MediaFetchError("unexpected_port")
    if netloc.lower() not in {host, f"{host}:443"}:
        raise MediaFetchError("malformed_url")
    return ValidatedMediaUrl(host=host, port=443, target=_request_target(parsed))


def _request_target(parsed) -> str:
    path = parsed.path or "/"
    if not path.startswith("/") or "\\" in path:
        raise MediaFetchError("malformed_url")
    target = path if not parsed.query else f"{path}?{parsed.query}"
    if any(ch in target for ch in "\r\n\x00 "):
        raise MediaFetchError("malformed_url")
    try:
        target.encode("ascii")
    except UnicodeEncodeError:
        raise MediaFetchError("malformed_url") from None
    return target


def _redirect_target(current: str, location: str) -> str:
    """Resolve one Location value. The caller validates it before connecting."""
    if not isinstance(location, str):
        raise MediaFetchError("redirect_rejected")
    location = location.strip()
    if not location or any(ch in location for ch in "\r\n\x00"):
        raise MediaFetchError("redirect_rejected")
    return urljoin(current, location)


def _twilio_media_credentials() -> tuple[str, str]:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not account_sid or not auth_token:
        raise MediaFetchError("missing_credentials")
    if any(ch in account_sid + auth_token for ch in "\r\n\x00"):
        raise MediaFetchError("invalid_credentials")
    return account_sid, auth_token


def _require_public_ips(ips: list[str]) -> list[str]:
    if not ips:
        raise MediaFetchError("dns_failure")
    checked: list[str] = []
    for raw in ips:
        try:
            ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            raise MediaFetchError("unsafe_address") from None
        if not _is_public_ip(ip):
            raise MediaFetchError("unsafe_address")
        checked.append(str(ip))
    return checked


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public_ip(ip.ipv4_mapped)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return not any(ip in network for network in _BLOCKED_NETWORKS)


def _resolve_public_ips(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise MediaFetchError("dns_failure") from None
    ips: list[str] = []
    for info in infos:
        ips.append(info[4][0])
    return _require_public_ips(ips)


def _open_pinned_tls(
    ip: str, port: int, hostname: str, connect_timeout: float, read_timeout: float
) -> socket.socket:
    raw = socket.create_connection((ip, port), timeout=connect_timeout)
    try:
        raw.settimeout(read_timeout)
        tls = ssl.create_default_context().wrap_socket(raw, server_hostname=hostname)
        tls.settimeout(read_timeout)
        return tls
    except Exception:
        raw.close()
        raise


def _send_get(
    sock: socket.socket,
    host: str,
    target: str,
    account_sid: str | None,
    auth_token: str | None,
) -> None:
    lines = [f"GET {target} HTTP/1.1", f"Host: {host}"]
    if account_sid is not None and auth_token is not None:
        basic = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode("ascii")
        lines.append(f"Authorization: Basic {basic}")
    lines.extend(
        ["Accept: image/jpeg, image/png, image/webp", "Connection: close", "", ""]
    )
    sock.sendall("\r\n".join(lines).encode("ascii"))


def _read_http_response(sock: socket.socket, max_bytes: int) -> tuple[int, dict, bytes]:
    reader = _BufferedReader(sock)
    header_blob = reader.read_until(b"\r\n\r\n", _MAX_HEADER_BYTES)
    if header_blob is None:
        raise MediaFetchError("malformed_response")
    header_text, _, _ = header_blob.partition(b"\r\n\r\n")
    try:
        header_lines = header_text.decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        raise MediaFetchError("malformed_response") from None
    if not header_lines or len(header_lines[0].split(" ", 2)) < 2:
        raise MediaFetchError("malformed_response")
    try:
        status = int(header_lines[0].split(" ", 2)[1])
    except ValueError:
        raise MediaFetchError("malformed_response") from None
    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            raise MediaFetchError("malformed_response")
        headers[name.strip().lower()] = value.strip()

    if status in _REDIRECT_STATUSES or status != 200:
        return status, headers, b""

    transfer = headers.get("transfer-encoding", "").lower()
    if transfer and transfer != "chunked":
        raise MediaFetchError("unsupported_transfer")
    if "chunked" in transfer:
        body = _read_chunked(reader, max_bytes)
        return status, headers, body

    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError:
            raise MediaFetchError("malformed_response") from None
        if length < 0:
            raise MediaFetchError("malformed_response")
        if length > max_bytes:
            raise MediaFetchError("too_large")
        body = reader.read_exact(length)
        if len(body) != length:
            raise MediaFetchError("truncated_body")
        return status, headers, body

    body = reader.read_at_most(max_bytes)
    return status, headers, body


def _read_chunked(reader: _BufferedReader, max_bytes: int) -> bytes:
    chunks = bytearray()
    while True:
        line = reader.read_line(64)
        if line is None:
            raise MediaFetchError("malformed_response")
        size_token = line.split(b";", 1)[0]
        try:
            size = int(size_token, 16)
        except ValueError:
            raise MediaFetchError("malformed_response") from None
        if size < 0:
            raise MediaFetchError("malformed_response")
        if size == 0:
            return bytes(chunks)
        if len(chunks) + size > max_bytes:
            raise MediaFetchError("too_large")
        data = reader.read_exact(size)
        if len(data) != size:
            raise MediaFetchError("truncated_body")
        crlf = reader.read_exact(2)
        if crlf != b"\r\n":
            raise MediaFetchError("malformed_response")
        chunks += data


class _BufferedReader:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def read_until(self, marker: bytes, limit: int) -> bytes | None:
        while marker not in self._buf:
            if len(self._buf) > limit:
                raise MediaFetchError("malformed_response")
            if not self._pull():
                return None
        end = self._buf.index(marker) + len(marker)
        if end > limit:
            raise MediaFetchError("malformed_response")
        found = bytes(self._buf[:end])
        del self._buf[:end]
        return found

    def read_line(self, limit: int) -> bytes | None:
        blob = self.read_until(b"\r\n", limit)
        if blob is None:
            return None
        return blob[:-2]

    def read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            if not self._pull():
                break
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data

    def read_at_most(self, max_bytes: int) -> bytes:
        while len(self._buf) <= max_bytes:
            if not self._pull():
                return bytes(self._buf)
        raise MediaFetchError("too_large")

    def _pull(self) -> bytes:
        try:
            chunk = self._sock.recv(65536)
        except TimeoutError:
            raise MediaFetchError("timeout") from None
        except OSError:
            raise MediaFetchError("connection_error") from None
        if chunk:
            self._buf += chunk
        return chunk


def _content_type(value: str | None) -> str:
    if not value:
        raise MediaFetchError("unsupported_type")
    return value.split(";", 1)[0].strip().lower()


def _image_matches(content_type: str, data: bytes) -> bool:
    checkers = {
        "image/jpeg": _is_jpeg,
        "image/png": _is_png,
        "image/webp": _is_webp,
    }
    checker = checkers.get(content_type)
    return checker is not None and checker(data)


def _is_jpeg(data: bytes) -> bool:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return False
    index = 2
    saw_sof = False
    while index < len(data):
        if data[index] != 0xFF:
            return False
        index += 1
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return False
        marker = data[index]
        index += 1
        if marker == 0xD9:
            return saw_sof
        if marker in (0x00, 0xD8):
            return False
        if marker == 0xDA:
            if not saw_sof or index + 2 > len(data):
                return False
            segment_length = int.from_bytes(data[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(data):
                return False
            return _jpeg_scan_ends(data, index + segment_length)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            return False
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            return False
        if marker in _JPEG_SOF:
            saw_sof = True
        index += segment_length
    return False


def _jpeg_scan_ends(data: bytes, index: int) -> bool:
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        next_index = index + 1
        if next_index >= len(data):
            return False
        marker = data[next_index]
        if marker == 0x00:
            index = next_index + 1
            continue
        if marker == 0xFF:
            index = next_index
            continue
        return marker == 0xD9
    return False


def _is_png(data: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    pos = 8
    saw_ihdr = False
    first_chunk = True
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        tag = data[pos + 4 : pos + 8]
        pos += 8
        if length < 0 or pos + length + 4 > len(data):
            return False
        payload = data[pos : pos + length]
        crc = int.from_bytes(data[pos + length : pos + length + 4], "big")
        if crc != (zlib.crc32(tag + payload) & 0xFFFFFFFF):
            return False
        if first_chunk and tag != b"IHDR":
            return False
        first_chunk = False
        if tag == b"IHDR":
            if length != 13 or saw_ihdr:
                return False
            saw_ihdr = True
        if tag == b"IEND":
            return length == 0 and saw_ihdr and pos + length + 4 == len(data)
        pos += length + 4
    return False


def _is_webp(data: bytes) -> bool:
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    riff_size = int.from_bytes(data[4:8], "little")
    if riff_size != len(data) - 8:
        return False
    pos = 12
    saw_image = False
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        pos += 8
        padded = size + (size & 1)
        if size < 0 or pos + padded > len(data):
            return False
        if tag in (b"VP8 ", b"VP8L", b"VP8X") and size > 0:
            saw_image = True
        pos += padded
    return saw_image and pos == len(data)


def _max_media_bytes() -> int:
    raw = os.environ.get("TWILIO_MEDIA_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_MEDIA_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise MediaFetchError("invalid_media_limit") from None
    if value <= 0 or value > DEFAULT_MAX_MEDIA_BYTES:
        raise MediaFetchError("invalid_media_limit")
    return value


def _timeout_setting(name: str, default: float, upper: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise MediaFetchError("invalid_media_timeout") from None
    if value <= 0 or value > upper:
        raise MediaFetchError("invalid_media_timeout")
    return value
