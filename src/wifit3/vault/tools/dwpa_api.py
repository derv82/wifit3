"""Talk to a DWPA instance: submit a capture, query the k-anonymity DB for a network, and
download the potfile of keys it has cracked for the user."""
import hashlib
import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from wifit3.persist.potfile import PotfileEntry, parse_potfile

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


class DwpaError(Exception):
    """The server refused the request; the message is the server's own when it sent one."""


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
    """The user's cracked keys, as the API's download link serves them."""
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


def retrieve_cracked(url: str, key: str) -> list[PotfileEntry]:
    """The networks DWPA cracked for ``key``, read from its potfile download."""
    request = urllib.request.Request(potfile_endpoint(url))
    request.add_unredirected_header("Cookie", f"key={key}")
    return parse_potfile(decode_potfile(_response_bytes(request, url)))


def decode_potfile(body: bytes) -> str:
    """The downloaded potfile text; an answer holding no potfile lines is an error, not an
    empty result (e.g. a login page for a key the server no longer accepts)."""
    text = body.decode("utf-8", "replace")
    if not text.strip() or parse_potfile(text):
        return text
    raise DwpaError("The server did not send a potfile")


def upload_capture(url: str, key: str, capture_path: str) -> str:
    """Submit the capture and return the server's answer; raise DwpaError on any refusal."""
    endpoint = submit_endpoint(url)
    boundary = uuid.uuid4().hex
    body = _multipart_body(Path(capture_path), boundary)
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    request.add_unredirected_header("Cookie", f"key={key}")
    answer = _response_text(request, url)
    rejection = next((message for message in REJECTIONS if message in answer), None)
    if rejection is not None:
        raise DwpaError(rejection)
    return answer


def _base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if "://" in url else f"http://{url}"


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
