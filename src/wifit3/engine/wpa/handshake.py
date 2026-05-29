"""Single source of truth for WPA/WPA2 4-way handshake crackability + hc22000.

Both decisions that used to disagree now route through ``crackable_pairs()``:
the "did we capture a handshake?" verdict (events, CAPTURE-panel counts) AND the
hc22000 hashline build. A banner can therefore never claim a capture that
``save`` then silently refuses — they are literally the same code path.

Ground truth — what each 4-way message carries and what hashcat needs
---------------------------------------------------------------------
hashcat (mode 22000) cracks by guessing the PMK, deriving the PTK from
``ANonce + SNonce + AP_MAC + STA_MAC``, recomputing the MIC over a captured
EAPOL frame, and comparing. So it needs ANonce, SNonce, a MIC, and the exact
802.1X bytes that MIC covered.

    Msg  Dir      Nonce field      MIC   Gives hashcat
    M1   AP->STA  ANonce           no    ANonce (donor only)
    M2   STA->AP  SNonce           yes   SNonce + MIC + EAPOL  (the keystone)
    M3   AP->STA  ANonce (repeat)  yes   ANonce (its own MIC/EAPOL are unused)
    M4   STA->AP  usually ZERO     yes   MIC + EAPOL; SNonce only if not zeroed

hashcat reads the SNonce out of the *MIC frame's* embedded nonce, so the MIC
frame must actually contain the SNonce. That means the MIC frame is always M2
(SNonce present) or M4 (only when the client didn't zero its nonce) — never M3
(its nonce is the ANonce) and never M1 (no MIC). hashcat's MESSAGEPAIR table:

    0x00  M1+M2, EAPOL from M2   always crackable (M2 complete)
    0x02  M2+M3, EAPOL from M2   always crackable (M2 complete; M3 = ANonce)
    0x05  M3+M4, EAPOL from M4   only if M4's nonce != 0 (echoed SNonce)
    0x01  M1+M4, EAPOL from M4   only if M4's nonce != 0
    0x03/0x04 (EAPOL from M3)    marked "unused" by hashcat — never emitted here

So a "captured handshake" requires a usable *keystone*: a complete M2 (or, rarely,
a complete M4 with a non-zero nonce) plus an ANonce donor (M1 or M3) from the
same association.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from wifit3.engine.models import EapolFrame, Handshake

# hashcat module_22000 MESSAGEPAIR bytes (EAPOL-source encoded in the low bits).
_PAIR_M1M2_E2 = 0x00
_PAIR_M1M4_E4 = 0x01
_PAIR_M2M3_E2 = 0x02
_PAIR_M3M4_E4 = 0x05

# MIC sits at offset 81 within the 802.1X payload; a usable MIC frame's payload
# must reach at least through it (81 + 16).
_MIC_OFFSET = 81
_MIC_LEN = 16
_FULL_EAPOL = _MIC_OFFSET + _MIC_LEN
_NONCE_LEN = 32
_ZERO_NONCE = b"\x00" * _NONCE_LEN
_ZERO_MIC = b"\x00" * _MIC_LEN

# Coarse wall-clock backstop: two frames whose replay counters line up but that
# arrived farther apart than this are treated as different associations. Matches
# hcxpcapngtool's EAPOLTIMEOUT (5 s default); the precise same-association
# binding is done by arrival order in _same_association. Skipped when timestamps
# are unset (fixtures / round-tripped pcaps).
_EAPOL_PAIR_WINDOW_S = 5.0


@dataclass(frozen=True)
class MessageInfo:
    """Per-frame content descriptor — the hashcat-relevant fields, for logging.

    ``useful`` answers "does this frame contribute what a crackable pair needs?"
    so the UI can dim frames that arrived degraded (e.g. a clipped M2)."""
    msg_num: int
    has_nonce: bool       # a real 32-byte, non-zero nonce
    has_mic: bool         # a real 16-byte, non-zero MIC (M1 legitimately has none)
    eapol_complete: bool  # 802.1X payload reaches through the MIC (>= 97 bytes)

    @property
    def useful(self) -> bool:
        if self.msg_num == 1:   # ANonce donor
            return self.has_nonce
        if self.msg_num == 2:   # keystone — SNonce + MIC + complete EAPOL
            return self.has_nonce and self.has_mic and self.eapol_complete
        if self.msg_num == 3:   # ANonce donor (own MIC/EAPOL unused by hashcat)
            return self.has_nonce
        if self.msg_num == 4:   # conditional keystone — needs an echoed SNonce too
            return self.has_mic and self.eapol_complete and self.has_nonce
        return False


def describe(f: EapolFrame) -> MessageInfo:
    """Content descriptor for one EAPOL frame."""
    return MessageInfo(
        msg_num=f.msg_num,
        has_nonce=len(f.nonce) == _NONCE_LEN and f.nonce != _ZERO_NONCE,
        has_mic=len(f.mic) == _MIC_LEN and f.mic != _ZERO_MIC,
        eapol_complete=len(f.eapol_payload) >= _FULL_EAPOL,
    )


@dataclass(frozen=True)
class CrackablePair:
    """A pair that yields a hashcat-crackable WPA*02 line.

    ``anonce_frame`` donates the ANonce; ``mic_frame`` (always M2 or a non-zeroed
    M4) donates the MIC, the SNonce, and the EAPOL bytes."""
    anonce_frame: EapolFrame
    mic_frame: EapolFrame
    pair_byte: int

    @property
    def instance_key(self) -> bytes:
        """ANonce — fresh per association, so it identifies one 4-way instance."""
        return self.anonce_frame.nonce


def _replay(f: EapolFrame) -> int:
    return int.from_bytes(bytes.fromhex(f.replay_hex), "big")


def _within_window(a: EapolFrame, b: EapolFrame) -> bool:
    """True if two frames are close enough in time to be one handshake. Skipped
    when either timestamp is unset (0.0) — keeps fixtures / pre-timestamp
    captures working off the replay-counter rules alone."""
    if a.timestamp <= 0 or b.timestamp <= 0:
        return True
    return abs(a.timestamp - b.timestamp) <= _EAPOL_PAIR_WINDOW_S


def _mic_frame_usable(f: EapolFrame) -> bool:
    """Can this frame be the hashline's MIC/EAPOL source? It must carry a real
    MIC, a complete 802.1X payload, AND its embedded nonce (the SNonce hashcat
    reads back) — which is why a zero-nonce M4 is rejected here."""
    i = describe(f)
    return i.has_mic and i.eapol_complete and i.has_nonce


def _mic_assoc_anonce(hs: Handshake, mic_f: EapolFrame, pos: dict):
    """The ANonce of the association the MIC frame actually belongs to, inferred
    from the AP-side frame it answered: an M2 answers the M1 with the same replay
    counter, an M4 answers the M3 with the same replay counter, and that frame
    carries this association's ANonce.

    Reconnect spam makes replay counters collide across associations (the AP
    resets its counter), so several candidates match on replay alone — pick the
    most recent one that PRECEDED the MIC frame in arrival order (``pos``), since
    a response answers the request just before it. Arrival order is used rather
    than timestamps because it survives a saved/round-tripped pcap (which drops
    per-frame timing) and is always present. Returns None when no partner was
    captured, so the caller falls back to the looser replay/window rules."""
    partner_msg = 1 if mic_f.msg_num == 2 else 3
    rc = _replay(mic_f)
    mic_pos = pos[id(mic_f)]
    cands = [f for f in hs.eapol_frames
             if f.msg_num == partner_msg and _replay(f) == rc
             and len(f.nonce) == _NONCE_LEN and f.nonce != _ZERO_NONCE]
    if not cands:
        return None
    before = [f for f in cands if pos[id(f)] < mic_pos]
    chosen = (max(before, key=lambda f: pos[id(f)]) if before
              else min(cands, key=lambda f: abs(pos[id(f)] - mic_pos)))
    return chosen.nonce


def _same_association(hs: Handshake, anonce_f: EapolFrame, mic_f: EapolFrame,
                      pos: dict) -> bool:
    """Reject a pair whose two frames are from different associations even though
    their replay counters line up (the reconnect-spam collision, where the AP
    resets its key-replay counter each attempt):

    1. Arrival order — the 4-way progresses M1→M2→M3→M4, so the lower-numbered
       message is captured first. A higher-numbered frame that arrived BEFORE the
       lower one is a stale leftover from a prior attempt (e.g. an old M3 ahead
       of a fresh M2).
    2. ANonce binding — the MIC frame's own association ANonce (the AP-side frame
       it answered, ``_mic_assoc_anonce``) must equal the ANonce this pair emits.

    Keyed on arrival order (``pos``: index within ``hs.eapol_frames``, which is
    append-in-arrival-order) rather than wall-clock time, so it holds even on a
    round-tripped pcap whose per-frame timestamps aren't preserved."""
    lo, hi = ((anonce_f, mic_f) if anonce_f.msg_num < mic_f.msg_num
              else (mic_f, anonce_f))
    if pos[id(hi)] < pos[id(lo)]:
        return False
    assoc = _mic_assoc_anonce(hs, mic_f, pos)
    if assoc is not None and anonce_f.nonce != assoc:
        return False
    return True


