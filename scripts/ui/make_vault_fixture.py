"""Write the vault benchmarks' captures dir, 168 synthetic APs: python make_vault_fixture.py <dir>.
Every hashline is dated in its .meta, as a first load leaves it; copy it with cp -a to keep the mtimes."""
import os
import random
import sys
from pathlib import Path
from typing import NamedTuple

from wifit3.crack.hc22000_format import hashline_id
from wifit3.crack.wpa_psk import pmkid
from wifit3.dot11.eapol import eapol_key, set_mic
from wifit3.dot11.wpa import KCK_LEN, derive_pmk, derive_ptk, eapol_mic
from wifit3.persist.common import bssid_to_dashed, safe_ssid
from wifit3.persist.config import Config
from wifit3.persist.exhaustion import SSID_VARIANTS_IDENTITY, SSID_VARIANTS_NAME
from wifit3.persist.meta import update_meta
from wifit3.persist.pcap import write_pcap

FIRST_EPOCH = 1_790_000_000
UNCRACKED_PSK = "not-in-the-vault"
ROCKYOU_IDENTITY = "139921507:1790000000000000000"
RSN_IE = bytes.fromhex("30140100000fac040100000fac040100000fac020000")
M1_KEY_INFO = 0x008a
M2_KEY_INFO = 0x010a                # pairwise, MIC, key descriptor version 2
_M2_M3_NONCE_CORRECTED = "82"


def mac_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def data_frame(bssid: bytes, client: bytes, eapol: bytes) -> bytes:
    header = b"\x08\x02\x00\x00" + client + bssid + bssid + b"\x00\x00"
    return header + b"\xaa\xaa\x03\x00\x00\x00\x88\x8e" + eapol


class M1M2(NamedTuple):
    """An M1's EAPOL payload and the M2 answering it, its MIC zeroed in ``m2_unsigned``."""
    anonce: bytes
    snonce: bytes
    m1: bytes
    m2_unsigned: bytes
    mic: bytes

    @property
    def m2(self) -> bytes:
        return set_mic(self.m2_unsigned, self.mic)


def m1_m2(psk: str, ssid: str, bssid: str, client: str, rng: random.Random) -> M1M2:
    """An M1 and the M2 a client knowing ``psk`` answers it with."""
    anonce, snonce = rng.randbytes(32), rng.randbytes(32)
    m2_unsigned = eapol_key(key_info=M2_KEY_INFO, key_len=0, replay=1, nonce=snonce, key_data=RSN_IE)
    kck = derive_ptk(derive_pmk(psk, ssid.encode()), mac_bytes(bssid), mac_bytes(client), anonce, snonce,
                     KCK_LEN)
    m1 = eapol_key(key_info=M1_KEY_INFO, key_len=16, replay=1, nonce=anonce)
    return M1M2(anonce, snonce, m1, m2_unsigned, eapol_mic(kck, m2_unsigned))


