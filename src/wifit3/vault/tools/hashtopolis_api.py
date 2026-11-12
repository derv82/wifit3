"""Hashtopolis client. A JWT access key uses API v2 (``/api/v2``); any other key uses the
User API (``POST /api/user.php``)."""
import base64
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

from wifit3.crack.wpa_psk import hashcat_plain

TIMEOUT_SECONDS = 10.0
WPA_HASH_TYPE_ID = 22000
WORDLIST_FILE_TYPE = 0
_TASK_COMPLETED = 3


class HashtopolisError(Exception):
    """The server answered with an error; the message is the server's own."""


class HashtopolisUnreachable(HashtopolisError):
    """No answer from the server, or an answer that isn't its API."""


@dataclass(frozen=True)
class ServerFile:
    file_id: int
    filename: str


@dataclass(frozen=True)
class AccessGroup:
    group_id: int
    name: str


@dataclass(frozen=True)
class TaskProgress:
    keyspace: int
    searched: int
    is_complete: bool


class HashtopolisApi:
    def __init__(self, url: str, access_key: str) -> None:
        self.access_key = _normalize_access_key(access_key)
        self.v2 = _is_v2_token(self.access_key)
        self.endpoint = api_endpoint(url, self.access_key)

    def call(self, section: str, request: str, **params) -> dict:
        body = {"section": section, "request": request, "accessKey": self.access_key, **params}
        raw = self._read(self.endpoint, data=json.dumps(body).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            data = json.loads(raw)
        except ValueError:
            raise HashtopolisUnreachable(f"{self.endpoint} is not a Hashtopolis User API") from None
        if not isinstance(data, dict):
            raise HashtopolisUnreachable(f"{self.endpoint} is not a Hashtopolis User API")
        if data.get("response") not in ("OK", "SUCCESS"):
            raise HashtopolisError(str(data.get("message") or f"{section}/{request} failed"))
        return data

    def check_access(self) -> None:
        if self.v2:
            self._v2("GET", "/ui/accessgroups", {"page[size]": 1})
            return
        self.call("test", "connection")
        self.call("test", "access")

    def wordlists(self) -> list[ServerFile]:
        if self.v2:
            return self._v2_wordlists()
        files = self.call("file", "listFiles").get("files") or []
        with _reply_to("file/listFiles"):
            return [ServerFile(int(f["fileId"]), str(f["filename"])) for f in files
                    if f.get("fileType") == WORDLIST_FILE_TYPE]

    def access_groups(self) -> list[AccessGroup]:
        if self.v2:
            return self._v2_access_groups()
        groups = self.call("group", "listGroups").get("groups") or []
        with _reply_to("group/listGroups"):
            return [AccessGroup(int(g["groupId"]), str(g["name"])) for g in groups]

    def newest_hashcat_version_id(self) -> int:
        if self.v2:
            return self._v2_newest_hashcat_version_id()
        crackers = self.call("cracker", "listCrackers").get("crackers") or []
        with _reply_to("cracker/listCrackers"):
            hashcat = next((c for c in crackers if c.get("crackerTypeName") == "hashcat"), None)
            if hashcat is None:
                raise HashtopolisError("The server has no hashcat cracker")
            cracker_type_id = hashcat["crackerTypeId"]
        versions = self.call("cracker", "getCracker",
                             crackerTypeId=cracker_type_id).get("crackerVersions") or []
        if not versions:
            raise HashtopolisError("The server has no hashcat version")
        with _reply_to("cracker/getCracker"):
            return max(int(v["versionId"]) for v in versions)

    def create_hashlist(self, name: str, hashlines: list[str], access_group_id: int) -> int:
        if self.v2:
            return self._v2_create_hashlist(name, hashlines, access_group_id)
        data = base64.b64encode(("\n".join(hashlines) + "\n").encode("utf-8")).decode("ascii")
        response = self.call(
            "hashlist", "createHashlist", name=name, isSalted=False, isSecret=False,
            isHexSalt=False, separator=":", format=0, hashtypeId=WPA_HASH_TYPE_ID,
            accessGroupId=access_group_id, useBrain=False, brainFeatures=0, data=data)
        with _reply_to("hashlist/createHashlist"):
            return int(response["hashlistId"])

    def create_task(self, name: str, hashlist_id: int, wordlist: ServerFile,
                    cracker_version_id: int) -> int:
        if self.v2:
            return self._v2_create_task(name, hashlist_id, wordlist, cracker_version_id)
        response = self.call(
            "task", "createTask", name=name, hashlistId=hashlist_id,
            attackCmd=f"#HL# {wordlist.filename}", chunksize=600, statusTimer=5,
            benchmarkType="speed", color="", isCpuOnly=False, isSmall=False, skip=0,
            crackerVersionId=cracker_version_id, files=[wordlist.file_id], preprocessorId=0,
            preprocessorCommand="", priority=10, maxAgents=0)
        with _reply_to("task/createTask"):
            return int(response["taskId"])

    def cracked_keys(self, hashlist_id: int) -> list[str]:
        if self.v2:
            return self._v2_cracked_keys(hashlist_id)
        cracked = self.call("hashlist", "getCracked", hashlistId=hashlist_id).get("cracked") or []
        with _reply_to("hashlist/getCracked"):
            return [hashcat_plain(str(c["plain"])) for c in cracked if c["plain"]]

    def task_progress(self, task_id: int) -> TaskProgress:
        if self.v2:
            return self._v2_task_progress(task_id)
        task = self.call("task", "getTask", taskId=task_id)
        with _reply_to("task/getTask"):
            return TaskProgress(int(task.get("keyspace") or 0), int(task.get("searched") or 0),
                                bool(task.get("isComplete")))

    def archive_task(self, task_id: int) -> None:
        if self.v2:
            self._v2_archive_task(task_id)
            return
        self.call("task", "archiveTask", taskId=task_id)

    def _read(self, url: str, *, data: bytes | None, headers: dict[str, str], method: str,
              v2: bool = False) -> bytes:
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if v2:
                raise self._http_error(e) from None
            raise HashtopolisUnreachable(f"HTTP {e.code} from {self.endpoint}") from None
        except urllib.error.URLError as e:
            raise HashtopolisUnreachable(f"Cannot reach {self.endpoint}: {e.reason}") from None
        except OSError as e:
            raise HashtopolisUnreachable(f"Cannot reach {self.endpoint}: {e}") from None
        except (ValueError, OverflowError, http.client.InvalidURL):
            raise HashtopolisError(f"Invalid server URL: {self.endpoint}") from None
        except http.client.HTTPException:
            raise HashtopolisUnreachable(f"{self.endpoint} is not a Hashtopolis API") from None

    def _http_error(self, error: urllib.error.HTTPError) -> HashtopolisError:
        try:
            data = json.loads(error.read() or b"")
        except ValueError:
            data = None
        title = str(data["title"]) if isinstance(data, dict) and data.get("title") else ""
        if 400 <= error.code < 500:
            return HashtopolisError(title or f"HTTP {error.code} from {self.endpoint}")
        return HashtopolisUnreachable(title or f"HTTP {error.code} from {self.endpoint}")

    def _v2(self, method: str, path: str, query: dict | None = None,
            body: dict | None = None) -> dict:
        url = self.endpoint + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": "Bearer " + self.access_key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        raw = self._read(url, data=data, headers=headers, method=method, v2=True)
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise HashtopolisUnreachable(f"{self.endpoint} is not a Hashtopolis API") from None
        if not isinstance(parsed, dict):
            raise HashtopolisUnreachable(f"{self.endpoint} is not a Hashtopolis API")
        return parsed

    def _list(self, path: str, query: dict | None = None) -> list[dict]:
        params = dict(query or {})
        seen: set[str] = set()
        items: list[dict] = []
        while True:
            page = self._v2("GET", path, params)
            data = page.get("data")
            if not isinstance(data, list):
                raise HashtopolisError(f"Unexpected reply to {path}")
            items.extend(data)
            links = page.get("links")
            nxt = links.get("next") if isinstance(links, dict) else None
            if not nxt:
                return items
            after = _page_after(str(nxt))
            if not after or after in seen:
                raise HashtopolisError(f"Unexpected reply to {path}")
            seen.add(after)
            params["page[after]"] = after

    def _v2_wordlists(self) -> list[ServerFile]:
        files = self._list("/ui/files", {"filter[fileType]": WORDLIST_FILE_TYPE})
        with _reply_to("/ui/files"):
            return [ServerFile(int(f["id"]), str(f["attributes"]["filename"])) for f in files
                    if f["attributes"]["fileType"] == WORDLIST_FILE_TYPE]

    def _v2_access_groups(self) -> list[AccessGroup]:
        groups = self._list("/ui/accessgroups")
        with _reply_to("/ui/accessgroups"):
            return [AccessGroup(int(g["id"]), str(g["attributes"]["groupName"])) for g in groups]

    def _v2_newest_hashcat_version_id(self) -> int:
        types = self._list("/ui/crackertypes", {"filter[typeName]": "hashcat"})
        with _reply_to("/ui/crackertypes"):
            hashcat = next((t for t in types if t["attributes"]["typeName"] == "hashcat"), None)
            if hashcat is None:
                raise HashtopolisError("The server has no hashcat cracker")
            type_id = int(hashcat["id"])
        query = {"filter[crackerBinaryTypeId]": type_id}
        binaries = self._list("/ui/crackers", query)
        with _reply_to("/ui/crackers"):
            if any("hashtypes" in binary.get("relationships", {}) for binary in binaries):
                binaries = self._list("/ui/crackers", {**query, "include": "hashtypes"})
            supporting: list[int] = []
            unknown: list[int] = []
            for binary in binaries:
                if int(binary["attributes"]["crackerBinaryTypeId"]) != type_id:
                    continue
                hashtypes = _hashtype_ids(binary)
                if WPA_HASH_TYPE_ID in hashtypes:
                    supporting.append(int(binary["id"]))
                elif not hashtypes:
                    unknown.append(int(binary["id"]))
            chosen = supporting or unknown
            if not chosen:
                raise HashtopolisError("The server has no hashcat version")
            return max(chosen)

    def _v2_create_hashlist(self, name: str, hashlines: list[str], access_group_id: int) -> int:
        data = base64.b64encode(("\n".join(hashlines) + "\n").encode("utf-8")).decode("ascii")
        response = self._v2("POST", "/ui/hashlists", body=_resource("hashlist", {
            "name": name, "isSalted": False, "isSecret": False, "isHexSalt": False,
            "separator": ":", "format": 0, "hashTypeId": WPA_HASH_TYPE_ID, "hashCount": 0,
            "accessGroupId": access_group_id, "useBrain": False, "brainFeatures": 0,
            "notes": "", "isArchived": False, "sourceType": "paste", "sourceData": data}))
        with _reply_to("/ui/hashlists"):
            return int(response["data"]["id"])

    def _v2_create_task(self, name: str, hashlist_id: int, wordlist: ServerFile,
                        cracker_version_id: int) -> int:
        response = self._v2("POST", "/ui/tasks", body=_resource("task", {
            "taskName": name, "hashlistId": hashlist_id,
            "attackCmd": f"#HL# {wordlist.filename}", "chunkTime": 600, "statusTimer": 5,
            "color": "", "useNewBench": True, "isCpuTask": False, "isSmall": False, "skipKeyspace": 0,
            "crackerBinaryId": cracker_version_id, "files": [wordlist.file_id],
            "preprocessorId": 0, "preprocessorCommand": "", "priority": 10, "maxAgents": 0,
            "notes": "", "staticChunks": 0, "chunkSize": 0, "forcePipe": False,
            "isArchived": False}))
        with _reply_to("/ui/tasks"):
            return int(response["data"]["id"])

    def _v2_cracked_keys(self, hashlist_id: int) -> list[str]:
        cracked = self._list("/ui/hashes", {"filter[hashlistId]": hashlist_id,
                                            "filter[isCracked]": "true"})
        with _reply_to("/ui/hashes"):
            return [hashcat_plain(str(row["attributes"]["plaintext"])) for row in cracked
                    if row["attributes"]["plaintext"]]

    def _v2_task_progress(self, task_id: int) -> TaskProgress:
        task = self._v2("GET", f"/ui/tasks/{task_id}", {"aggregate[task]": "cprogress,status"})
        with _reply_to("/ui/tasks"):
            attributes = task["data"]["attributes"]
            return TaskProgress(int(attributes["keyspace"]), int(attributes["cprogress"]),
                                int(attributes["status"]) == _TASK_COMPLETED)

    def _v2_archive_task(self, task_id: int) -> None:
        self._v2("PATCH", f"/ui/tasks/{task_id}", body={
            "data": {"type": "task", "id": task_id, "attributes": {"isArchived": True}}})


@contextmanager
def _reply_to(request: str) -> Generator[None]:
    try:
        yield
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HashtopolisError(f"Unexpected reply to {request}") from None


def api_endpoint(url: str, access_key: str) -> str:
    """``http://host:8080`` to ``/api/v2`` for a JWT, otherwise ``/api/user.php``."""
    suffix = "/api/v2" if _is_v2_token(_normalize_access_key(access_key)) else "/api/user.php"
    return _server_base(url) + suffix


def user_api_endpoint(url: str) -> str:
    """``http://host:8080`` (scheme optional) to its ``/api/user.php``."""
    return _server_base(url) + "/api/user.php"


def _server_base(url: str) -> str:
    base = url.strip().rstrip("/")
    if "://" not in base:
        base = "http://" + base
    for suffix in ("/api/user.php", "/api/v2"):
        if base.endswith(suffix):
            return base[:-len(suffix)].rstrip("/")
    return base


def _normalize_access_key(access_key: str) -> str:
    key = access_key.strip()
    if key.lower().startswith("bearer "):
        return key[7:].strip()
    return key


def _is_v2_token(access_key: str) -> bool:
    parts = access_key.split(".")
    return len(parts) == 3 and all(parts)


def _resource(type_name: str, attributes: dict) -> dict:
    return {"data": {"type": type_name, "attributes": attributes}}


def _page_after(url: str) -> str:
    values = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("page[after]") or []
    return values[0] if values else ""


def _hashtype_ids(resource: dict) -> set[int]:
    relationships = resource.get("relationships") or {}
    hashtypes = relationships.get("hashtypes") if isinstance(relationships, dict) else None
    data = hashtypes.get("data") if isinstance(hashtypes, dict) else None
    if not isinstance(data, list):
        return set()
    return {int(item["id"]) for item in data}
