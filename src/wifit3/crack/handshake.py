"""Single source of truth for WPA/WPA2 4-way handshake crackability + hc22000.

The "did we capture a handshake?" verdict AND the hc22000 hashline build both route
through ``crackable_pairs()``, so a banner can never claim a capture that ``save``
then silently refuses: they are literally the same code path.

Ground truth: what each 4-way message carries and what hashcat needs
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
(SNonce present) or M4 (only when the client didn't zero its nonce), never M3
(its nonce is the ANonce) and never M1 (no MIC). hashcat's MESSAGEPAIR table:

    0x00  M1+M2, EAPOL from M2   always crackable (M2 complete)
    0x02  M2+M3, EAPOL from M2   always crackable (M2 complete; M3 = ANonce)
    0x05  M3+M4, EAPOL from M4   only if M4's nonce != 0 (echoed SNonce)
    0x01  M1+M4, EAPOL from M4   only if M4's nonce != 0
    0x03/0x04 (EAPOL from M3)    marked "unused" by hashcat, never emitted here

So a "captured handshake" requires a usable *keystone*: a complete M2 (or, rarely,
a complete M4 with a non-zero nonce) plus an ANonce donor (M1 or M3) from the
same association.

References:
  - hashcat 22000 hashline format + MESSAGEPAIR semantics:
    https://github.com/hashcat/hashcat/issues/1816
    https://github.com/hashcat/hashcat/blob/master/src/modules/module_22000.c
  - Pairing + tolerance heuristics mirrored from hcxpcapngtool (EAPOLTIMEOUT 5 s,
    --nonce-error-corrections 8, the MESSAGEPAIR table):
    https://github.com/ZerBea/hcxtools
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from wifit3.models import HandshakeMessage, Handshake

# hashcat module_22000 MESSAGEPAIR bytes (EAPOL-source encoded in the low bits).
_PAIR_M1M2_E2 = 0x00
_PAIR_M1M4_E4 = 0x01
_PAIR_M2M3_E2 = 0x02
_PAIR_M3M4_E4 = 0x05

_KEY_INFO_OFFSET = 5

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
# message_pair bit 7: tells hashcat nonce-error-correction is needed/allowed.
_NC_BIT = 0x80

# ----- AKM crackability (00-0F-AC suite numbers) -----------------------------
# A capture is worth emitting only if hashcat -m 22000 can crack the *specific
# artifact*: the PMK must be passphrase-derived (PBKDF2) AND hashcat must support
# the resulting hashline. SAE (ephemeral PMK), EAP (PMK from the 802.1X MSK, no
# passphrase), OWE (no password) fail the first test. FT-PSK-SHA384 passes it but
# fails the second (-m 22000's FT path is the SHA-256 KDF only), so the artifacts
# gate differently:
#
#   EAPOL: hashcat reads the Key Descriptor Version from the embedded frame, so plain
#     PSK (keyver 2) AND PSK-SHA256 (keyver 3 / AES-CMAC) both crack as WPA*02.
#     FT-PSK (AKM 4) cracks as WPA*04, which needs an M2 of keyver 3 carrying the
#     MDID, R0KH-ID and R1KH-ID; any other FT-PSK pair is withheld as "FT".
#     → uncrackable AKMs = SAE | FT-PSK-SHA384 | EAP | OWE.
#   PMKID: plain PSK (AKM 2, HMAC-SHA1) and PSK-SHA256 (AKM 6, HMAC-SHA256, flagged
#     by MESSAGEPAIR "03") as WPA*01; FT-PSK (AKM 4, PMKR1Name) as WPA*03, once an M2
#     of the same client supplied the three FT identifiers.
#
# SAE, FT, EAP, and OWE are *strict* (withhold on an unconfirmed transition AP); an
# FT-PSK capture also needs its AKM known, since that picks WPA*04 over WPA*02.
# A PMKID of unknown AKM takes the algorithm from its M1's key descriptor version
# (3 = SHA256, 2 = SHA1); failing that, SHA1 unless the AP offers no plain PSK.
_SAE_AKMS = frozenset({8, 9, 24, 25})       # SAE / FT-SAE / SAE-EXT-KEY / FT-SAE-EXT-KEY
FT_PSK_AKM = 4
_FT_PSK_SHA384_AKM = 19
_FT_PSK_AKMS = frozenset({FT_PSK_AKM, _FT_PSK_SHA384_AKM})
_EAP_AKMS = frozenset({1, 3, 5, 11, 12, 13})  # EAP/FT-EAP/EAP-SHA256/EAP-Suite-B[-192]/FT-EAP-SHA384 (Enterprise)
_OWE_AKMS = frozenset({18})                 # OWE / Enhanced Open: PMK from ECDH, no passphrase
_EAPOL_BAD_AKMS = _SAE_AKMS | {_FT_PSK_SHA384_AKM} | _EAP_AKMS | _OWE_AKMS  # never -m 22000-crackable
_FT_KEY_DESCRIPTOR_VERSION = 3              # hashcat rejects a WPA*04 of any other keyver
_FT_KEY_HOLDER_ID_MAX_LEN = 48
_PMKID_PSK_AKM = 2                          # HMAC-SHA1 PMKID
PMKID_PSK_SHA256_AKM = 6                    # HMAC-SHA256 PMKID
_PMKID_PSK_AKMS = frozenset({_PMKID_PSK_AKM, PMKID_PSK_SHA256_AKM})
_PMKID_AKM_BY_KEY_DESCRIPTOR_VERSION = {2: _PMKID_PSK_AKM, 3: PMKID_PSK_SHA256_AKM}
_EAP_LABEL = "EAP/Enterprise"               # badge for a captured-but-worthless EAP 4-way
_OWE_LABEL = "OWE"                          # badge for a captured-but-worthless OWE (open) 4-way


def eapol_verdict(akm: Optional[int], offered: List[int]) -> str:
    """EAPOL crackability for a negotiated ``akm``, falling back to the AP's ``offered``
    suites when the frame's own AKM is unknown (SAE/FT/EAP/OWE are uncrackable then)."""
    off = set(offered)
    if akm is not None:
        return "uncrackable" if akm in _EAPOL_BAD_AKMS else "crackable"
    bad = _EAPOL_BAD_AKMS | _FT_PSK_AKMS
    if not (off & bad):
        return "crackable"            # no SAE/FT/EAP/OWE offered, akm unknown → legacy emit
    if off - bad:
        return "unknown"              # bad + a non-bad AKM offered, akm unknown
    return "uncrackable"              # only SAE/FT/EAP/OWE offered