class ApFilesWriter:
    """Writes one SSID+BSSID's files to captures/: pcaps, .hc22000, PSK/PIN/WEP .txt and .meta."""

    def __init__(self, captures_dir: Path, ssid: str, bssid: str, rng: random.Random) -> None:
        self.captures_dir, self.ssid, self.bssid, self.rng = captures_dir, ssid, bssid, rng
        self.lines: list[str] = []
        self.dates: dict[str, dict] = {}
        self.exhausted: dict[str, dict[str, int]] = {}
        self.wordlist_names: dict[str, list[str]] = {}

    def _stem(self) -> str:
        return f"{safe_ssid(self.ssid)}_{bssid_to_dashed(self.bssid)}"

    def add_handshake(self, psk: str, client: str, epoch: int) -> None:
        """An M1+M2 pcap and its M2+M3 hashline, which ``psk`` cracks."""
        messages = m1_m2(psk, self.ssid, self.bssid, client, self.rng)
        aa, spa = mac_bytes(self.bssid), mac_bytes(client)
        write_pcap(self.captures_dir / f"{self._stem()}_{epoch}_handshake.pcap",
                   [(data_frame(aa, spa, messages.m1), epoch), (data_frame(aa, spa, messages.m2), epoch + 0.01)])
        self._add_line(f"WPA*02*{messages.mic.hex()}*{aa.hex()}*{spa.hex()}*{self.ssid.encode().hex()}"
                       f"*{messages.anonce.hex()}*{messages.m2_unsigned.hex()}*{_M2_M3_NONCE_CORRECTED}", epoch)

    def add_pmkid(self, psk: str, client: str, epoch: int) -> None:
        aa, spa = mac_bytes(self.bssid), mac_bytes(client)
        value = pmkid(derive_pmk(psk, self.ssid.encode()), aa, spa)
        self._add_line(f"WPA*01*{value.hex()}*{aa.hex()}*{spa.hex()}*{self.ssid.encode().hex()}***", epoch)

    def _add_line(self, raw: str, epoch: int) -> None:
        self.lines.append(raw)
        self.dates[hashline_id(raw)] = {"captured": [epoch]}

    def add_psk(self, psk: str, epoch: int) -> None:
        self._write_txt(epoch, "wpa_psk", f"PSK: {psk}\n")

    def add_wps_pin(self, pin: str, psk: str, epoch: int) -> None:
        self._write_txt(epoch, "wps_pin", f"PSK: {psk}\nPIN: {pin}\n")

    def add_wep_key(self, key_hex: str, epoch: int) -> None:
        self._write_txt(epoch, "wep_key", f"WEP key (hex):   {key_hex}\n")

    def _write_txt(self, epoch: int, kind: str, fields: str) -> None:
        (self.captures_dir / f"{self._stem()}_{epoch}_{kind}.txt").write_text(
            f"SSID: {self.ssid}\nBSSID: {self.bssid}\n{fields}", encoding="utf-8")

    def add_exhausted(self, identity: str, wordlist_name: str, hashline_count: int, epoch: int) -> None:
        """Record that the wordlist ran out on the first ``hashline_count`` hashlines."""
        lines = self.lines[:hashline_count]
        self.exhausted.setdefault(identity, {}).update({hashline_id(raw): epoch for raw in lines})
        self.wordlist_names[identity] = [wordlist_name]

    def write(self) -> None:
        if self.lines:
            path = self.captures_dir / f"{self._stem()}.hc22000"
            path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
            newest = max(epoch for date in self.dates.values() for epoch in date["captured"])
            os.utime(path, (newest, newest))
        update_meta(self.ssid, self.bssid, hashlines=self.dates or None, exhausted=self.exhausted or None,
                    wordlists=self.wordlist_names or None)


