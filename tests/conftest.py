"""Global test fixtures."""
import itertools
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from wifit3.campaigns.campaign import Campaign
from wifit3.persist.config import Config, CpuCrackSettings, HashcatSettings, HashtopolisSettings

_test_dir_numbers = itertools.count()


@pytest.fixture(autouse=True)
def _reset_campaign_active():
    Campaign.active = None   # before test
    yield
    Campaign.active = None   # after test


@pytest.fixture(scope="session")
def _test_dirs_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("test_dirs")


@pytest.fixture
def _test_dir(request: pytest.FixtureRequest, _test_dirs_root: Path) -> Path:
    """The test's tmp_path if it uses one, else a fresh empty dir: tmp_path lists every earlier
    test's dir before making its own, which costs seconds across the suite."""
    if "tmp_path" in request.fixturenames:
        return request.getfixturevalue("tmp_path")
    path = _test_dirs_root / str(next(_test_dir_numbers))
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _captures_to_tmp(_test_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Config.captures_dir at each test's own dir so nothing writes to ./captures."""
    monkeypatch.setattr(Config, "captures_dir", str(_test_dir))


@pytest.fixture(autouse=True)
def _fresh_tool_settings(monkeypatch):
    """Each test starts from the default cracking-tool tables, so one test's edits can't leak."""
    monkeypatch.setattr(Config, "cpu_crack", CpuCrackSettings())
    monkeypatch.setattr(Config, "hashcat", HashcatSettings())
    monkeypatch.setattr(Config, "hashtopolis", HashtopolisSettings())


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
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01},
                                        daemon=True)
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
