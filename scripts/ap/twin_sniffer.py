"""Twin sniffer: a passive third card on the twin's channel, logging what clients do there.

Run it before starting EvilTwin, on a card wifit3 won't use. It never transmits. Per client MAC it
logs (first copy only; retries are counted, not re-logged):
  probes   directed probe requests for --ssid; wildcard probes and probe responses only counted
  twin     auth / assoc / reassoc to --twin-bssid, and the twin's answers (probe resp, auth, assoc, M1)
  eapol    any EAPOL message to or from the twin
  details  auth algorithm/status; assoc-req AKMs, PMKIDs, MFPC/MFPR; assoc-resp status; M1/M2 key
           descriptor version and AKM
  target   the same, for --target-bssid, when the real AP shares this channel (single-card setup)
  kicks    deauth / disassoc involving either BSSID
  data     data frames to / from either BSSID, per client: logged once, counted always
Every 15 s and on Ctrl+C: a per-client table, plus beacons/s heard from the twin and the target.

The sniffer only hears what reaches it: a client it never hears may still have been on this channel.

Output prefixes: [+] found/ok   [*] step   [#] a measured number   [-] a problem.
BSSIDs and the SSID are passed at runtime; nothing real is hardcoded.
"""
import argparse
import asyncio
import logging
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows console is cp1252
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wifit3.device.manager import wlan_ifaces
from wifit3.dot11.ie import iter_information_elements
from wifit3.dot11.mac import mac_to_str

_DATA_SUBTYPES = {0x00: "data", 0x04: "null", 0x08: "qos data", 0x0C: "qos null"}
_QUIET = ("wildcard probe", "probe-resp")   # every passer-by does these: counted, never logged
_LOG_ONCE = ("data",)                        # logged on first sight per client, counted always
_SUBTYPES = {0x00: "assoc-req", 0x01: "assoc-resp", 0x02: "reassoc-req", 0x03: "reassoc-resp",
             0x04: "probe-req", 0x05: "probe-resp", 0x0A: "disassoc", 0x0B: "auth", 0x0C: "deauth"}
_AKMS = {1: "802.1X", 2: "PSK", 4: "FT-PSK", 6: "PSK-SHA256", 8: "SAE", 9: "FT-SAE", 24: "SAE-EXT"}
_AUTH_ALGS = {0: "open", 1: "shared", 2: "FT", 3: "SAE"}
_ASSOC_REQ_IES = {0x00: 28, 0x02: 34}                # IE offset: header + fixed fields


def rsne_detail(raw: bytes, start: int) -> str:
    """AKMs, PMKID count and MFPC/MFPR from the first RSN element at or after ``start``."""
    rsne = next((body for tag, body, _raw in iter_information_elements(raw, start) if tag == 0x30), None)
    if rsne is None or len(rsne) < 8:
        return "no RSNE"
    pw_count = int.from_bytes(rsne[6:8], "little")
    i = 8 + 4 * pw_count
    akm_count = int.from_bytes(rsne[i:i + 2], "little")
    akms = [rsne[i + 2 + 4 * n + 3] for n in range(akm_count)]
    i += 2 + 4 * akm_count
    out = "AKM " + "+".join(_AKMS.get(a, str(a)) for a in akms)
    if i + 2 <= len(rsne):
        caps = int.from_bytes(rsne[i:i + 2], "little")
        out += f", MFPC={caps >> 7 & 1} MFPR={caps >> 6 & 1}"
        i += 2
    pmkids = int.from_bytes(rsne[i:i + 2], "little") if i + 2 <= len(rsne) else 0
    return out + f", PMKID x{pmkids}"


def mgmt_detail(sub: int, raw: bytes) -> str:
    if sub == 0x0B and len(raw) >= 30:
        alg, seq, status = (int.from_bytes(raw[o:o + 2], "little") for o in (24, 26, 28))
        return f" [{_AUTH_ALGS.get(alg, alg)}, seq {seq}, status {status}]"
    if sub in _ASSOC_REQ_IES:
        return f" [{rsne_detail(raw, _ASSOC_REQ_IES[sub])}]"
    if sub in (0x01, 0x03) and len(raw) >= 28:
        return f" [status {int.from_bytes(raw[26:28], 'little')}]"
    if sub in (0x0A, 0x0C) and len(raw) >= 26:
        return f" [reason {int.from_bytes(raw[24:26], 'little')}]"
    return ""


def eapol_detail(pkt) -> str:
    key_info = getattr(pkt, "key_info", None)
    if key_info is None:
        return ""
    akm = getattr(pkt, "akm", None)
    return f" [key ver {key_info & 7}" + (f", AKM {_AKMS.get(akm, akm)}" if akm is not None else "") + "]"


def mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