def write_heavy(captures_dir: Path, rng: random.Random) -> None:
    """Heavy-050..200: that many handshakes, half cracked by an old PSK, then 3 PMKIDs; both PSKs saved."""
    for n, count in enumerate((50, 100, 150, 200)):
        ap = ApFilesWriter(captures_dir, f"Heavy-{count:03d}", f"02:00:00:02:00:{n:02x}", rng)
        clients = [f"02:00:00:03:{n:02x}:{c:02x}" for c in range(3)]
        old_psk, new_psk = f"old-password-{n}", f"new-password-{n}"
        for i in range(count):
            ap.add_handshake(old_psk if i < count // 2 else new_psk, clients[i % 3], FIRST_EPOCH + i * 600)
        for c, client in enumerate(clients):
            ap.add_pmkid(new_psk, client, FIRST_EPOCH + count * 600 + c)
        ap.add_psk(old_psk, FIRST_EPOCH + 300)
        ap.add_psk(new_psk, FIRST_EPOCH + count * 600)
        ap.write()


def write_networks(captures_dir: Path, rng: random.Random) -> None:
    """Network-000..059: in turn cracked, a lone handshake, a WPS PIN, a WEP key."""
    for i in range(60):
        ap = ApFilesWriter(captures_dir, f"Network-{i:03d}", f"02:00:00:01:00:{i:02x}", rng)
        client, epoch, psk = f"02:00:00:04:00:{i:02x}", FIRST_EPOCH + i * 3600, f"password-{i}"
        if i % 4 == 0:
            ap.add_handshake(psk, client, epoch)
            ap.add_handshake(psk, client, epoch + 60)
            ap.add_pmkid(psk, client, epoch + 61)
            ap.add_psk(psk, epoch + 120)
        elif i % 4 == 1:
            ap.add_handshake(psk, client, epoch)
        elif i % 4 == 2:
            ap.add_handshake(psk, client, epoch)
            ap.add_wps_pin(f"{12345670 + i:08d}", psk, epoch + 60)
        else:
            ap.add_wep_key(f"{i:010x}", epoch)
        ap.write()


def write_uncracked(ap: ApFilesWriter, handshake_count: int, clients: list[str]) -> None:
    """Handshakes and a PMKID per client; SSID variants ran out on all, rockyou.txt on half."""
    for i in range(handshake_count):
        ap.add_handshake(UNCRACKED_PSK, clients[i % len(clients)], FIRST_EPOCH + i * 600)
    for c, client in enumerate(clients):
        ap.add_pmkid(UNCRACKED_PSK, client, FIRST_EPOCH + handshake_count * 600 + c)
    exhausted_epoch = FIRST_EPOCH + handshake_count * 600 + 3600
    ap.add_exhausted(SSID_VARIANTS_IDENTITY, SSID_VARIANTS_NAME, len(ap.lines), exhausted_epoch)
    ap.add_exhausted(ROCKYOU_IDENTITY, "rockyou.txt", len(ap.lines) // 2, exhausted_epoch)
    ap.write()


def write_uncracked_aps(captures_dir: Path, rng: random.Random) -> None:
    """Uncracked-100/250/500 with 8 clients, and Uncracked-Network-000..099 with 1-6 handshakes."""
    for n, count in enumerate((100, 250, 500)):
        clients = [f"02:00:00:06:{n:02x}:{c:02x}" for c in range(8)]
        write_uncracked(ApFilesWriter(captures_dir, f"Uncracked-{count:03d}", f"02:00:00:05:00:{n:02x}", rng),
                        count, clients)
    for i in range(100):
        clients = [f"02:00:00:08:{i:02x}:{c:02x}" for c in range(1 + i % 3)]
        write_uncracked(ApFilesWriter(captures_dir, f"Uncracked-Network-{i:03d}", f"02:00:00:07:00:{i:02x}", rng),
                        1 + i % 6, clients)


def write_renamed(captures_dir: Path, rng: random.Random) -> None:
    """One BSSID seen under 5 SSIDs, Renamed-1..5, 2 handshakes each; the PSK saved under the last."""
    bssid, client, psk = "02:00:00:09:00:00", "02:00:00:0a:00:00", "renamed-password"
    for n in range(1, 6):
        ap = ApFilesWriter(captures_dir, f"Renamed-{n}", bssid, rng)
        epoch = FIRST_EPOCH + n * 86400
        ap.add_handshake(psk, client, epoch)
        ap.add_handshake(psk, client, epoch + 60)
        if n == 5:
            ap.add_psk(psk, epoch + 120)
        ap.write()


def main() -> None:
    captures_dir = Path(sys.argv[1])
    captures_dir.mkdir(parents=True, exist_ok=True)
    Config.captures_dir = str(captures_dir)
    rng = random.Random(4321)
    write_heavy(captures_dir, rng)
    write_networks(captures_dir, rng)
    write_uncracked_aps(captures_dir, rng)
    write_renamed(captures_dir, rng)


if __name__ == "__main__":
    main()