def crackable_pmkid_akm(hs: Handshake) -> Optional[int]:
    """The AKM the PMKID was derived under when hashcat ``-m 22000`` cracks it: 2 (PSK,
    HMAC-SHA1), 6 (PSK-SHA256, HMAC-SHA256) or 4 (FT-PSK). None when uncrackable."""
    if hs.pmkid_akm == FT_PSK_AKM:
        return FT_PSK_AKM if ft_identifiers_frame(hs) is not None else None
    if hs.pmkid_akm is not None:
        return hs.pmkid_akm if hs.pmkid_akm in _PMKID_PSK_AKMS else None
    offered = set(hs.akm_offered)
    if offered & (_SAE_AKMS | _FT_PSK_AKMS):
        return None                   # SAE/FT could be the negotiated AKM → withhold
    psk_offered = offered & _PMKID_PSK_AKMS if offered else _PMKID_PSK_AKMS  # none: fixtures / pre-RSN
    if not psk_offered:
        return None
    m1_akm = _PMKID_AKM_BY_KEY_DESCRIPTOR_VERSION.get(hs.pmkid_key_descriptor_version)
    if m1_akm in psk_offered:
        return m1_akm                 # the M1's key descriptor version, as hcxpcapngtool reads it
    return _PMKID_PSK_AKM if _PMKID_PSK_AKM in psk_offered else PMKID_PSK_SHA256_AKM


def pmkid_crackable(hs: Handshake) -> bool:
    """True for a PSK (AKM 2), PSK-SHA256 (AKM 6) or complete FT-PSK (AKM 4) PMKID."""
    return crackable_pmkid_akm(hs) is not None


def uncrackable_label(akm: Optional[int], offered: List[int]) -> Optional[str]:
    """Short reason an EAPOL capture is uncrackable (``"EAP/Enterprise"`` / ``"OWE"``
    / ``"FT"`` / ``"SAE"``), or None when it *is* crackable/unknown."""
    if eapol_verdict(akm, offered) != "uncrackable":
        return None
    if akm is not None:
        if akm in _EAP_AKMS:
            return _EAP_LABEL
        if akm in _OWE_AKMS:
            return _OWE_LABEL
        return "FT" if akm in _FT_PSK_AKMS else "SAE"
    # AKM unknown → reached only when every offered AKM is uncrackable, so
    # name it by the dominant family.
    off = set(offered)
    if off & _EAP_AKMS and not (off & (_SAE_AKMS | _FT_PSK_AKMS | _OWE_AKMS)):
        return _EAP_LABEL
    if off & _OWE_AKMS and not (off & (_SAE_AKMS | _FT_PSK_AKMS | _EAP_AKMS)):
        return _OWE_LABEL
    if off & _FT_PSK_AKMS and not (off & _SAE_AKMS):
        return "FT"
    return "SAE"


