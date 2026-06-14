"""Single source of truth for WPA/WPA2 4-way handshake crackability + hc22000.

The "did we capture a handshake?" verdict (events, CAPTURE-panel counts) AND the
hc22000 hashline build both route through ``crackable_pairs()``, so a banner can never
claim a capture that ``save`` then silently refuses — they are literally the same code path.

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

# Coarse wall-clock backstop matching hcxpcapngtool's EAPOLTIMEOUT (5 s): two
# frames whose replay counters line up but that arrived farther apart than this
# aren't paired. The precise binding is arrival order in crackable_pairs; this is
# skipped when timestamps are unset (fixtures / round-tripped pcaps).
_EAPOL_PAIR_WINDOW_S = 5.0

# Nonce-error-correction tolerance (hcxpcapngtool --nonce-error-corrections, 8 by
# default): pair a keystone with a donor whose replay counter is within this many
# of the expected value, then set the NC bit so hashcat brute-forces the small
# nonce drift. Exact (gap 0) is the norm; the tolerance only bridges lossy
# captures / AP retransmits that bumped the counter or the ANonce.
_NC_MAX = 8
# message_pair bit 7 — tells hashcat nonce-error-correction is needed/allowed.
_NC_BIT = 0x80

# ----- AKM crackability (00-0F-AC suite numbers) -----------------------------
# A captured 4-way / PMKID is only worth a passphrase attack if the PMK it
# protects is PSK-derived (PBKDF2). SAE derives its PMK from the live,
# password-authenticated Commit/Confirm exchange — the EAPOL that follows is a
# normal 4-way, but the PMK is ephemeral, so the capture is uncrackable offline.
# EAP (no passphrase) and OWE (no password) are likewise out. There is no
# hashcat passphrase mode for any of them, so we must not emit them as captures.
_PSK_AKMS = frozenset({2, 6, 20})        # PSK / PSK-SHA256 / PSK-SHA384 — plain PBKDF2
_FT_PSK_AKMS = frozenset({4, 19})        # FT-PSK / FT-PSK-SHA384 — PSK-derived, FT hierarchy
_CRACKABLE_AKMS = _PSK_AKMS | _FT_PSK_AKMS
_SAE_AKMS = frozenset({8, 9, 24, 25})    # SAE / FT-SAE / SAE-EXT-KEY / FT-SAE-EXT-KEY


def akm_verdict(hs: Handshake) -> str:
    """Crackability of this capture from its negotiated AKM — one of
    ``"crackable"`` / ``"uncrackable"`` / ``"unknown"``.

    Two inputs, both stamped by the interface: 
      1. ``akm_offered`` (the AP's RSN-IE AKM list, from beacons),
      2. ``akm_client`` (the suite the client actually chose, read from the cleartext RSN IE in EAPOL M2).
         The client's choice is authoritative; the offered list is the fallback.

    SAE gates nothing unless the AP actually offers it — a WPA2/WPA/WEP AP keeps
    its existing purely-structural verdict, so this only ever *removes* SAE
    false-positives, never WPA2 captures:

        no SAE offered                     -> "crackable"   (legacy behavior, untouched)
        client chose a PSK-family suite    -> "crackable"
        client chose SAE/EAP/OWE/…         -> "uncrackable"
        SAE offered, client suite unknown:
            transition (PSK also offered)  -> "unknown"     (can't confirm which side)
            SAE-only                       -> "uncrackable"
    """
    offered = set(hs.akm_offered)
    if not (offered & _SAE_AKMS):
        return "crackable"            # no SAE in play → don't second-guess the structure
    if hs.akm_client is not None:
        return "crackable" if hs.akm_client in _CRACKABLE_AKMS else "uncrackable"
    if offered & _CRACKABLE_AKMS:
        return "unknown"              # transition AP, client's chosen suite not yet seen
    return "uncrackable"              # SAE-only AP — no PSK association is possible


def is_crackable_akm(hs: Handshake) -> bool:
    """True only when the AKM is *confirmed* passphrase-crackable. An
    unconfirmed transition-mode capture (``"unknown"``) counts as NOT crackable:
    we emit/save on positive confirmation only, mirroring hcxpcapngtool's refusal
    to write a hash it can't stand behind."""
    return akm_verdict(hs) == "crackable"


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


def crackable_pairs(hs: Handshake) -> List[CrackablePair]:
    """Every distinct, hashcat-crackable handshake instance for this client,
    extracted the way hcxpcapngtool does.

    Each usable keystone (a complete M2, or a non-zeroed M4) is paired with every
    ANonce donor (M1/M3) whose replay counter is within ``_NC_MAX`` of the
    expected value (the nonce-error-correction tolerance) AND that sits on the
    correct side of it in arrival order — the 4-way runs M1→M2→M3→M4, so the
    lower-numbered message is captured first. Candidates are then taken best-first:
    smallest replay gap, then AP-confirmed ("authorized": M2+M3 / M3+M4) over
    "challenge" (M1+M2 / M1+M4), then the temporally nearest donor. Each keystone
    AND each ANonce is emitted once, so spurious cross-association donors collapse
    to the single best pair while a genuine re-handshake (fresh ANonce) adds
    another. ``hc22000_line`` sets the NC bit, so hashcat fixes whatever small
    nonce drift the tolerance let through.

    Ordering keys on arrival order, not wall-clock time, so the verdict is
    identical live and from a round-tripped pcap; the time window is only a coarse
    backstop when timestamps are present (``_within_window``).

    Gated on the AKM: a SAE/EAP/OWE association still runs a structurally perfect
    4-way, but its PMK isn't passphrase-derived, so there is nothing to crack —
    ``is_crackable_akm`` returns False and we emit no pairs (and thus no banner,
    save, or hashline)."""
    if not is_crackable_akm(hs):
        return []
    pos = {id(f): i for i, f in enumerate(hs.eapol_frames)}
    by_msg: dict[int, List[EapolFrame]] = {1: [], 2: [], 3: [], 4: []}
    for f in hs.eapol_frames:
        if f.msg_num in by_msg:
            by_msg[f.msg_num].append(f)

    # (replay-gap, confidence, arrival-distance, donor, keystone, pair_byte).
    # confidence: 0/2 = AP-confirmed pairs, 1/3 = challenge — lower sorts first.
    cands: list = []
    for mic_f in by_msg[2] + by_msg[4]:
        if not _mic_frame_usable(mic_f):
            continue
        rc = _replay(mic_f)
        if mic_f.msg_num == 2:        # keystone M2: M3 (authorized) or M1 (challenge)
            options = ((by_msg[3], rc + 1, _PAIR_M2M3_E2, 0),
                       (by_msg[1], rc,     _PAIR_M1M2_E2, 1))
        else:                         # keystone M4: M3 (authorized) or M1 (challenge)
            options = ((by_msg[3], rc,     _PAIR_M3M4_E4, 2),
                       (by_msg[1], rc - 1, _PAIR_M1M4_E4, 3))
        for donors, exp_rc, pair_byte, confidence in options:
            for d in donors:
                if len(d.nonce) != _NONCE_LEN or d.nonce == _ZERO_NONCE:
                    continue
                lo, hi = (d, mic_f) if d.msg_num < mic_f.msg_num else (mic_f, d)
                if pos[id(lo)] > pos[id(hi)]:
                    continue          # lower-numbered message must arrive first
                if not _within_window(d, mic_f):
                    continue
                rcgap = abs(_replay(d) - exp_rc)
                if rcgap > _NC_MAX:
                    continue
                dist = abs(pos[id(d)] - pos[id(mic_f)])
                cands.append((rcgap, confidence, dist, d, mic_f, pair_byte))

    cands.sort(key=lambda c: c[:3])
    out: List[CrackablePair] = []
    used_keystones: set[int] = set()
    seen_anonce: set[bytes] = set()
    for _rcgap, _conf, _dist, donor, mic_f, pair_byte in cands:
        if id(mic_f) in used_keystones or donor.nonce in seen_anonce:
            continue
        used_keystones.add(id(mic_f))
        seen_anonce.add(donor.nonce)
        out.append(CrackablePair(donor, mic_f, pair_byte))
    return out


# ----- hc22000 emission ------------------------------------------------------

def mac_compact(mac: str) -> str:
    """``aa:bb:cc:dd:ee:ff`` -> ``aabbccddeeff`` (hashcat MAC encoding)."""
    return mac.replace(":", "").replace("-", "").lower()


def ssid_hex(ssid: str) -> str:
    """SSID -> UTF-8 hex (hashcat consumes the raw advertised bytes)."""
    return ssid.encode("utf-8", errors="replace").hex()


def hc22000_line(ssid: str, hs: Handshake, pair: CrackablePair) -> str:
    """The ``WPA*02*…`` hashline for a crackable pair, MIC field zeroed.

    The message-pair byte carries the NC bit (0x80), so hashcat applies
    nonce-error-correction by default (it's off unless this bit is set) and
    fixes the small ANonce/replay drift the pairing tolerated — matching how
    hcxpcapngtool flags its output."""
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
        f"*{pair.pair_byte | _NC_BIT:02x}"
    )