def crackable_pairs(hs: Handshake) -> List[CrackablePair]:
    """Every distinct, hashcat-crackable handshake instance for this client.

    One entry per association (deduped by ANonce); a re-handshake (fresh ANonce)
    adds another. A pair qualifies only when it belongs to one association
    (replay relationship + arrival window + arrival order + the MIC frame's own
    association ANonce — see ``_same_association``) AND its MIC frame is a usable
    keystone (complete M2, or a non-zeroed M4) — i.e. exactly what
    ``hc22000_line`` can serialise. Confidence order favours the M2 keystone.

    Mirrors hcxpcapngtool's extraction: pair on replay counter + a timeout window
    (matched to its 5 s EAPOLTIMEOUT), binding the MIC frame to the request it
    answered by arrival order. The same-association binding matters under
    reconnect spam: the AP resets its key-replay counter per association, so a
    stale M3 from a prior attempt and a fresh M2 collide on replay; pairing them
    would mix nonces from different PTKs into an uncrackable line."""
    by_msg: dict[int, List[EapolFrame]] = {}
    for f in hs.eapol_frames:
        if f.msg_num in (1, 2, 3, 4):
            by_msg.setdefault(f.msg_num, []).append(f)

    out: List[CrackablePair] = []
    seen: set[bytes] = set()
    # Arrival-order index for each frame — the ordering signal _same_association
    # uses (object identity keys, stable within this call).
    pos = {id(f): i for i, f in enumerate(hs.eapol_frames)}

    def consider(anonce_f: EapolFrame, mic_f: EapolFrame,
                 pair_byte: int, rc_ok: bool) -> None:
        if not rc_ok or not _within_window(anonce_f, mic_f):
            return
        if not _mic_frame_usable(mic_f):
            return
        if len(anonce_f.nonce) != _NONCE_LEN or anonce_f.nonce == _ZERO_NONCE:
            return
        if not _same_association(hs, anonce_f, mic_f, pos):
            return
        key = anonce_f.nonce
        if key in seen:
            return
        seen.add(key)
        out.append(CrackablePair(anonce_f, mic_f, pair_byte))

    # M2 keystone first (most reliable), then the conditional M4 keystone.
    for m2 in by_msg.get(2, []):
        for m3 in by_msg.get(3, []):
            consider(m3, m2, _PAIR_M2M3_E2, _replay(m3) == _replay(m2) + 1)
        for m1 in by_msg.get(1, []):
            consider(m1, m2, _PAIR_M1M2_E2, _replay(m1) == _replay(m2))
    for m4 in by_msg.get(4, []):
        for m3 in by_msg.get(3, []):
            consider(m3, m4, _PAIR_M3M4_E4, _replay(m4) == _replay(m3))
        for m1 in by_msg.get(1, []):
            consider(m1, m4, _PAIR_M1M4_E4, _replay(m4) == _replay(m1) + 1)
    return out


# ----- hc22000 emission ------------------------------------------------------

def mac_compact(mac: str) -> str:
    """``aa:bb:cc:dd:ee:ff`` -> ``aabbccddeeff`` (hashcat MAC encoding)."""
    return mac.replace(":", "").replace("-", "").lower()


def ssid_hex(ssid: str) -> str:
    """SSID -> UTF-8 hex (hashcat consumes the raw advertised bytes)."""
    return ssid.encode("utf-8", errors="replace").hex()


def hc22000_line(ssid: str, hs: Handshake, pair: CrackablePair) -> str:
    """The ``WPA*02*…`` hashline for a crackable pair, MIC field zeroed."""
    payload = bytearray(pair.mic_frame.eapol_payload)
    payload[_MIC_OFFSET: _MIC_OFFSET + _MIC_LEN] = _ZERO_MIC
    return (
        "WPA*02"
        f"*{pair.mic_frame.mic.hex()}"
        f"*{mac_compact(hs.bssid)}"
        f"*{mac_compact(hs.client_mac)}"
        f"*{ssid_hex(ssid)}"
        f"*{pair.anonce_frame.nonce.hex()}"
        f"*{bytes(payload).hex()}"
        f"*{pair.pair_byte:02x}"
    )
