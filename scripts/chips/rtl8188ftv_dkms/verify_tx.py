"""Acceptance check: decode rtl8188ftv_dkms bulk-OUT TX against the vendor capture.

Walks every EP 0x02 bulk-OUT URB with the port's ``tx`` descriptor decoder:
40B descriptor (checksum over the first 32 bytes with the checksum field
zeroed) + 802.11 payload. Every descriptor must rebuild byte-exact via
``tx.build_tx_desc`` from its decoded fields. MGMT frames match the port's
MGMT template (bcmc stainfo mac_id 1, QSLT_MGNT, 11B raid 8, CCK-1M rate,
HWSEQ_EN, plain dump except the wait-ack disconnect deauth) with a single
monotonic mgnt_seq domain starting at 0; DATA frames the BE template
(mac_id 0, raid 6, agg 1) with their own seq domain from 1, and the
EAP/ARP/DHCP rule (1M use-rate + short preamble) is checked against the
SNAP ether type and UDP ports. Slow (full tshark pass); run on demand,
not in the hot gate.

Run: uv run python scripts/chips/rtl8188ftv_dkms/verify_tx.py [pcap-path]
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8188ftv_dkms import tx as tx_mod

BUNDLES = (
    REPO / "driver_captures" / "captures_8188fu" / "capture-6.pcap",
    REPO / "src" / "wifit3" / "scripts" / "captures_rtl8188fu"
    / "capture-6.pcap",
)
CARD_MAC = bytes.fromhex("44efbf1f9dfb")
AP_BSSID = bytes.fromhex("1c634979c104")
BEWAVE_SSID = b"BEWAVE_AP_001B9C181A97"
RATES_IE = bytes((0x01, 0x08, 0x82, 0x84, 0x8B, 0x96, 0x8C, 0x12, 0x98, 0x24))
EXT_RATES_IE = bytes((0x32, 0x04, 0xB0, 0x48, 0x60, 0x6C))


def _resolve(arg: str | None) -> Path:
    if arg is not None:
        p = Path(arg)
        if p.exists():
            return p
        raise SystemExit(f"capture not found: {arg}")
    for cand in BUNDLES:
        if cand.exists():
            return cand
    raise SystemExit("capture-6.pcap not found in either bundle dir")


def _words(desc: bytes) -> list[int]:
    return [struct.unpack("<I", desc[i:i + 4])[0]
            for i in range(0, 40, 4)]


def _decode(desc: bytes, payload: bytes) -> dict:
    dw = _words(desc)
    return {"size": dw[0] & 0xFFFF,
            "bmc": (dw[0] >> 24) & 1,
            "macid": dw[1] & 0x7F,
            "qsel": (dw[1] >> 8) & 0x1F,
            "rateid": (dw[1] >> 16) & 0x1F,
            "agg_break": (dw[2] >> 16) & 1,
            "spe_rpt": (dw[2] >> 19) & 1,
            "userate": (dw[3] >> 8) & 1,
            "txrate": dw[4] & 0x7F,
            "rten": (dw[4] >> 17) & 1,
            "rtlim": (dw[4] >> 18) & 0x3F,
            "short": (dw[5] >> 4) & 1,
            "mbssid": (dw[6] >> 12) & 0xF,
            "swdef": dw[6] & 0xFFF,
            "agg": (dw[7] >> 24) & 0xFF,
            "hwseq": (dw[8] >> 15) & 1,
            "seq": (dw[9] >> 12) & 0xFFF,
            "dw2rest": dw[2] & ~(0x10000 | 0x80000),
            "dw3rest": dw[3] & ~(0x100),
            "dw5rest": dw[5] & ~(0x10),
            "dw6rest": dw[6] & ~(0xF000),
            "dw8rest": dw[8] & ~(0x8000)}


def _is_mcast(addr: bytes) -> bool:
    return bool(addr[0] & 0x01)


def _ether(payload: bytes) -> int | None:
    if len(payload) < 32 or payload[24:30] != b"\xaa\xaa\x03\x00\x00\x00":
        return None
    return struct.unpack(">H", payload[30:32])[0]


def _udp_ports(payload: bytes) -> tuple | None:
    if _ether(payload) != 0x0800 or len(payload) < 34:
        return None
    ihl = (payload[32] & 0xF) * 4
    base = 32 + ihl
    if len(payload) < base + 4:
        return None
    return (payload[base:base + 2], payload[base + 2:base + 4])


def main() -> int:
    pcap = _resolve(sys.argv[1] if len(sys.argv) > 1 else None)
    out = subprocess.run(
        ["tshark", "-r", str(pcap),
         "-Y", "(usb.endpoint_address==0x02 || usb.endpoint_address==0x03) "
               "&& usb.data_len>0",
         "-T", "fields", "-e", "frame.number", "-e", "usb.endpoint_address",
         "-e", "usb.capdata"],
        capture_output=True, text=True, check=True).stdout
    mgnt_seq = 0
    data_seq = 1
    counts: dict = {}
    n = 0
    for line in out.splitlines():
        parts = line.split("\t")
        fno, ep, data = int(parts[0]), int(parts[1], 0), parts[2].strip()
        if not data:
            continue
        buf = bytes.fromhex(data)
        assert len(buf) > tx_mod.TXDESC_SIZE, (fno, len(buf))
        desc, payload = buf[:tx_mod.TXDESC_SIZE], buf[tx_mod.TXDESC_SIZE:]
        assert tx_mod.txdesc_checksum(desc) == \
            struct.unpack("<H", desc[28:30])[0], \
            f"frame {fno}: descriptor checksum mismatch"
        f = _decode(desc, payload)
        assert f["size"] == len(payload), (fno, f)
        assert f["mbssid"] == 0 and f["hwseq"] == 1, (fno, f)
        assert f["bmc"] == (1 if _is_mcast(payload[4:10]) else 0), (fno, f)
        assert payload[10:16] == CARD_MAC, (fno, "TX SA")
        typ, sub = (payload[0] >> 2) & 0x3, (payload[0] >> 4) & 0xF
        if typ == 0:
            assert ep == 0x02, (fno, hex(ep))
            assert f["macid"] == tx_mod.MGMT_MACID, (fno, f)
            assert f["qsel"] == tx_mod.QSLT_MGNT, (fno, f)
            assert f["rateid"] == tx_mod.MGMT_RAID, (fno, f)
            assert f["userate"] == 1 and f["txrate"] == 0, (fno, f)
            assert f["rten"] == 1, (fno, f)
            assert f["agg"] == 0 and f["swdef"] == 0, (fno, f)
            assert f["dw2rest"] == 0 and f["dw3rest"] == 0 \
                and f["dw5rest"] == 0 and f["dw6rest"] == 0 \
                and f["dw8rest"] == 0, (fno, f)
            assert f["seq"] == mgnt_seq, (fno, f["seq"], mgnt_seq)
            assert payload[22:24] == struct.pack("<H", (mgnt_seq << 4) & 0xFFF0), \
                (fno, "frame seq")
            mgnt_seq = (mgnt_seq + 1) & 0xFFF
            if sub == 12:
                assert f["rtlim"] == tx_mod.MGMT_RETRY_LIMIT_INJECT, (fno, f)
                assert f["spe_rpt"] == 1 and f["agg_break"] == 0, (fno, f)
                assert payload[4:10] == AP_BSSID, (fno, "deauth DA")
                assert payload[24:26] == b"\x03\x00", (fno, "reason")
                rebuilt = tx_mod.build_mgnt_desc(
                    size=len(payload), seq=f["seq"], bmc=False,
                    retry_limit=tx_mod.MGMT_RETRY_LIMIT_INJECT, spe_rpt=True)
            else:
                assert f["rtlim"] == tx_mod.MGMT_RETRY_LIMIT_ASSOC, (fno, f)
                assert f["spe_rpt"] == 0 and f["agg_break"] == 0, (fno, f)
                rebuilt = tx_mod.build_mgnt_desc(
                    size=len(payload), seq=f["seq"], bmc=f["bmc"] == 1,
                    retry_limit=tx_mod.MGMT_RETRY_LIMIT_ASSOC)
            assert rebuilt == desc, (fno, "rebuild")
            if sub == 4:
                assert payload[4:10] == b"\xff" * 6, (fno, "probe DA")
                body = payload[24:]
                assert body[2:2 + len(BEWAVE_SSID)] == BEWAVE_SSID \
                    or body[:2] == b"\x00\x00", (fno, "probe SSID")
                assert RATES_IE in body and EXT_RATES_IE in body, \
                    (fno, "probe rates")
            elif sub == 11:
                assert payload[4:10] == AP_BSSID, (fno, "auth DA")
            elif sub == 0:
                assert payload[4:10] == AP_BSSID, (fno, "assoc DA")
                assert BEWAVE_SSID in payload, (fno, "assoc SSID")
            elif sub == 12:
                pass
            else:
                raise AssertionError(f"frame {fno}: MGMT {sub:#x}")
            counts[f"mgmt-{sub:#x}"] = counts.get(f"mgmt-{sub:#x}", 0) + 1
        else:
            assert typ == 2, (fno, hex(payload[0]))
            assert ep == 0x03, (fno, hex(ep))
            assert f["macid"] == 0 and f["qsel"] == tx_mod.QSLT_BE, (fno, f)
            assert f["rateid"] == 6 and f["txrate"] == 0, (fno, f)
            assert f["rten"] == 0 and f["rtlim"] == 0, (fno, f)
            assert f["agg"] == 1 and f["spe_rpt"] == 0, (fno, f)
            assert f["agg_break"] == 1, (fno, f)
            assert f["dw3rest"] == 0 and f["dw5rest"] == 0 \
                and f["dw6rest"] == 0 and f["dw8rest"] == 0 \
                and f["dw2rest"] == 0, (fno, f)
            assert f["seq"] == data_seq, (fno, f["seq"], data_seq)
            data_seq += 1
            assert payload[22:24] == struct.pack("<H", (f["seq"] << 4) & 0xFFF0), \
                (fno, "data frame seq")
            ether = _ether(payload)
            ports = _udp_ports(payload)
            if ether in (0x0806, 0x888E) or (ports is not None and (
                    ports[0] in (b"\x00\x43", b"\x00\x44")
                    or ports[1] in (b"\x00\x43", b"\x00\x44"))):
                expect_sp = True
            else:
                expect_sp = False
            assert f["userate"] == (1 if expect_sp else 0), (fno, f, ether)
            assert f["short"] == (1 if expect_sp else 0), (fno, f, ether)
            rebuilt = tx_mod.build_tx_desc(
                size=len(payload), seq=f["seq"], macid=0, qsel=0, rateid=6,
                use_rate=bool(f["userate"]), tx_rate=0, retry_en=False,
                retry_limit=0, bmc=f["bmc"] == 1, agg_num=1,
                agg_break=True, data_short=bool(f["short"]),
                fb_limit=0x1F if not expect_sp else 0)
            assert rebuilt == desc, (fno, "rebuild")
            counts["data"] = counts.get("data", 0) + 1
        n += 1
    print(f"tx: {n} bulk-OUT URBs from {pcap.name}: {counts}, "
          f"mgnt_seq={mgnt_seq}, data_seq={data_seq}")
    assert n > 0 and mgnt_seq > 0
    print("tx: green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
