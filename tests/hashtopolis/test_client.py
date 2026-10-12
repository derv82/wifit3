from __future__ import annotations

import base64
import json
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

from wifit3.hashtopolis.client import (
    HashtopolisAuthError,
    HashtopolisClient,
    HashtopolisConfigError,
    HashtopolisError,
    HashtopolisTransientError,
    HttpResponse,
    _SafeRedirectHandler,
    normalize_url,
)


@dataclass
class Call:
    method: str
    url: str
    headers: dict
    body: Optional[bytes]
    timeout: float

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    def json(self):
        return json.loads(self.body) if self.body else None


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._routes: list[tuple[str, str, Callable[[Call], HttpResponse]]] = []
        self.net_faults = 0

    def route(self, method: str, path: str, fn: Callable[[Call], HttpResponse]) -> "FakeServer":
        self._routes.append((method, "/api/v2" + path, fn))
        return self

    def json(self, method: str, path: str, payload, status: int = 200) -> "FakeServer":
        return self.route(method, path, lambda c: HttpResponse(status, json.dumps(payload).encode()))

    def __call__(self, method, url, headers, body, timeout) -> HttpResponse:
        self.calls.append(Call(method, url, dict(headers), body, timeout))
        if self.net_faults > 0:
            self.net_faults -= 1
            raise TimeoutError("injected network fault")
        path = urlsplit(url).path
        for m, p, fn in self._routes:
            if m == method and p == path:
                return fn(self.calls[-1])
        return HttpResponse(404, b'{"message":"not found"}')

    def calls_to(self, path: str) -> list[Call]:
        return [c for c in self.calls if c.path == "/api/v2" + path]


def _collection(items):
    return {"data": [{"type": "X", "id": str(i), "attributes": a} for i, a in items]}


def _resource(id_, attrs):
    return {"data": {"type": "X", "id": str(id_), "attributes": attrs}}


def _client(server, **kw):
    kw.setdefault("token", "TOK")
    return HashtopolisClient("http://ht.local", transport=server, sleep=lambda *_: None, **kw)


