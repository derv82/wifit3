from __future__ import annotations

import base64
import ipaddress
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

WPA_HASH_TYPE = 22000
_API_PREFIX = "/api/v2"
_JSON_API_MEDIA_TYPE = "application/vnd.api+json"
_HASHLIST_RESOURCE_TYPE = "hashlist"
_REDACTED = "***"


class HashtopolisError(Exception):
    pass


class HashtopolisTransientError(HashtopolisError):
    pass


class HashtopolisAuthError(HashtopolisError):
    pass


class HashtopolisConfigError(HashtopolisError):
    pass


@dataclass(frozen=True)
class AccessGroupChoice:
    id: int
    name: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else {}


Transport = Callable[[str, str, dict[str, str], Optional[bytes], float], HttpResponse]


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drops the Authorization header when a redirect crosses to another origin, so the
    Bearer token is never replayed to a different scheme/host/port than it was issued for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _same_origin(req.full_url, newurl):
            for header in [h for h in new.headers if h.lower() == "authorization"]:
                del new.headers[header]
        return new


_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _urllib_transport(method: str, url: str, headers: dict[str, str],
                      body: Optional[bytes], timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return HttpResponse(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"")


def normalize_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise HashtopolisConfigError("Hashtopolis URL is empty")
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise HashtopolisConfigError("Invalid Hashtopolis URL") from None
    if parts.scheme not in ("http", "https"):
        raise HashtopolisConfigError(f"Unsupported URL scheme: {parts.scheme!r} (use http or https)")
    if not host:
        raise HashtopolisConfigError("Hashtopolis URL has no host")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in host):
        raise HashtopolisConfigError("Invalid Hashtopolis URL host")
    if "[" in parts.netloc:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise HashtopolisConfigError("Invalid Hashtopolis URL host") from None
        after_bracket = parts.netloc.rsplit("@", 1)[-1].split("]", 1)[1]
        if after_bracket and not (after_bracket.startswith(":") and after_bracket[1:].isdigit()):
            raise HashtopolisConfigError("Invalid Hashtopolis URL host")
    if parts.netloc.rsplit("@", 1)[-1].endswith(":"):
        raise HashtopolisConfigError("Invalid Hashtopolis URL")
    if port is not None and not 1 <= port <= 65535:
        raise HashtopolisConfigError("Invalid Hashtopolis URL")
    safe_host = f"[{host}]" if ":" in host else host
    netloc = f"{safe_host}:{port}" if port is not None else safe_host
    path = parts.path.rstrip("/")
    if path.endswith(_API_PREFIX):
        path = path[: -len(_API_PREFIX)]
    return urlunsplit((parts.scheme, netloc, path, "", ""))