class TwinSniffer:
    def __init__(self, ssid: str, twin: bytes, target: bytes | None) -> None:
        self.ssid, self.twin, self.target = ssid, twin, target
        self.started = time.monotonic()
        self.beacons: Counter = Counter()                              # "twin" / "target" -> count
        self.per_client: dict[str, Counter] = defaultdict(Counter)     # client -> event -> count
        self.retries: Counter = Counter()
        self._seen: set[tuple] = set()                                 # first-copy dedupe

    def _name(self, bssid: bytes) -> str | None:
        if bssid == self.twin:
            return "twin"
        if self.target is not None and bssid == self.target:
            return "target"
        return None

    def _event(self, client: bytes, event: str, raw: bytes) -> None:
        seq = int.from_bytes(raw[22:24], "little") >> 4 if len(raw) >= 24 else -1
        key = (client, event, seq)
        if key in self._seen:
            self.retries[event] += 1
            return
        self._seen.add(key)
        who = mac_to_str(client)
        self.per_client[who][event] += 1
        if event.startswith(_QUIET):
            return
        if self.per_client[who][event] == 1 or not event.startswith(_LOG_ONCE):
            print(f"[{time.monotonic() - self.started:7.1f} s] {who}  {event}")

    def __call__(self, pkt) -> None:
        raw = pkt.raw
        if len(raw) < 24:
            return
        a1, a2, a3 = raw[4:10], raw[10:16], raw[16:22]
        if pkt.type == "eapol":
            for ap, client, arrow in ((a1, a2, "->"), (a2, a1, "<-")):
                name = self._name(ap)
                if name:
                    self._event(client, f"eapol M{getattr(pkt, 'msg_num', 0)} {arrow} {name}"
                                + eapol_detail(pkt), raw)
            return
        if pkt.type_id == 2:                              # data: client <-> AP by the DS bits
            kind = _DATA_SUBTYPES.get(pkt.subtype_id, f"subtype {pkt.subtype_id}")
            kind += ", protected" if raw[1] & 0x40 else ", open"
            if pkt.to_ds and not pkt.from_ds and self._name(a1):
                self._event(a2, f"data ({kind}) -> {self._name(a1)}", raw)
            elif pkt.from_ds and not pkt.to_ds and self._name(a2) and not a1[0] & 1:
                self._event(a1, f"data ({kind}) <- {self._name(a2)}", raw)
            return
        if pkt.type_id != 0:
            return
        sub = pkt.subtype_id
        if sub == 0x08:                                   # beacon
            name = self._name(a2)
            if name:
                self.beacons[name] += 1
            return
        if sub == 0x04:                                   # probe request: client is a2
            ssid = getattr(pkt, "ssid", None)
            if ssid == self.ssid:
                to = self._name(a1) or ("broadcast" if a1[0] & 1 else mac_to_str(a1))
                self._event(a2, f"probe for SSID -> {to}", raw)
            elif ssid in (None, "", "<hidden>"):
                self._event(a2, "wildcard probe", raw)
            return
        label = _SUBTYPES.get(sub)
        if label is None:
            return
        to_ap, from_ap = self._name(a1), self._name(a2)
        detail = mgmt_detail(sub, raw)
        if to_ap:
            self._event(a2, f"{label} -> {to_ap}{detail}", raw)
        elif from_ap:
            client = a1 if not a1[0] & 1 else a3
            self._event(client, f"{label} <- {from_ap}{detail}", raw)

    def summary(self) -> None:
        secs = time.monotonic() - self.started
        rates = "  ".join(f"{k} {n / secs:.1f}/s" for k, n in sorted(self.beacons.items())) or "none"
        print(f"\n[#] {secs:.0f} s, beacons heard: {rates}; retries not re-logged: {sum(self.retries.values())}")
        quiet = 0
        for client, events in sorted(self.per_client.items()):
            if all(e.startswith(_QUIET) for e in events):
                quiet += 1
                continue
            print(f"    {client}  " + ", ".join(f"{e} x{n}" for e, n in sorted(events.items())))
        print(f"    (+{quiet} clients with only wildcard probes / probe responses)\n")


async def main(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.ERROR)
    ifaces = [i for i in wlan_ifaces() if a.sniffer_card.lower() in (i.description or "").lower()
              and a.channel in i.driver.SUPPORTED_CHANNELS]
    if not ifaces:
        print(f"[-] no '{a.sniffer_card}' card that supports channel {a.channel}")
        return
    sniffer = ifaces[0]
    print(f"[+] sniffer: {sniffer.description}")
    if not await sniffer.connect():
        print("[-] connect failed")
        return
    await sniffer.set_channel(a.channel)
    tap = TwinSniffer(a.ssid, mac_bytes(a.twin_bssid), mac_bytes(a.target_bssid) if a.target_bssid else None)
    sniffer.register_rx_callback(tap)
    print(f"[*] channel {a.channel}, SSID '{a.ssid}', twin {a.twin_bssid}"
          + (f", target {a.target_bssid}" if a.target_bssid else "") + ". Ctrl+C to stop.\n")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        tap.summary()                                 # the one table: on Ctrl+C
        await sniffer.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Passive sniffer for an EvilTwin run")
    p.add_argument("--sniffer-card", required=True, help="substring of the sniffer adapter name, e.g. 7925")
    p.add_argument("--channel", type=int, required=True, help="the twin's channel")
    p.add_argument("--ssid", required=True)
    p.add_argument("--twin-bssid", required=True, help="the twin's BSSID (the target's own on a decoy channel)")
    p.add_argument("--target-bssid", default="", help="the real AP, when it shares this channel")
    p.add_argument("--debug", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