@pytest.mark.parametrize("raw, expected", [
    ("http://host", "http://host"),
    ("https://host/", "https://host"),
    ("host:8080", "http://host:8080"),
    ("192.0.2.1", "http://192.0.2.1"),
    ("192.0.2.1:8080", "http://192.0.2.1:8080"),
    ("http://[2001:db8::1]", "http://[2001:db8::1]"),
    ("http://[2001:db8::1]:8080", "http://[2001:db8::1]:8080"),
    ("https://user:pw@host:443/sub/", "https://host:443/sub"),
    ("http://host/api/v2", "http://host"),
    ("https://host/root/api/v2/", "https://host/root"),
    ("https://host/root/?token=secret#fragment", "https://host/root"),
    ("  https://Host.Local  ", "https://host.local"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("bad", [
    "", "ftp://host", "file:///etc/passwd", "   ",
    "http://host:", "http://host:nope", "http://host:0", "http://host:65536",
    "http://2001:db8::1:8080", "http://[2001:db8::1",
    "http://exa mple.com", "http://ho st:8080", "http://[2001:db8::1]junk",
    "http://[not-an-ip]", "http://[2001:db8::1]x:80",
])
def test_normalize_url_rejects(bad):
    with pytest.raises(HashtopolisConfigError):
        normalize_url(bad)


def test_normalize_url_strips_embedded_credentials():
    assert "secret" not in normalize_url("https://admin:secret@host/")


def test_invalid_url_error_does_not_leak_embedded_credentials():
    with pytest.raises(HashtopolisConfigError) as exc:
        normalize_url("https://admin:VERYSECRET@host:notaport")
    assert "admin" not in str(exc.value)
    assert "VERYSECRET" not in str(exc.value)


def test_get_sends_json_api_accept_and_bearer_only():
    server = FakeServer().json("GET", "/ui/accessgroups", _collection([(1, {"groupName": "A"})]))
    _client(server, token="abc123").list_access_groups()
    headers = server.calls[-1].headers
    assert headers["Accept"] == "application/vnd.api+json"
    assert headers["Authorization"] == "Bearer abc123"
    assert "Content-Type" not in headers
    assert not server.calls_to("/auth/token")


def test_client_requires_nonempty_token():
    with pytest.raises(HashtopolisConfigError):
        HashtopolisClient("http://ht.local", token="")


def test_get_retries_transient_then_succeeds():
    server = FakeServer().json("GET", "/ui/accessgroups", _collection([(1, {"groupName": "A"})]))
    server.net_faults = 2
    assert _client(server).list_access_groups()[0].id == 1
    assert len(server.calls) == 3


def test_get_gives_up_as_transient():
    server = FakeServer().json("GET", "/ui/accessgroups", _collection([]))
    server.net_faults = 99
    with pytest.raises(HashtopolisTransientError):
        _client(server, max_retries=2).list_access_groups()


def test_5xx_on_get_is_transient():
    server = FakeServer().route("GET", "/ui/accessgroups", lambda c: HttpResponse(503, b"{}"))
    with pytest.raises(HashtopolisTransientError):
        _client(server, max_retries=1).list_access_groups()


def test_post_is_not_retried_on_network_error():
    server = FakeServer()
    server.net_faults = 99
    with pytest.raises(HashtopolisTransientError):
        _client(server).create_hashlist("n", ["WPA*01*a*b*c*d***"], 1)
    assert len(server.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_is_auth_error(status):
    server = FakeServer().route(
        "GET", "/ui/accessgroups", lambda c: HttpResponse(status, b"{}"))
    with pytest.raises(HashtopolisAuthError):
        _client(server).list_access_groups()


def test_4xx_is_config_error_with_detail():
    server = FakeServer().route(
        "GET", "/ui/accessgroups",
        lambda c: HttpResponse(400, json.dumps({"message": "bad filter"}).encode()))
    with pytest.raises(HashtopolisConfigError) as exc:
        _client(server).list_access_groups()
    assert "bad filter" in str(exc.value)


def test_create_hashlist_payload_and_base64():
    server = FakeServer().json("POST", "/ui/hashlists", _resource(42, {"name": "Home"}), status=201)
    lines = ["WPA*01*aa*bb*cc*dd***", "WPA*02*ee*ff*gg*hh*ii*jj*00"]
    hid = _client(server, is_secret=True).create_hashlist(
        "Home", lines, access_group_id=7, notes="wifit3:upload-id")
    assert hid == 42
    call = server.calls_to("/ui/hashlists")[0]
    assert call.headers["Accept"] == "application/vnd.api+json"
    assert call.headers["Content-Type"] == "application/vnd.api+json"
    assert call.headers["Authorization"] == "Bearer TOK"
    assert call.json()["data"]["type"] == "hashlist"
    attrs = call.json()["data"]["attributes"]
    assert attrs["hashTypeId"] == 22000
    assert attrs["sourceType"] == "paste"
    assert attrs["accessGroupId"] == 7
    assert attrs["isSecret"] is True
    assert attrs["notes"] == "wifit3:upload-id"
    assert base64.b64decode(attrs["sourceData"]).decode() == "\n".join(lines) + "\n"


def test_create_hashlist_is_secret_reflects_trusted_setting():
    server = FakeServer().json("POST", "/ui/hashlists", _resource(1, {}), status=201)
    _client(server, is_secret=False).create_hashlist("n", ["WPA*01*a*b*c*d***"], 1)
    assert server.calls_to("/ui/hashlists")[0].json()["data"]["attributes"]["isSecret"] is False


def test_create_hashlist_without_id_raises():
    server = FakeServer().json("POST", "/ui/hashlists", {"data": {"type": "hashlist", "attributes": {}}},
                               status=201)
    with pytest.raises(HashtopolisConfigError):
        _client(server).create_hashlist("n", ["WPA*01*a*b*c*d***"], 1)


def test_find_hashlist_filters_by_marker_and_access_group():
    server = FakeServer().json(
        "GET", "/ui/hashlists",
        _collection([(42, {"name": "Home", "notes": "wifit3:upload-id", "accessGroupId": 7})]))
    assert _client(server).find_hashlist("wifit3:upload-id", 7) == 42
    query = parse_qs(urlsplit(server.calls_to("/ui/hashlists")[0].url).query)
    assert query == {"filter[notes]": ["wifit3:upload-id"],
                     "filter[accessGroupId]": ["7"], "page[size]": ["2"]}


def test_find_hashlist_rejects_non_exact_result():
    server = FakeServer().json(
        "GET", "/ui/hashlists",
        _collection([(42, {"notes": "wifit3:other", "accessGroupId": 7})]))
    assert _client(server).find_hashlist("wifit3:upload-id", 7) is None


def test_resolve_access_group_auto_single():
    server = FakeServer().json("GET", "/ui/accessgroups", _collection([(4, {"groupName": "only"})]))
    assert _client(server).resolve_access_group() == 4


def test_resolve_access_group_uses_configured():
    server = FakeServer().json("GET", "/ui/accessgroups",
                               _collection([(1, {"groupName": "a"}), (2, {"groupName": "b"})]))
    assert _client(server, access_group_id=2).resolve_access_group() == 2


def test_resolve_access_group_ambiguous_raises():
    server = FakeServer().json("GET", "/ui/accessgroups",
                               _collection([(1, {"groupName": "a"}), (2, {"groupName": "b"})]))
    with pytest.raises(HashtopolisConfigError):
        _client(server).resolve_access_group()


def test_resolve_access_group_missing_configured_raises():
    server = FakeServer().json("GET", "/ui/accessgroups", _collection([(1, {"groupName": "a"})]))
    with pytest.raises(HashtopolisConfigError):
        _client(server, access_group_id=99).resolve_access_group()


def test_test_connection_reports_access_group():
    server = FakeServer().json("GET", "/ui/accessgroups", _collection([(3, {"groupName": "only"})]))
    info = _client(server).test_connection()
    assert info["access_group_id"] == 3
    assert info["access_groups"] == [{"id": 3, "name": "only"}]
    assert len(server.calls_to("/ui/accessgroups")) == 1


def test_errors_never_leak_secrets():
    server = FakeServer().route(
        "POST", "/ui/hashlists",
        lambda c: HttpResponse(400, json.dumps({"message": "nope"}).encode()))
    line = "WPA*02*deadbeef*aabbccddeeff*112233445566*4869*nonce*eapol*00"
    c = HashtopolisClient("http://ht.local", token="SUPERSECRETTOKEN",
                          transport=server, sleep=lambda *_: None)
    with pytest.raises(HashtopolisError) as exc:
        c.create_hashlist("Home", [line], access_group_id=1)
    text = str(exc.value)
    assert "SUPERSECRETTOKEN" not in text
    assert base64.b64encode((line + "\n").encode()).decode() not in text


def test_reflected_payload_is_withheld_not_truncated_into_error():
    # A realistic handshake base64's to well over the old 200-char detail cap, so a reflected
    # prefix used to survive. The whole server body is now withheld for payload-bearing requests.
    line = "WPA*02*" + "d" * 32 + "*aabbccddeeff*112233445566*" + "e" * 64 + "*" + "f" * 32 + "*00"
    source_b64 = base64.b64encode((line + "\n").encode()).decode()

    def _echo(call):
        sent = call.json()["data"]["attributes"]
        body = {"message": f"rejected token=SUPERSECRETTOKEN data={sent['sourceData']}"}
        return HttpResponse(400, json.dumps(body).encode())

    server = FakeServer().route("POST", "/ui/hashlists", _echo)
    c = HashtopolisClient("http://ht.local", token="SUPERSECRETTOKEN",
                          transport=server, sleep=lambda *_: None)
    with pytest.raises(HashtopolisConfigError) as exc:
        c.create_hashlist("Home", [line], access_group_id=1)
    text = str(exc.value)
    assert "SUPERSECRETTOKEN" not in text
    assert source_b64 not in text
    assert source_b64[:40] not in text          # not even a surviving prefix
    assert "withheld" in text


def test_reflected_token_is_redacted_in_get_error():
    def _echo(call):
        return HttpResponse(400, json.dumps({"message": "bad token=SUPERSECRETTOKEN sorry"}).encode())

    server = FakeServer().route("GET", "/ui/accessgroups", _echo)
    c = HashtopolisClient("http://ht.local", token="SUPERSECRETTOKEN",
                          transport=server, sleep=lambda *_: None)
    with pytest.raises(HashtopolisConfigError) as exc:
        c.list_access_groups()
    text = str(exc.value)
    assert "SUPERSECRETTOKEN" not in text
    assert "***" in text


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirect_to_other_origin_drops_authorization(code):
    handler = _SafeRedirectHandler()
    req = urllib.request.Request("https://trusted.example/api/v2/ui/accessgroups",
                                 headers={"Authorization": "Bearer SECRET"}, method="GET")
    new = handler.redirect_request(req, None, code, "redirect", {},
                                   "https://evil.example/api/v2/ui/accessgroups")
    assert all(h.lower() != "authorization" for h in new.headers)


def test_redirect_within_same_origin_keeps_authorization():
    handler = _SafeRedirectHandler()
    req = urllib.request.Request("https://trusted.example/api/v2/ui/accessgroups",
                                 headers={"Authorization": "Bearer SECRET"}, method="GET")
    new = handler.redirect_request(req, None, 302, "redirect", {},
                                   "https://trusted.example/api/v2/ui/hashlists")
    assert new.headers.get("Authorization") == "Bearer SECRET"


def test_non_json_response_raises_hashtopolis_error():
    server = FakeServer().route(
        "GET", "/ui/accessgroups", lambda c: HttpResponse(200, b"<html>not json</html>"))
    with pytest.raises(HashtopolisError):
        _client(server).list_access_groups()


@pytest.mark.parametrize("payload", [[], None, "nope", 123])
def test_non_object_json_top_level_raises(payload):
    server = FakeServer().route(
        "GET", "/ui/accessgroups", lambda c: HttpResponse(200, json.dumps(payload).encode()))
    with pytest.raises(HashtopolisError):
        _client(server).list_access_groups()


@pytest.mark.parametrize("payload", [{}, {"data": "oops"}, {"data": [1, 2]}, {"data": None}])
def test_unexpected_data_shapes_yield_empty_collection(payload):
    server = FakeServer().route(
        "GET", "/ui/accessgroups", lambda c: HttpResponse(200, json.dumps(payload).encode()))
    assert _client(server)._collection("/ui/accessgroups") == []


def test_access_group_with_non_numeric_id_is_ignored():
    server = FakeServer().json(
        "GET", "/ui/accessgroups", _collection([("abc", {"groupName": "bad"})]))
    with pytest.raises(HashtopolisConfigError):
        _client(server).resolve_access_group()