@dataclass(frozen=True)
class MessageInfo:
    """Per-frame content descriptor: the hashcat-relevant fields, for logging.

    ``useful`` answers "does this frame contribute what a crackable pair needs?"
    True unless the frame arrived degraded (e.g. a clipped M2)."""
    msg_num: int
    has_nonce: bool       # a real 32-byte, non-zero nonce
    has_mic: bool         # a real 16-byte, non-zero MIC (M1 legitimately has none)
    eapol_complete: bool  # 802.1X payload reaches through the MIC (>= 97 bytes)

    @property
    def useful(self) -> bool:
        if self.msg_num == 1:   # ANonce donor
            return self.has_nonce
        if self.msg_num == 2:   # keystone: SNonce + MIC + complete EAPOL
            return self.has_nonce and self.has_mic and self.eapol_complete
        if self.msg_num == 3:   # ANonce donor (own MIC/EAPOL unused by hashcat)
            return self.has_nonce
        if self.msg_num == 4:   # conditional keystone: needs an echoed SNonce too
            return self.has_mic and self.eapol_complete and self.has_nonce
        return False


def describe(f: HandshakeMessage) -> MessageInfo:
    """Content descriptor for one EAPOL frame."""
    return MessageInfo(
        msg_num=f.msg_num,
        has_nonce=len(f.nonce) == _NONCE_LEN and f.nonce != _ZERO_NONCE,
        has_mic=len(f.mic) == _MIC_LEN and f.mic != _ZERO_MIC,
        eapol_complete=len(f.eapol_payload) >= _FULL_EAPOL,
    )


@dataclass(frozen=True)
class CrackablePair:
    """A pair that yields a hashcat-crackable WPA*02 or WPA*04 line.

    ``anonce_frame`` donates the ANonce; ``mic_frame`` (always M2 or a non-zeroed
    M4) donates the MIC, the SNonce, and the EAPOL bytes."""
    anonce_frame: HandshakeMessage
    mic_frame: HandshakeMessage
    pair_byte: int

    @property
    def instance_key(self) -> bytes:
        """ANonce: fresh per association, so it identifies one 4-way instance."""
        return self.anonce_frame.nonce


def _replay(f: HandshakeMessage) -> int:
    return int.from_bytes(bytes.fromhex(f.replay_hex), "big")


def _within_window(a: HandshakeMessage, b: HandshakeMessage) -> bool:
    """True if two frames are close enough in time to be one handshake. Skipped
    when either timestamp is unset (0.0), keeps fixtures / pre-timestamp
    captures working off the replay-counter rules alone."""
    if a.timestamp <= 0 or b.timestamp <= 0:
        return True
    return abs(a.timestamp - b.timestamp) <= _EAPOL_PAIR_WINDOW_S


def _mic_frame_usable(f: HandshakeMessage) -> bool:
    """Can this frame be the hashline's MIC/EAPOL source? It must carry a real
    MIC, a complete 802.1X payload, AND its embedded nonce (the SNonce hashcat
    reads back), which is why a zero-nonce M4 is rejected here."""
    i = describe(f)
    return i.has_mic and i.eapol_complete and i.has_nonce


def _ft_identifiers_complete(f: HandshakeMessage) -> bool:
    return (len(f.mdid) == 2 and 0 < len(f.r0khid) <= _FT_KEY_HOLDER_ID_MAX_LEN
            and 0 < len(f.r1khid) <= _FT_KEY_HOLDER_ID_MAX_LEN)


def ft_identifiers_frame(hs: Handshake) -> Optional[HandshakeMessage]:
    """The first frame carrying a complete MDID / R0KH-ID / R1KH-ID, or None."""
    return next((f for f in hs.messages if _ft_identifiers_complete(f)), None)


def _ft_pair_incomplete(pair: CrackablePair) -> bool:
    """An FT-PSK pair WPA*04 can't carry: its keystone must be an M2 of keyver 3 with all
    three FT identifiers."""
    f = pair.mic_frame
    if f.akm != FT_PSK_AKM:
        return False
    key_info = int.from_bytes(f.eapol_payload[_KEY_INFO_OFFSET:_KEY_INFO_OFFSET + 2], "big")
    return not (f.msg_num == 2 and key_info & 3 == _FT_KEY_DESCRIPTOR_VERSION
                and _ft_identifiers_complete(f))


