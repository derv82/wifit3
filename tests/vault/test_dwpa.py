import base64
import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wifit3.vault.tools.dwpa_api import (DwpaError, DwpaNetwork, is_valid_key, network_hash,
                                         network_leaked, parse_potfile, potfile_endpoint,
                                         query_endpoint, retrieve_cracked, submit_endpoint,
                                         upload_capture)

_KEY = "0123456789abcdef0123456789abcdef"

POTFILE = (
    "aabbccddeeff:112233445566:TestAP-DWPA:88888888\n"
    "aabbccddeeff:112233445566:TestAP-DWPA:88888888\n"
    "8e6a8da6db51:001122334455:E36W126F-Guest1:pass:with:colons\n"
    "nothex:112233445566:Broken:12345678\n"
    "aabbccddeeff:112233445566:TooShort:short\n"
)


@dataclass(frozen=True)
class SentRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup: urllib capitalises header names on the wire."""
        return next((value for key, value in self.headers.items() if key.lower() == name.lower()),
                    None)


class MockDwpaService:
    """A local stand-in for a DWPA instance: answers every request with one scripted reply
    and records what it was sent, so the tests never leave the machine."""

    def __init__(self) -> None:
        self.requests: list[SentRequest] = []
        self.status = 200
        self.body = b""
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def answer(self, body: bytes, status: int = 200) -> None:
        self.body, self.status = body, status

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def _handler(service: MockDwpaService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond()

        def do_POST(self) -> None:
            self._respond()

        def _respond(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            service.requests.append(SentRequest(self.command, self.path, dict(self.headers),
                                                self.rfile.read(length) if length else b""))
            self.send_response(service.status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(service.body)))
            self.end_headers()
            self.wfile.write(service.body)

        def log_message(self, *args) -> None:
            pass

    return Handler


@pytest.fixture
def dwpa_service():
    service = MockDwpaService()
    yield service
    service.close()


@pytest.fixture
def capture(tmp_path):
    path = tmp_path / "handshake.pcap"
    path.write_bytes(b"\xd4\xc3\xb2\xa1capture-bytes")
    return path


def test_endpoint_appends_the_submit_query_to_a_bare_host():
    assert submit_endpoint("dwpa.example") == "http://dwpa.example/?submit="


def test_endpoint_keeps_the_scheme_and_drops_a_trailing_slash():
    assert submit_endpoint("https://dwpa.example:8080/") == "https://dwpa.example:8080/?submit="


def test_the_query_endpoint_is_bmacssid(dwpa_service):
    assert query_endpoint("dwpa.example") == "http://dwpa.example/bmacssid"
    assert query_endpoint(dwpa_service.url) == f"{dwpa_service.url}/bmacssid"


def test_the_potfile_endpoint_is_the_api_download(dwpa_service):
    assert potfile_endpoint(dwpa_service.url) == f"{dwpa_service.url}/?api&dl=1"


@pytest.mark.parametrize("key", [_KEY, "ffffffffffffffffffffffffffffffff"])
def test_a_32_character_lowercase_hex_key_is_valid(key):
    assert is_valid_key(key)


@pytest.mark.parametrize("key", ["", "0123456789ABCDEF0123456789ABCDEF", "0123", "0" * 31,
                                 "g" * 32, " " + _KEY])
def test_anything_else_is_not_a_key(key):
    assert not is_valid_key(key)


def test_upload_posts_the_capture_as_multipart_with_the_key_cookie(capture, dwpa_service):
    dwpa_service.answer(b"<pre>1 handshakes</pre>")

    answer = upload_capture(dwpa_service.url, _KEY, str(capture))

    assert "1 handshakes" in answer
    request = dwpa_service.requests[0]
    assert request.method == "POST"
    assert request.path == "/?submit="
    assert request.header("Cookie") == f"key={_KEY}"
    content_type = request.header("Content-Type")
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]
    assert request.body.startswith(f"--{boundary}\r\n".encode())
    assert b'name="webfile"; filename="handshake.pcap"' in request.body
    assert capture.read_bytes() in request.body


def test_a_server_that_rejects_the_file_raises_its_message(capture, dwpa_service):
    dwpa_service.answer(b"<pre>Not a valid capture file. We support pcap and pcapng.</pre>")

    with pytest.raises(DwpaError, match="Not a valid capture file."):
        upload_capture(dwpa_service.url, _KEY, str(capture))


def test_an_http_error_raises_with_the_status(capture, dwpa_service):
    dwpa_service.answer(b"", status=500)

    with pytest.raises(DwpaError, match="HTTP 500"):
        upload_capture(dwpa_service.url, _KEY, str(capture))


def test_an_unreachable_server_raises(capture):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]

    with pytest.raises(DwpaError, match="Cannot reach"):
        upload_capture(f"http://127.0.0.1:{port}", _KEY, str(capture))


def test_the_network_hash_matches_the_k_anonymity_example():
    assert network_hash("1c7ee5e2f2d0", "dlink") == "1fe143ebc72790738a4346dc2a4e26ea92ed78a5"
    assert network_hash("1C:7E:E5:E2:F2:D0", "dlink") == network_hash("1c7ee5e2f2d0", "dlink")


def test_a_matching_suffix_in_the_answer_means_the_network_leaked(dwpa_service):
    dwpa_service.answer(b'{"1fe1":["cbc8ff3d","92ed78a5"]}')

    assert network_leaked(dwpa_service.url, "1c7ee5e2f2d0", "dlink")
    request = dwpa_service.requests[0]
    assert (request.method, request.path) == ("POST", "/bmacssid")
    assert json.loads(request.body) == ["1fe1"]
    assert request.header("Cookie") is None


def test_an_answer_without_the_suffix_is_a_miss(dwpa_service):
    dwpa_service.answer(b'{"1fe1":["78852df0"]}')

    assert not network_leaked(dwpa_service.url, "1c7ee5e2f2d0", "dlink")


def test_the_potfile_lines_become_networks_and_skip_unusable_rows():
    assert parse_potfile(POTFILE) == [
        DwpaNetwork("aa:bb:cc:dd:ee:ff", "TestAP-DWPA", "88888888"),
        DwpaNetwork("8e:6a:8d:a6:db:51", "E36W126F-Guest1", "pass:with:colons"),
    ]


def test_retrieve_downloads_and_base64_decodes_the_potfile(dwpa_service):
    dwpa_service.answer(base64.b64encode(POTFILE.encode("utf-8")))

    assert retrieve_cracked(dwpa_service.url, _KEY) == [
        DwpaNetwork("aa:bb:cc:dd:ee:ff", "TestAP-DWPA", "88888888"),
        DwpaNetwork("8e:6a:8d:a6:db:51", "E36W126F-Guest1", "pass:with:colons"),
    ]
    request = dwpa_service.requests[0]
    assert (request.method, request.path) == ("GET", "/?api&dl=1")
    assert request.header("Cookie") == f"key={_KEY}"


def test_an_unwrapped_base64_potfile_still_decodes(dwpa_service):
    encoded = base64.encodebytes(POTFILE.encode("utf-8"))       # 76-column, newline-wrapped
    dwpa_service.answer(encoded)

    assert retrieve_cracked(dwpa_service.url, _KEY)[0].key == "88888888"


def test_a_plain_text_potfile_like_wpa_sec_serves_it_is_read_too(dwpa_service):
    dwpa_service.answer(POTFILE.encode("utf-8"))

    assert retrieve_cracked(dwpa_service.url, _KEY) == [
        DwpaNetwork("aa:bb:cc:dd:ee:ff", "TestAP-DWPA", "88888888"),
        DwpaNetwork("8e:6a:8d:a6:db:51", "E36W126F-Guest1", "pass:with:colons"),
    ]


def test_an_html_answer_raises_instead_of_looking_empty(dwpa_service):
    dwpa_service.answer(b"<!DOCTYPE html><html><body><form>Key</form></body></html>")

    with pytest.raises(DwpaError, match="did not send a potfile"):
        retrieve_cracked(dwpa_service.url, _KEY)


def test_an_empty_potfile_has_no_networks(dwpa_service):
    dwpa_service.answer(base64.b64encode(b""))

    assert retrieve_cracked(dwpa_service.url, _KEY) == []