class HashtopolisClient:
    def __init__(self, url: str, *, token: str, access_group_id: Optional[int] = None,
                 is_secret: bool = True, transport: Optional[Transport] = None,
                 timeout: float = 10.0, max_retries: int = 4, backoff_base: float = 1.0,
                 backoff_cap: float = 60.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = normalize_url(url)
        self.access_group_id = access_group_id
        self.is_secret = is_secret
        self._token = token
        self._transport = transport or _urllib_transport
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        if not token:
            raise HashtopolisConfigError("No Hashtopolis API token configured")

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_cap, self._backoff_base * (2 ** attempt))

    def _send(self, method: str, url: str, headers: dict[str, str],
              body: Optional[bytes]) -> HttpResponse:
        attempt = 0
        while True:
            try:
                resp = self._transport(method, url, headers, body, self._timeout)
            except (urllib.error.URLError, TimeoutError, OSError):
                if method == "GET" and attempt < self._max_retries:
                    self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise HashtopolisTransientError("Could not reach Hashtopolis (network error)") from None
            if method == "GET" and (resp.status == 429 or 500 <= resp.status < 600) \
                    and attempt < self._max_retries:
                self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            return resp

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 body: Optional[dict] = None) -> HttpResponse:
        url = self._build_url(path, params)
        data = None
        headers = {
            "Accept": _JSON_API_MEDIA_TYPE,
            "Authorization": "Bearer " + self._token,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = _JSON_API_MEDIA_TYPE
        resp = self._send(method, url, headers, data)
        if resp.status in (401, 403):
            raise HashtopolisAuthError(f"{method} {self._safe(path)} -> HTTP {resp.status}")
        if resp.status == 429 or 500 <= resp.status < 600:
            raise HashtopolisTransientError(f"{method} {self._safe(path)} -> HTTP {resp.status}")
        if resp.status >= 400:
            raise HashtopolisConfigError(
                f"{method} {self._safe(path)} -> HTTP {resp.status}: {self._error_detail(resp, body)}")
        return resp

    def _build_url(self, path: str, params: Optional[dict]) -> str:
        url = path if path.startswith(("http://", "https://")) else self.base_url + _API_PREFIX + path
        if params:
            query = urlencode({k: v for k, v in params.items() if v is not None}, safe="[]")
            url = f"{url}?{query}"
        return url

    @staticmethod
    def _safe(path: str) -> str:
        return path.split("?", 1)[0]

    def _error_detail(self, resp: HttpResponse, body: Optional[dict]) -> str:
        # A request that carried the uploaded hash payload never echoes the server's free-form
        # body: a reflected payload could leak in part even after redaction, so withhold it whole.
        if _carries_hash_payload(body):
            return "(server response withheld to avoid leaking the upload)"
        return self._redact_token(self._detail(resp))[:200]

    @staticmethod
    def _detail(resp: HttpResponse) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.body.decode("utf-8", errors="replace") if resp.body else ""
        if isinstance(data, dict):
            for key in ("message", "title", "detail", "error"):
                if data.get(key):
                    return str(data[key])
        return ""

    def _redact_token(self, text: str) -> str:
        # Redact before any truncation, so a reflected token cannot survive as a surviving prefix.
        return text.replace(self._token, _REDACTED) if text and self._token else text

    def _json(self, resp: HttpResponse) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise HashtopolisConfigError("Hashtopolis returned a non-JSON response") from None
        if not isinstance(data, dict):
            raise HashtopolisConfigError("Hashtopolis returned an unexpected response shape")
        return data

    @staticmethod
    def _flatten(resource: dict) -> dict:
        attributes = resource.get("attributes")
        out = dict(attributes) if isinstance(attributes, dict) else {}
        out["id"] = _to_int(resource.get("id"))
        return out

    def _collection(self, path: str, params: Optional[dict] = None) -> list[dict]:
        data = self._json(self._request("GET", path, params=params)).get("data")
        if not isinstance(data, list):
            return []
        return [self._flatten(item) for item in data if isinstance(item, dict)]

    def _create(self, path: str, resource_type: str, attributes: dict) -> dict:
        body = {"data": {"type": resource_type, "attributes": attributes}}
        payload = self._json(self._request("POST", path, body=body))
        created = payload.get("data")
        return self._flatten(created) if isinstance(created, dict) else {}

    def list_access_groups(self) -> list[AccessGroupChoice]:
        groups = self._collection("/ui/accessgroups", {"page[size]": 1000})
        return [AccessGroupChoice(id=g["id"], name=str(g.get("groupName") or g["id"]))
                for g in groups if isinstance(g.get("id"), int)]

    def resolve_access_group(self) -> int:
        groups = self.list_access_groups()
        return self._resolve_access_group(groups)

    def _resolve_access_group(self, groups: list[AccessGroupChoice]) -> int:
        if self.access_group_id is not None:
            if any(g.id == self.access_group_id for g in groups):
                return self.access_group_id
            raise HashtopolisConfigError(
                f"Configured access group {self.access_group_id} is not reachable")
        if not groups:
            raise HashtopolisConfigError("No reachable Hashtopolis access group")
        if len(groups) > 1:
            raise HashtopolisConfigError(
                "Multiple access groups reachable; choose one in Preferences")
        return groups[0].id

    def find_hashlist(self, upload_marker: str, access_group_id: int) -> Optional[int]:
        hashlists = self._collection("/ui/hashlists", {
            "filter[notes]": upload_marker,
            "filter[accessGroupId]": access_group_id,
            "page[size]": 2,
        })
        matches = [item for item in hashlists
                   if item.get("notes") == upload_marker
                   and _to_int(item.get("accessGroupId")) == access_group_id
                   and item.get("id") is not None]
        return max((item["id"] for item in matches), default=None)

    def create_hashlist(self, name: str, hashlines: list[str], access_group_id: int,
                        *, notes: str = "") -> int:
        source = "\n".join(hashlines) + "\n"
        attributes = {
            "name": name,
            "hashTypeId": WPA_HASH_TYPE,
            "format": 0,
            "separator": ";",
            "isSalted": False,
            "isHexSalt": False,
            "accessGroupId": access_group_id,
            "useBrain": False,
            "brainFeatures": 3,
            "notes": notes,
            "sourceType": "paste",
            "sourceData": base64.b64encode(source.encode("utf-8")).decode("ascii"),
            "hashCount": 0,
            "isArchived": False,
            "isSecret": self.is_secret,
        }
        created = self._create("/ui/hashlists", _HASHLIST_RESOURCE_TYPE, attributes)
        hashlist_id = created.get("id")
        if hashlist_id is None:
            raise HashtopolisConfigError("Hashlist creation returned no id")
        return hashlist_id

    def test_connection(self) -> dict:
        groups = self.list_access_groups()
        return {
            "access_group_id": self._resolve_access_group(groups),
            "access_groups": [{"id": g.id, "name": g.name} for g in groups],
        }


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _carries_hash_payload(body: Optional[dict]) -> bool:
    if not isinstance(body, dict):
        return False
    attributes = (body.get("data") or {}).get("attributes") or {}
    return bool(isinstance(attributes, dict) and attributes.get("sourceData"))
