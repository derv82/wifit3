import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wifit3.vault.tools.dwpa_api import (DwpaError, PotfileEntry, is_valid_user_key,
                                         network_hash, network_leaked, parse_potfile,
                                         potfile_endpoint, query_endpoint, retrieve_cracked,
                                         split_on_known_ssid, submit_endpoint, upload_capture)

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
        self.location: str | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def answer(self, body: bytes, status: int = 200, location: str | None = None) -> None:
        self.body, self.status, self.location = body, status, location

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
            if service.location is not None:
                self.send_header("Location", service.location)
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


def test_endpoint_appends_the_submit_query_to_a_bare_host_over_https():
    assert submit_endpoint("dwpa.example") == "https://dwpa.example/?submit="


def test_endpoint_keeps_the_scheme_and_drops_a_trailing_slash():
    assert submit_endpoint("https://dwpa.example:8080/") == "https://dwpa.example:8080/?submit="
    assert submit_endpoint("http://192.168.1.10") == "http://192.168.1.10/?submit="


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://dwpa.example"])
def test_a_scheme_other_than_http_or_https_is_refused(url):
    with pytest.raises(DwpaError, match="Invalid server URL"):
        submit_endpoint(url)


def test_the_query_endpoint_is_bmacssid(dwpa_service):
    assert query_endpoint("dwpa.example") == "https://dwpa.example/bmacssid"
    assert query_endpoint(dwpa_service.url) == f"{dwpa_service.url}/bmacssid"


def test_the_potfile_endpoint_is_the_api_download(dwpa_service):
    assert potfile_endpoint(dwpa_service.url) == f"{dwpa_service.url}/?api&dl=1"


@pytest.mark.parametrize("key", [_KEY, "ffffffffffffffffffffffffffffffff"])
def test_a_32_character_lowercase_hex_key_is_valid(key):
    assert is_valid_user_key(key)


@pytest.mark.parametrize("key", ["", "0123456789ABCDEF0123456789ABCDEF", "0123", "0" * 31,
                                 "g" * 32, " " + _KEY])
def test_anything_else_is_not_a_key(key):
    assert not is_valid_user_key(key)


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
        PotfileEntry("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "TestAP-DWPA", "88888888"),
        PotfileEntry("8e:6a:8d:a6:db:51", "00:11:22:33:44:55", "E36W126F-Guest1",
                     "pass:with:colons"),
    ]


def test_a_colon_in_the_ssid_splits_on_the_aps_known_ssid():
    [entry] = parse_potfile("020000000a01:020000000c01:My:Net:password1\n")

    assert (entry.ssid, entry.psk) == ("My", "Net:password1")
    assert split_on_known_ssid(entry, ["My", "My:Net"]) == PotfileEntry(
        "02:00:00:00:0a:01", "02:00:00:00:0c:01", "My:Net", "password1")


def test_colons_in_both_the_ssid_and_the_psk_split_on_the_aps_known_ssid():
    [entry] = parse_potfile("020000000a01:020000000c01:a:b:c:x:y:12345678\n")

    assert split_on_known_ssid(entry, ["a:b:c"]) == PotfileEntry(
        "02:00:00:00:0a:01", "02:00:00:00:0c:01", "a:b:c", "x:y:12345678")


def test_an_ssid_the_ap_does_not_have_keeps_the_first_colon_split():
    [entry] = parse_potfile("020000000a01:020000000c01:My:Net:password1\n")

    assert split_on_known_ssid(entry, ["Elsewhere"]) == entry


def test_a_line_without_an_ssid_is_skipped():
    assert parse_potfile("aabbccddeeff:112233445566::88888888\n") == []


def test_retrieve_downloads_the_potfile_with_the_key_cookie(dwpa_service):
    dwpa_service.answer(POTFILE.encode("utf-8"))

    assert retrieve_cracked(dwpa_service.url, _KEY) == [
        PotfileEntry("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "TestAP-DWPA", "88888888"),
        PotfileEntry("8e:6a:8d:a6:db:51", "00:11:22:33:44:55", "E36W126F-Guest1",
                     "pass:with:colons"),
    ]
    request = dwpa_service.requests[0]
    assert (request.method, request.path) == ("GET", "/?api&dl=1")
    assert request.header("Cookie") == f"key={_KEY}"


def test_an_html_answer_raises_instead_of_looking_empty(dwpa_service):
    dwpa_service.answer(b"<!DOCTYPE html><html><body><form>Key</form></body></html>")

    with pytest.raises(DwpaError, match="did not send a potfile"):
        retrieve_cracked(dwpa_service.url, _KEY)


def test_a_redirect_raises_and_the_key_does_not_follow_it(dwpa_service):
    target = MockDwpaService()
    try:
        dwpa_service.answer(b"", status=301, location=f"{target.url}/?api&dl=1")

        with pytest.raises(DwpaError, match="redirects to"):
            retrieve_cracked(dwpa_service.url, _KEY)
        assert target.requests[0].header("Cookie") is None
    finally:
        target.close()


def test_an_empty_potfile_has_no_networks(dwpa_service):
    dwpa_service.answer(b"")

    assert retrieve_cracked(dwpa_service.url, _KEY) == []