def crackable_pairs(hs: Handshake) -> List[CrackablePair]:
    """Every hashcat-crackable handshake instance: the structural pairs of
    ``_pairs_ignoring_akm`` kept only when the keystone (M2/M4) frame's AKM cracks."""
    offered = hs.akm_offered
    return [p for p in _pairs_ignoring_akm(hs)
            if eapol_verdict(p.mic_frame.akm, offered) == "crackable"
            and not _ft_pair_incomplete(p)]


def withheld_capture_label(hs: Handshake) -> Optional[str]:
    """Badge (SAE/FT/EAP/OWE) for the first captured 4-way instance whose AKM has no
    ``-m 22000`` crack path, or None. Per instance (a mixed capture still names it)."""
    for p in _pairs_ignoring_akm(hs):
        if _ft_pair_incomplete(p):
            return "FT"
        label = uncrackable_label(p.mic_frame.akm, hs.akm_offered)
        if label:
            return label
    return None


def handshake_uncrackable_label(hs: Handshake) -> Optional[str]:
    """Badge (SAE/FT/EAP/OWE) for this handshake's negotiated AKM, from its frames (no
    complete pair required, unlike withheld_capture_label), or None if crackable/unknown."""
    akm = next((f.akm for f in reversed(hs.messages) if f.akm is not None), None)
    return uncrackable_label(akm, hs.akm_offered)


def _pairs_ignoring_akm(hs: Handshake) -> List[CrackablePair]:
    """The pairing algorithm alone, *before* the AKM crackability gate, extracted
    the way hcxpcapngtool does. Callers that want the real crackable set must use
    ``crackable_pairs`` (which applies the AKM gate); this exists so the UI can
    tell 'a usable 4-way arrived but its AKM is worthless' from 'no 4-way yet'.

    Each usable keystone (a complete M2, or a non-zeroed M4) is paired with every
    ANonce donor (M1/M3) whose replay counter is within ``_NC_MAX`` of the
    expected value (the nonce-error-correction tolerance) AND that sits on the
    correct side of it in arrival order: the 4-way runs M1→M2→M3→M4, so the
    lower-numbered message is captured first. Candidates are then taken best-first:
    smallest replay gap, then AP-confirmed ("authorized": M2+M3 / M3+M4) over
    "challenge" (M1+M2 / M1+M4), then the temporally nearest donor. Each keystone
    AND each ANonce is emitted once, so spurious cross-association donors collapse
    to the single best pair while a genuine re-handshake (fresh ANonce) adds
    another."""
    pos = {id(f): i for i, f in enumerate(hs.messages)}
    by_msg: dict[int, List[HandshakeMessage]] = {1: [], 2: [], 3: [], 4: []}
    for f in hs.messages:
        if f.msg_num in by_msg:
            by_msg[f.msg_num].append(f)

    # (replay-gap, confidence, arrival-distance, donor, keystone, pair_byte).
    # confidence: 0/2 = AP-confirmed pairs, 1/3 = challenge, lower sorts first.
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
    """The ``WPA*02*…`` hashline for a crackable pair (``WPA*04*…`` plus MDID, R0KH-ID
    and R1KH-ID for FT-PSK), MIC field zeroed.

    The message-pair byte carries the NC bit (0x80), so hashcat applies
    nonce-error-correction by default (it's off unless this bit is set) and
    fixes the small ANonce/replay drift the pairing tolerated, matching how
    hcxpcapngtool flags its output."""
    payload = bytearray(pair.mic_frame.eapol_payload)
    payload[_MIC_OFFSET: _MIC_OFFSET + _MIC_LEN] = _ZERO_MIC
    f = pair.mic_frame
    is_ft = f.akm == FT_PSK_AKM
    line = (
        f"WPA*{'04' if is_ft else '02'}"
        f"*{f.mic.hex()}"
        f"*{mac_compact(hs.bssid)}"
        f"*{mac_compact(hs.client_mac)}"
        f"*{ssid_hex(ssid)}"
        f"*{pair.anonce_frame.nonce.hex()}"
        f"*{bytes(payload).hex()}"
        f"*{pair.pair_byte | _NC_BIT:02x}"
    )
    if is_ft:
        line += f"*{f.mdid.hex()}*{f.r0khid.hex()}*{f.r1khid.hex()}"
    return line
