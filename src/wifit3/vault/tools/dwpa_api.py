"""Talk to a DWPA instance: submit a capture, query the k-anonymity DB for a network, and
download the potfile of keys it has cracked for the user."""
import base64
import binascii
import hashlib
import json
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from wifit3.crack.passwords import is_valid_psk

TIMEOUT_SECONDS = 120.0
FILE_FIELD = "webfile"
REJECTIONS = (
    "Not a valid capture file.",
    "No valid handshakes/PMKIDs found in the submitted file.",
    "This capture file was already submitted.",
    "Capture processing error.",
    "Bad capture file",
    "No capture submitted",
)

_KEY = re.compile(r"[0-9a-f]{32}")
_BSSID = re.compile(r"[0-9a-f]{12}")


class DwpaError(Exception):
    """The server refused the request; the message is the server's own when it sent one."""


@dataclass(frozen=True)
class DwpaNetwork:
    """One cracked line of the DWPA potfile: the AP, its SSID and the recovered PSK."""
    bssid: str
    ssid: str
    key: str


def is_valid_key(key: str) -> bool:
    """Whether ``key`` is the 32-character lowercase hex key a DWPA server issues."""
    return _KEY.fullmatch(key) is not None


def submit_endpoint(url: str) -> str:
    """``url`` with the submit query; the server accepts its web form's ``?submit=`` target."""
    return f"{_base_url(url)}/?submit="


def query_endpoint(url: str) -> str:
    """The k-anonymity endpoint for the BSSID + SSID perspective."""
    return f"{_base_url(url)}/bmacssid"


def potfile_endpoint(url: str) -> str:
    """The user's cracked keys, base64-encoded, as the API's download link serves them."""
    return f"{_base_url(url)}/?api&dl=1"


def network_hash(bssid: str, ssid: str) -> str:
    """SHA-1 of the BSSID then the SSID, both as lowercase hex: the k-anonymity lookup hash."""
    hex_bssid = bssid.replace(":", "").lower()
    return hashlib.sha1(f"{hex_bssid}{ssid.encode('utf-8').hex()}".encode("ascii")).hexdigest()


def network_leaked(url: str, bssid: str, ssid: str) -> bool:
    """Whether the server's leaked-key DB may know this network's key; false positives are
    possible when the key was changed after it leaked."""
    digest = network_hash(bssid, ssid)
    request = urllib.request.Request(
        query_endpoint(url), data=json.dumps([digest[:4]]).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    return digest[-8:] in _response_text(request, url)


def retrieve_cracked(url: str, key: str) -> list[DwpaNetwork]:
    """The networks DWPA cracked for ``key``, read from its potfile download."""
    request = urllib.request.Request(potfile_endpoint(url), headers={"Cookie": f"key={key}"})
    return parse_potfile(decode_potfile(_response_bytes(request, url)))


def decode_potfile(body: bytes) -> str:
    """wpa-sec serves the potfile as plain text; instances that base64-encode it also work."""
    text = body.decode("utf-8", "replace")
    if parse_potfile(text):
        return text
    try:
        return base64.b64decode(b"".join(body.split()), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        raise DwpaError("The server did not send a potfile") from None


def parse_potfile(text: str) -> list[DwpaNetwork]:
    """``BSSID:MAC_STA:SSID:PASS`` lines, as DWPA writes them, without duplicates."""
    networks = (network for network in map(_potfile_line, text.splitlines()) if network)
    return list(dict.fromkeys(networks))


def upload_capture(url: str, key: str, capture_path: str) -> str:
    """Submit the capture and return the server's answer; raise DwpaError on any refusal."""
    endpoint = submit_endpoint(url)
    boundary = uuid.uuid4().hex
    body = _multipart_body(Path(capture_path), boundary)
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Cookie": f"key={key}"})
    answer = _response_text(request, url)
    rejection = next((message for message in REJECTIONS if message in answer), None)
    if rejection is not None:
        raise DwpaError(rejection)
    return answer


def _potfile_line(line: str) -> DwpaNetwork | None:
    fields = line.split(":", 3)
    if len(fields) != 4:
        return None
    bssid = _colon_bssid(fields[0])
    ssid, key = fields[2], fields[3]
    if bssid is None or not ssid or not is_valid_psk(key):
        return None
    return DwpaNetwork(bssid, ssid, key)


def _colon_bssid(text: str) -> str | None:
    hex_bssid = text.strip().lower()
    if _BSSID.fullmatch(hex_bssid) is None:
        return None
    return ":".join(hex_bssid[index:index + 2] for index in range(0, 12, 2))


def _base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if "://" in url else f"http://{url}"


def _response_bytes(request: urllib.request.Request, url: str) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
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
