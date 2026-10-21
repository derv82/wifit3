"""Hashtopolis User API client (``POST <server>/api/user.php``, JSON in and out); the User API,
not API v2, because every server from 0.11 to 1.0.1 serves it unchanged."""
import base64
import json
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

TIMEOUT_SECONDS = 10.0
WPA_HASH_TYPE_ID = 22000
WORDLIST_FILE_TYPE = 0


class HashtopolisError(Exception):
    """The server answered with an error; the message is the server's own."""


class HashtopolisUnreachable(HashtopolisError):
    """No answer from the server, or an answer that isn't the User API."""


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
        self.endpoint = user_api_endpoint(url)
        self.access_key = access_key

    def call(self, section: str, request: str, **params) -> dict:
        body = {"section": section, "request": request, "accessKey": self.access_key, **params}
        try:
            http_request = urllib.request.Request(
                self.endpoint, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(http_request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise HashtopolisUnreachable(f"HTTP {e.code} from {self.endpoint}") from None
        except urllib.error.URLError as e:
            raise HashtopolisUnreachable(f"Cannot reach {self.endpoint}: {e.reason}") from None
        except OSError as e:
            raise HashtopolisUnreachable(f"Cannot reach {self.endpoint}: {e}") from None
        except (ValueError, OverflowError):
            raise HashtopolisError(f"Invalid server URL: {self.endpoint}") from None
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
        self.call("test", "connection")
        self.call("test", "access")

    def wordlists(self) -> list[ServerFile]:
        files = self.call("file", "listFiles").get("files") or []
        with _reply_to("file/listFiles"):
            return [ServerFile(int(f["fileId"]), str(f["filename"])) for f in files
                    if f.get("fileType") == WORDLIST_FILE_TYPE]

    def access_groups(self) -> list[AccessGroup]:
        groups = self.call("group", "listGroups").get("groups") or []
        with _reply_to("group/listGroups"):
            return [AccessGroup(int(g["groupId"]), str(g["name"])) for g in groups]

    def newest_hashcat_version_id(self) -> int:
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
        data = base64.b64encode(("\n".join(hashlines) + "\n").encode("utf-8")).decode("ascii")
        response = self.call(
            "hashlist", "createHashlist", name=name, isSalted=False, isSecret=False,
            isHexSalt=False, separator=":", format=0, hashtypeId=WPA_HASH_TYPE_ID,
            accessGroupId=access_group_id, useBrain=False, brainFeatures=0, data=data)
        with _reply_to("hashlist/createHashlist"):
            return int(response["hashlistId"])

    def create_task(self, name: str, hashlist_id: int, wordlist: ServerFile,
                    cracker_version_id: int) -> int:
        response = self.call(
            "task", "createTask", name=name, hashlistId=hashlist_id,
            attackCmd=f"#HL# {wordlist.filename}", chunksize=600, statusTimer=5,
            benchmarkType="speed", color="", isCpuOnly=False, isSmall=False, skip=0,
            crackerVersionId=cracker_version_id, files=[wordlist.file_id], preprocessorId=0,
            preprocessorCommand="", priority=10, maxAgents=0)
        with _reply_to("task/createTask"):
            return int(response["taskId"])

    def cracked_keys(self, hashlist_id: int) -> list[str]:
        cracked = self.call("hashlist", "getCracked", hashlistId=hashlist_id).get("cracked") or []
        with _reply_to("hashlist/getCracked"):
            return [str(c["plain"]) for c in cracked if c["plain"]]

    def task_progress(self, task_id: int) -> TaskProgress:
        task = self.call("task", "getTask", taskId=task_id)
        with _reply_to("task/getTask"):
            return TaskProgress(int(task.get("keyspace") or 0), int(task.get("searched") or 0),
                                bool(task.get("isComplete")))

    def archive_task(self, task_id: int) -> None:
        self.call("task", "archiveTask", taskId=task_id)


@contextmanager
def _reply_to(request: str) -> Generator[None]:
    try:
        yield
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HashtopolisError(f"Unexpected reply to {request}") from None


def user_api_endpoint(url: str) -> str:
    """``http://host:8080`` (scheme optional) to its ``/api/user.php``."""
    base = url.strip().rstrip("/")
    if "://" not in base:
        base = "http://" + base
    if base.endswith("/api/user.php"):
        return base
    return base + "/api/user.php"
