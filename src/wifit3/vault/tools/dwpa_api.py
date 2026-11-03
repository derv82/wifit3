"""Talk to a DWPA instance: submit a capture, query the k-anonymity DB for a network, and
download the potfile of keys it has cracked for the user."""
import hashlib
import json
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from wifit3.crack.passwords import is_valid_psk

TIMEOUT_SECONDS = 120.0
FILE_FIELD = "webfile"
UPLOAD_ERRORS = (
    "Not a valid capture file.",
    "No valid handshakes/PMKIDs found in the submitted file.",
    "This capture file was already submitted.",
    "Capture processing error.",
    "Bad capture file",
    "No capture submitted",
)

_USER_KEY = re.compile(r"[0-9a-f]{32}")
_BSSID = re.compile(r"[0-9a-f]{12}")


class DwpaError(Exception):
    """The server refused the request; the message is the server's own when it sent one."""


@dataclass(frozen=True)
class PotfileEntry:
    bssid: str
    mac_sta: str
    ssid: str
    psk: str


def parse_potfile(text: str) -> list[PotfileEntry]:
    """The usable entries of a potfile, one ``BSSID:MAC_STA:SSID:PASS`` line per key, without
    duplicates."""
    entries = (entry for entry in map(_entry, text.splitlines()) if entry is not None)
    return list(dict.fromkeys(entries))


def split_on_known_ssid(entry: PotfileEntry, ssids: Iterable[str]) -> PotfileEntry:
    """``entry`` split after the longest of ``ssids`` its ``SSID:PASS`` starts with; the server
    does not escape a colon in an SSID, so the parse splits on the first one."""
    ssid_and_psk = f"{entry.ssid}:{entry.psk}"
    for ssid in sorted(ssids, key=len, reverse=True):
        if ssid_and_psk.startswith(f"{ssid}:"):
            psk = ssid_and_psk[len(ssid) + 1:]
            if is_valid_psk(psk):
                return replace(entry, ssid=ssid, psk=psk)
    return entry


def is_valid_user_key(user_key: str) -> bool:
    """Whether ``user_key`` is the 32-character lowercase hex key a DWPA server issues."""
    return _USER_KEY.fullmatch(user_key) is not None


def network_hash(bssid: str, ssid: str) -> str:
    """SHA-1 of the BSSID then the SSID, both as lowercase hex: the k-anonymity lookup hash."""
    hex_bssid = bssid.replace(":", "").lower()
    return hashlib.sha1(f"{hex_bssid}{ssid.encode('utf-8').hex()}".encode("ascii")).hexdigest()


def network_leaked(url: str, bssid: str, ssid: str) -> bool:
    """Whether the server's leaked-key DB may know this network's key; false positives are
    possible when the key was changed after it leaked."""
    digest = network_hash(bssid, ssid)
    request = urllib.request.Request(
        f"{_base_url(url)}/bmacssid", data=json.dumps([digest[:4]]).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    return digest[-8:] in _response_text(request, url)


def retrieve_cracked(url: str, user_key: str) -> list[PotfileEntry]:
    """The networks DWPA cracked for ``user_key``; a body with text but no potfile lines (e.g. an
    error page) raises DwpaError."""
    request = urllib.request.Request(f"{_base_url(url)}/?api&dl=1")
    request.add_unredirected_header("Cookie", f"key={user_key}")
    text = _response_text(request, url)
    entries = parse_potfile(text)
    if text.strip() and not entries:
        raise DwpaError("The server did not send a potfile")
    return entries


def upload_capture(url: str, user_key: str, capture_path: str) -> None:
    """Submit the capture; raise DwpaError on any refusal."""
    endpoint = f"{_base_url(url)}/?submit="
    boundary = uuid.uuid4().hex
    body = _multipart_body(Path(capture_path), boundary)
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    request.add_unredirected_header("Cookie", f"key={user_key}")
    answer = _response_text(request, url)
    rejection = next((message for message in UPLOAD_ERRORS if message in answer), None)
    if rejection is not None:
        raise DwpaError(rejection)


def _base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"https://{url}"
    if urlsplit(url).scheme not in ("http", "https"):
        raise DwpaError(f"Invalid server URL: {url}")
    return url


def _response_bytes(request: urllib.request.Request, url: str) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.url != request.full_url:
                raise DwpaError(f"{request.full_url} redirects to {response.url}; use that URL")
            return response.read()
    except urllib.error.HTTPError as e:
        raise DwpaError(f"HTTP {e.code} from {request.full_url}") from None
    except urllib.error.URLError as e:
        raise DwpaError(f"Cannot reach {request.full_url}: {e.reason}") from None
    except OSError as e:
        raise DwpaError(f"Cannot reach {request.full_url}: {e}") from None
    except (ValueError, OverflowError):
        raise DwpaError(f"Invalid server URL: {url}") from None


def _response_text(request: urllib.request.Request, url: str) -> str:
    return _response_bytes(request, url).decode("utf-8", "replace")


def _multipart_body(path: Path, boundary: str) -> bytes:
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{FILE_FIELD}"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n")
    return head.encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")


def _entry(line: str) -> PotfileEntry | None:
    fields = line.split(":", 3)
    if len(fields) != 4:
        return None
    bssid = _colon_bssid(fields[0])
    ssid, psk = fields[2], fields[3]
    if bssid is None or not ssid or not is_valid_psk(psk):
        return None
    return PotfileEntry(bssid, _colon_bssid(fields[1]) or "", ssid, psk)


def _colon_bssid(text: str) -> str | None:
    hex_bssid = text.strip().lower()
    if _BSSID.fullmatch(hex_bssid) is None:
        return None
    return ":".join(hex_bssid[index:index + 2] for index in range(0, 12, 2))
