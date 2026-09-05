"""802.11 frame model: parse the identifying IEs of a management frame into a diffable structure.

This is the curated fingerprint layer: only IEs in the `IE` (and, for element 255, `ExtIE`)
allowlist are kept; each is
decoded to a plain Python value (bytes / str / list / dict / a labeled enum). Two consequences fall
out for free:

  * diff is `==` on the values (set ops on the IE keys for add/remove), and
  * render walks the values with one recursive formatter, no per-IE special casing.

Values are a recursive union of those plain types, so a nested IE is just a decoder that calls
`parse_ies` again and returns a nested dict; render and diff handle any depth without new machinery.

The allowlist lives in one place: to keep an IE, add a member to `IE`/`ExtIE`; to drop it, leave it
out. Decoders are lossless (they never map distinct bytes to equal values) so a diff can't miss a
change; lossy prettifying (bytes as text, zero squeeze) happens only in `fmt`, at render time.
"""
from __future__ import annotations

import sys
from enum import Enum, Flag, KEEP, unique
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wifit3.dot11.wsc.messages import (
    ATTR_DEV_NAME, ATTR_MANUFACTURER, ATTR_MODEL_NAME, ATTR_MODEL_NUMBER,
    ATTR_PRIMARY_DEV_TYPE, ATTR_SERIAL_NUMBER, parse_tlvs,
)
from wifit3.wlan.fingerprint_vendors import VENDOR_BY_OUI


# ======================================================================================
# Vocabularies: an Enum whose value is the numeric id and that carries a display label.
# Plain Enum (not IntEnum) on purpose: members of different vocabularies must never compare
# equal, and nothing does arithmetic on them (use `.value` for the id). Never format a member
# except via `.label` (bare str()/f-string gives "IE.SSID", not the label). `.of(id)` returns
# the member or None; membership is the keep/drop allowlist.
# ======================================================================================
class Labeled(Enum):
    """Base for id+label enums. Member-less, so it can be subclassed."""
    def __new__(cls, value: int, label: str) -> "Labeled":
        obj = object.__new__(cls)
        obj._value_ = value
        obj.label = label
        return obj

    @classmethod
    def of(cls, ident: int) -> "Labeled | None":
        try:
            return cls(ident)
        except ValueError:
            return None


@unique
class IE(Labeled):
    """802.11 element ids we keep. Absent id -> dropped by the parser. (Element 255 is its own
    namespace: see ExtIE.)"""
    SSID = (0, "SSID")
    SUPP_RATES = (1, "SuppRates")
    HT_CAPS = (45, "HTCaps")
    RSN = (48, "RSN")
    EXT_SUPP_RATES = (50, "ExtSuppRates")
    EXT_CAPS = (127, "ExtCaps")
    VHT_CAPS = (191, "VHTCaps")
    VENDOR = (221, "Vendor")


@unique
class ExtIE(Labeled):
    """Element ID Extension (element 255) sub-ids we keep. The 6GHz / HE / EHT generation lives
    here, so it needs its own allowlist rather than sharing IE's single-byte id space."""
    HE_CAPS = (35, "HECaps")
    HE_OP = (36, "HEOp")


@unique
class Cipher(Labeled):
    """RSN cipher suite types (standard OUI 00-0f-ac)."""
    USE_GROUP = (0, "UseGroup")
    WEP40 = (1, "WEP40")
    TKIP = (2, "TKIP")
    CCMP = (4, "CCMP")
    WEP104 = (5, "WEP104")
    GCMP128 = (8, "GCMP128")
    GCMP256 = (9, "GCMP256")
    CCMP256 = (10, "CCMP256")


@unique
class Akm(Labeled):
    """RSN authentication and key-management suite types (standard OUI 00-0f-ac)."""
    DOT1X = (1, "802.1X")
    PSK = (2, "PSK")
    FT_DOT1X = (3, "FT-802.1X")
    FT_PSK = (4, "FT-PSK")
    DOT1X_SHA256 = (5, "802.1X-SHA256")
    PSK_SHA256 = (6, "PSK-SHA256")
    SAE = (8, "SAE")
    FT_SAE = (9, "FT-SAE")
    OWE = (18, "OWE")


# ======================================================================================
# Bitfields: a Flag per capability field. Plain Flag (not IntFlag) for the same reason the
# vocabularies are Enum not IntEnum: IntFlag members are ints, so they would compare equal across
# different flag classes and to raw ints, the cross-vocabulary false equality this design avoids.
# boundary=KEEP keeps unknown/reserved bits in .value (this Python defaults Flag to STRICT, which
# would raise), so a decode is lossless: it round-trips and a change in an unnamed bit still != .
# ======================================================================================
@unique
class HTCap(Flag, boundary=KEEP):
    """HT Capabilities Info (IE 45, first 2 bytes, little endian) single bits. The two wider
    subfields (SM Power Save 2-3, Rx STBC 8-9) are decoded as ints in _htcaps; bit 13 is Reserved
    but named so a vendor lighting it up surfaces by name, not as an anonymous residual."""
    LDPC = 0x0001
    CH_WIDTH_40 = 0x0002
    GREENFIELD = 0x0010
    SGI_20 = 0x0020
    SGI_40 = 0x0040
    TX_STBC = 0x0080
    DELAYED_BA = 0x0400
    MAX_AMSDU = 0x0800
    DSSS_CCK_40 = 0x1000
    RESERVED_13 = 0x2000
    FORTY_INTOLERANT = 0x4000
    LSIG_TXOP_PROT = 0x8000


@unique
class VHTCap(Flag, boundary=KEEP):
    """VHT Capabilities Info (IE 191, first 4 bytes, little endian) single bits. Every wider
    subfield (max MPDU length, channel width, Rx STBC, beamformee STS, sounding dims, max A-MPDU
    exponent, link adaptation, ext NSS BW) is an int in _vhtcaps; the field has no reserved bits."""
    RXLDPC = 0x00000010
    SGI_80 = 0x00000020
    SGI_160 = 0x00000040
    TX_STBC = 0x00000080
    SU_BEAMFORMER = 0x00000800
    SU_BEAMFORMEE = 0x00001000
    MU_BEAMFORMER = 0x00080000
    MU_BEAMFORMEE = 0x00100000
    TXOP_PS = 0x00200000
    HTC_VHT = 0x00400000
    RX_ANTENNA_PATTERN = 0x10000000
    TX_ANTENNA_PATTERN = 0x20000000


@unique
class ExtCap(Flag, boundary=KEEP):
    """Extended Capabilities (IE 127, variable length, little endian over all bytes). Every spec
    defined single bit through bit 79 is named, reserved ones as RESERVED_n so vendor use surfaces
    by name. The two wider subfields (Service Interval Granularity 41-43, Max MSDUs in A-MSDU
    63-64) are ints in _extcaps; KEEP holds any bit past bit 79."""
    TWENTY_FORTY_BSS_COEX = 1 << 0
    RESERVED_1 = 1 << 1
    EXT_CHANNEL_SWITCH = 1 << 2
    RESERVED_3 = 1 << 3
    PSMP = 1 << 4
    RESERVED_5 = 1 << 5
    S_PSMP = 1 << 6
    EVENT = 1 << 7
    DIAGNOSTICS = 1 << 8
    MULTICAST_DIAGNOSTICS = 1 << 9
    LOCATION_TRACKING = 1 << 10
    FMS = 1 << 11
    PROXY_ARP = 1 << 12
    COLLOCATED_INTERFERENCE = 1 << 13
    CIVIC_LOCATION = 1 << 14
    GEOSPATIAL_LOCATION = 1 << 15
    TFS = 1 << 16
    WNM_SLEEP_MODE = 1 << 17
    TIM_BROADCAST = 1 << 18
    BSS_TRANSITION = 1 << 19
    QOS_TRAFFIC_CAP = 1 << 20
    AC_STATION_COUNT = 1 << 21
    MULTIPLE_BSSID = 1 << 22
    TIMING_MEASUREMENT = 1 << 23
    CHANNEL_USAGE = 1 << 24
    SSID_LIST = 1 << 25
    DMS = 1 << 26
    UTC_TSF_OFFSET = 1 << 27
    TDLS_PEER_UAPSD_BUFFER_STA = 1 << 28
    TDLS_PEER_PSM = 1 << 29
    TDLS_CHANNEL_SWITCHING = 1 << 30
    INTERWORKING = 1 << 31
    QOS_MAP = 1 << 32
    EBR = 1 << 33
    SSPN_INTERFACE = 1 << 34
    RESERVED_35 = 1 << 35
    MSGCF = 1 << 36
    TDLS = 1 << 37
    TDLS_PROHIBITED = 1 << 38
    TDLS_CH_SW_PROHIBITED = 1 << 39
    REJECT_UNADMITTED_FRAME = 1 << 40
    IDENTIFIER_LOCATION = 1 << 44
    UAPSD_COEXISTENCE = 1 << 45
    WNM_NOTIFICATION = 1 << 46
    QAB = 1 << 47
    UTF8_SSID = 1 << 48
    QMF_ACTIVATED = 1 << 49
    QMF_RECONFIG_ACTIVATED = 1 << 50
    ROBUST_AV_STREAMING = 1 << 51
    ADVANCED_GCR = 1 << 52
    MESH_GCR = 1 << 53
    SCS = 1 << 54
    QLOAD_REPORT = 1 << 55
    ALTERNATE_EDCA = 1 << 56
    UNPROTECTED_TXOP_NEG = 1 << 57
    PROTECTED_TXOP_NEG = 1 << 58
    RESERVED_59 = 1 << 59
    PROTECTED_QLOAD_REPORT = 1 << 60
    TDLS_WIDER_BANDWIDTH = 1 << 61
    OP_MODE_NOTIF = 1 << 62
    CHANNEL_SCHEDULE_MGMT = 1 << 65
    GEODB_INBAND_ENABLING = 1 << 66
    NETWORK_CHANNEL_CONTROL = 1 << 67
    WHITE_SPACE_MAP = 1 << 68
    CHANNEL_AVAIL_QUERY = 1 << 69
    FTM_RESPONDER = 1 << 70
    FTM_INITIATOR = 1 << 71
    FILS_CAPABILITY = 1 << 72
    EXT_SPECTRUM_MGMT = 1 << 73
    FUTURE_CHANNEL_GUIDANCE = 1 << 74
    RESERVED_75 = 1 << 75
    RESERVED_76 = 1 << 76
    TWT_REQUESTER = 1 << 77
    TWT_RESPONDER = 1 << 78
    OBSS_NARROW_BW_RU_TOLERANCE = 1 << 79


@unique
class HeMacCap(Flag, boundary=KEEP):
    """HE MAC Capabilities Information (ExtIE 35, first 6 bytes, little endian) single bits. The
    wider subfields (dynamic frag, fragment counts, TID aggregation, link adaptation, A-MPDU
    exponent) are ints in _hecaps; bit 24 is Reserved but named."""
    HTC_HE = 1 << 0
    TWT_REQUESTER = 1 << 1
    TWT_RESPONDER = 1 << 2
    ALL_ACK = 1 << 17
    TRS = 1 << 18
    BSR = 1 << 19
    BCAST_TWT = 1 << 20
    THIRTYTWO_BIT_BA_BITMAP = 1 << 21
    MU_CASCADING = 1 << 22
    ACK_EN = 1 << 23
    RESERVED_24 = 1 << 24
    OMI_CONTROL = 1 << 25
    OFDMA_RA = 1 << 26
    AMSDU_FRAG = 1 << 29
    FLEX_TWT_SCHED = 1 << 30
    RX_CTRL_FRAME_TO_MULTIBSS = 1 << 31
    BSRP_BQRP_A_MPDU_AGG = 1 << 32
    QTP = 1 << 33
    BQR = 1 << 34
    PSR_RESP = 1 << 35
    NDP_FB_REP = 1 << 36
    OPS = 1 << 37
    AMSDU_IN_AMPDU = 1 << 38
    SUBCHAN_SELECTIVE_TRANSMISSION = 1 << 42
    UL_2X996_TONE_RU = 1 << 43
    OM_CTRL_UL_MU_DATA_DIS_RX = 1 << 44
    HE_DYNAMIC_SM_PS = 1 << 45
    PUNCTURED_SOUNDING = 1 << 46
    HT_VHT_TRIGGER_FRAME_RX = 1 << 47


@unique
class HePhyCap(Flag, boundary=KEEP):
    """HE PHY Capabilities Information (ExtIE 35, the 11 bytes after HE MAC, little endian) single
    bits. The wider subfields (channel width set, preamble punct, DCM, beamformee STS / sounding
    dims, max Nc, packet padding, ...) are ints in _hecaps; bit 0 and bits 81-87 are Reserved."""
    RESERVED_0 = 1 << 0
    DEVICE_CLASS = 1 << 12
    LDPC_CODING_IN_PAYLOAD = 1 << 13
    HE_SU_PPDU_1X_LTF_08US_GI = 1 << 14
    NDP_4X_LTF_AND_3_2US = 1 << 17
    STBC_TX_UNDER_80MHZ = 1 << 18
    STBC_RX_UNDER_80MHZ = 1 << 19
    DOPPLER_TX = 1 << 20
    DOPPLER_RX = 1 << 21
    UL_MU_FULL_MU_MIMO = 1 << 22
    UL_MU_PARTIAL_MU_MIMO = 1 << 23
    RX_PARTIAL_BW_SU_IN_20MHZ_MU = 1 << 30
    SU_BEAMFORMER = 1 << 31
    SU_BEAMFORMEE = 1 << 32
    MU_BEAMFORMER = 1 << 33
    NG16_SU_FEEDBACK = 1 << 46
    NG16_MU_FEEDBACK = 1 << 47
    CODEBOOK_SIZE_42_SU = 1 << 48
    CODEBOOK_SIZE_75_MU = 1 << 49
    TRIG_SU_BEAMFORMING_FB = 1 << 50
    TRIG_MU_BEAMFORMING_PARTIAL_BW_FB = 1 << 51
    TRIG_CQI_FB = 1 << 52
    PARTIAL_BW_EXT_RANGE = 1 << 53
    PARTIAL_BANDWIDTH_DL_MUMIMO = 1 << 54
    PPE_THRESHOLD_PRESENT = 1 << 55
    PSR_BASED_SR = 1 << 56
    POWER_BOOST_FACTOR_SUPP = 1 << 57
    HE_SU_MU_PPDU_4X_LTF_08US_GI = 1 << 58
    STBC_TX_ABOVE_80MHZ = 1 << 62
    STBC_RX_ABOVE_80MHZ = 1 << 63
    HE_ER_SU_PPDU_4X_LTF_08US_GI = 1 << 64
    PPDU_20MHZ_IN_40MHZ_2G = 1 << 65
    PPDU_20MHZ_IN_160MHZ = 1 << 66
    PPDU_80MHZ_IN_160MHZ = 1 << 67
    HE_ER_SU_1X_LTF_08US_GI = 1 << 68
    MIDAMBLE_RX_TX_2X_AND_1X_LTF = 1 << 69
    LONGER_THAN_16_SIGB_OFDM_SYM = 1 << 72
    NON_TRIGGERED_CQI_FEEDBACK = 1 << 73
    TX_1024_QAM_LESS_THAN_242_TONE_RU = 1 << 74
    RX_1024_QAM_LESS_THAN_242_TONE_RU = 1 << 75
    RX_FULL_BW_SU_MU_COMP_SIGB = 1 << 76
    RX_FULL_BW_SU_MU_NON_COMP_SIGB = 1 << 77
    HE_MU_M1RU_MAX_LTF = 1 << 80


@unique
class HeOpParams(Flag, boundary=KEEP):
    """HE Operation Parameters (ExtIE 36, first 3 bytes, little endian) single bits. default_pe_
    duration and txop_dur_rts_threshold are ints in _heop; the three presence bits select the
    optional trailing fields. Reserved bits 18-23 stay in the KEEP residual."""
    TWT_REQUIRED = 1 << 3
    VHT_OPER_INFO_PRESENT = 1 << 14
    CO_HOSTED_BSS = 1 << 15
    ER_SU_DISABLE = 1 << 16
    SIX_GHZ_OP_INFO_PRESENT = 1 << 17


@unique
class HeBssColor(Flag, boundary=KEEP):
    """BSS Color Information (ExtIE 36, the byte after HE Operation Parameters). bss_color is a
    6-bit int in _heop; these are the two flag bits."""
    PARTIAL_BSS_COLOR = 1 << 6
    BSS_COLOR_DISABLED = 1 << 7


_WSC_NAMES = {
    ATTR_MANUFACTURER: "mfr", ATTR_MODEL_NAME: "model", ATTR_MODEL_NUMBER: "model_no",
    ATTR_DEV_NAME: "name", ATTR_SERIAL_NUMBER: "serial", ATTR_PRIMARY_DEV_TYPE: "dev_type",
}
_WPS = b"\x00\x50\xf2\x04"   # Vendor Specific OUI+type carrying WSC attributes
_WPA = b"\x00\x50\xf2\x01"   # Vendor Specific OUI+type carrying the pre-RSN WPA element
_WMM = b"\x00\x50\xf2\x02"   # Vendor Specific OUI+type carrying WMM/WME parameters
_P2P = b"\x50\x6f\x9a\x09"   # Wi-Fi Direct (P2P) attributes
_HS20 = b"\x50\x6f\x9a\x10"  # Hotspot 2.0 Indication
_MBO = b"\x50\x6f\x9a\x16"   # MBO-OCE attributes
# Well-known vendor OUI+type (4 bytes) -> short label, shown alongside the numeric type.
_VENDOR_TYPES = {
    _WPA: "WPA", _WMM: "WMM", _WPS: "WPS", _P2P: "P2P", _HS20: "HS20", _MBO: "MBO",
}


# ======================================================================================
# Decoders: one IE's bytes -> a lossless plain value. Raw IEs return bytes; Mixed IEs return a
# dict (fields may be labeled enums, or further nested dicts/lists). `decode` routes an IE to
# its decoder; anything without one is kept raw. Decoders are total: they never raise on short
# or crafted input, they bound their reads.
# ======================================================================================
def _rates(v: bytes) -> list[str]:
    """Rate bytes to Mbps strings, 'b' marking a basic (mandatory) rate."""
    return [f"{(b & 0x7f) / 2:g}" + ("b" if b & 0x80 else "") for b in v]


def _suite(b: bytes, kind: type[Labeled], oui: bytes = b"\x00\x0f\xac"):
    """A 4-byte cipher/AKM suite (OUI + type) to its enum member, the raw type int for an unknown
    suite under `oui`, or the raw bytes for any other OUI. RSN uses 00-0f-ac, WPA uses 00-50-f2."""
    if len(b) == 4 and b[:3] == oui:
        return kind.of(b[3]) or b[3]
    return b


def _cipher_suites(v: bytes, oui: bytes) -> dict:
    """The shared RSN / WPA body -> {group, pair, akm, caps?}, ciphers/AKMs as labeled enums under
    `oui`. Counts are bounded to the bytes present, so a truncated or crafted element builds no junk.
    Both elements start version(2) + group(4), then the pairwise and AKM count-prefixed lists."""
    group = _suite(v[2:6], Cipher, oui)
    i = 6
    n = min(int.from_bytes(v[i:i + 2], "little"), max(0, (len(v) - i - 2) // 4))
    i += 2
    pair = [_suite(v[i + 4 * k:i + 4 * k + 4], Cipher, oui) for k in range(n)]
    i += 4 * n
    m = min(int.from_bytes(v[i:i + 2], "little"), max(0, (len(v) - i - 2) // 4))
    i += 2
    akm = [_suite(v[i + 4 * k:i + 4 * k + 4], Akm, oui) for k in range(m)]
    i += 4 * m
    out = {"group": group, "pair": pair, "akm": akm}
    if len(v) >= i + 2:
        out["caps"] = v[i:i + 2]
    return out


def _rsn(v: bytes) -> dict:
    """RSN element (IE 48): cipher/AKM suites under the standard OUI 00-0f-ac."""
    return _cipher_suites(v, b"\x00\x0f\xac")


def _wpa(v: bytes) -> dict:
    """WPA vendor element (00-50-f2 type 1): the pre-RSN cipher/AKM suites, same layout as RSN but
    under the Microsoft OUI 00-50-f2."""
    return _cipher_suites(v, b"\x00\x50\xf2")


def _wsc(v: bytes) -> dict:
    """WSC attribute blob -> {label: raw bytes} for the identity attributes we surface. The bytes
    stay raw (lossless for diff); fmt renders them as text. parse_tlvs never raises (it stops on a
    truncated attribute), so no guard is needed."""
    attrs = parse_tlvs(v)
    return {label: attrs[aid] for aid, label in _WSC_NAMES.items() if aid in attrs}


# (name, shift, width) for the subfields wider than one bit. The Flag classes name the single bits;
# masking these out of the flag leaves its KEEP residual holding only bits past our map.
_HT_FIELDS = [("sm_power_save", 2, 2), ("rx_stbc", 8, 2)]
_VHT_FIELDS = [
    ("max_mpdu", 0, 2), ("chan_width", 2, 2), ("rx_stbc", 8, 3), ("bf_sts", 13, 3),
    ("sounding_dims", 16, 3), ("max_ampdu_exp", 23, 3), ("link_adapt", 26, 2), ("ext_nss_bw", 30, 2),
]
_EXT_FIELDS = [("service_interval_granularity", 41, 3), ("max_msdus_in_amsdu", 63, 2)]
_HE_MAC_FIELDS = [
    ("dynamic_frag", 3, 2), ("max_num_frag_msdu", 5, 3), ("min_frag_size", 8, 2),
    ("trigger_frame_mac_padding_dur", 10, 2), ("multi_tid_agg_rx", 12, 3),
    ("he_link_adaptation", 15, 2), ("max_ampdu_len_exp", 27, 2), ("multi_tid_agg_tx", 39, 3),
]
_HE_PHY_FIELDS = [
    ("channel_width_set", 1, 7), ("preamble_punc_rx", 8, 4), ("midamble_rx_tx_max_nsts", 15, 2),
    ("dcm_max_constellation_tx", 24, 2), ("dcm_max_nss_tx", 26, 1),
    ("dcm_max_constellation_rx", 27, 2), ("dcm_max_nss_rx", 29, 1),
    ("beamformee_max_sts_under_80mhz", 34, 3), ("beamformee_max_sts_above_80mhz", 37, 3),
    ("beamformee_num_snd_dim_under_80mhz", 40, 3), ("beamformee_num_snd_dim_above_80mhz", 43, 3),
    ("max_nc", 59, 3), ("dcm_max_ru", 70, 2), ("nominal_packet_padding", 78, 2),
]
_HE_OP_FIELDS = [("default_pe_duration", 0, 3), ("txop_dur_rts_threshold", 4, 10)]
_BSS_COLOR_FIELDS = [("bss_color", 0, 6)]


def _bitfield(head: bytes, flag: type[Flag], fields: list) -> dict:
    """A little endian word -> {cap: Flag, <wide field>: int, ...}. Each wide field is masked out of
    the flag so it shows once as an int; a field whose bits run past the bytes present is omitted."""
    word = int.from_bytes(head, "little")
    present = [(n, s, w) for n, s, w in fields if s + w <= len(head) * 8]
    mask = 0
    for _, s, w in present:
        mask |= ((1 << w) - 1) << s
    out = {"cap": flag(word & ~mask)}
    for n, s, w in present:
        out[n] = (word >> s) & ((1 << w) - 1)
    return out


def _capfields(v: bytes, flag: type[Flag], fields: list, width: int) -> dict:
    """A capability word of `width` bytes decoded, the trailing bytes kept raw in rest."""
    out = _bitfield(v[:width], flag, fields)
    out["rest"] = v[width:]
    return out


def _htcaps(v: bytes) -> dict:
    """HT Capabilities (IE 45): the cap word (first 2 bytes) decoded; the remainder (A-MPDU params,
    MCS set, ...) kept raw in rest."""
    return _capfields(v, HTCap, _HT_FIELDS, 2)


def _vhtcaps(v: bytes) -> dict:
    """VHT Capabilities (IE 191): the cap word (first 4 bytes) decoded; the MCS/NSS map kept in rest."""
    return _capfields(v, VHTCap, _VHT_FIELDS, 4)


def _extcaps(v: bytes) -> dict:
    """Extended Capabilities (IE 127): the whole element is the bitfield, so there is no rest."""
    return _bitfield(v, ExtCap, _EXT_FIELDS)


def _hecaps(v: bytes) -> dict:
    """HE Capabilities (ExtIE 35): the HE MAC (first 6 bytes) and HE PHY (next 11 bytes) capability
    words decoded; the MCS/NSS set and PPE thresholds kept raw in rest."""
    return {
        "mac": _bitfield(v[:6], HeMacCap, _HE_MAC_FIELDS),
        "phy": _bitfield(v[6:17], HePhyCap, _HE_PHY_FIELDS),
        "rest": v[17:],
    }


def _heop(v: bytes) -> dict:
    """HE Operation (ExtIE 36): the parameters word (first 3 bytes) and the BSS color byte decoded;
    the basic MCS/NSS set and any optional VHT/6GHz tails kept raw in rest."""
    out = {"params": _bitfield(v[:3], HeOpParams, _HE_OP_FIELDS)}
    if len(v) > 3:
        out["color"] = _bitfield(v[3:4], HeBssColor, _BSS_COLOR_FIELDS)
    out["rest"] = v[4:]
    return out


def _wmm(t: bytes):
    """A WMM parameter element body (after OUI+type) -> readable EDCA structure, or None to leave
    it raw. Only a clean parameter subtype of the exact expected length is parsed; every bit of
    each AC record is captured in a named field, so the parse round-trips."""
    if t[:1] != b"\x01" or len(t) != 20:
        return None
    edca = []
    for k in range(4):
        b0, b1 = t[4 + 4 * k], t[5 + 4 * k]
        edca.append({
            "aifsn": b0 & 0x0f, "acm": bool(b0 & 0x10), "aci": (b0 >> 5) & 0x07,
            "ecwmin": b1 & 0x0f, "ecwmax": (b1 >> 4) & 0x0f,
            "txop": int.from_bytes(t[6 + 4 * k:8 + 4 * k], "little"),
        })
    return {"subtype": t[0], "version": t[1], "qos": t[2:3], "reserved": t[3:4], "edca": edca}


def _tlvs(t: bytes, lensize: int):
    """Yield (id, value) for a 1-byte-id TLV body whose length field is `lensize` bytes little endian
    (P2P uses 2, MBO uses 1), stopping on a truncated attribute."""
    i = 0
    while i + 1 + lensize <= len(t):
        ln = int.from_bytes(t[i + 1:i + 1 + lensize], "little")
        val = t[i + 1 + lensize:i + 1 + lensize + ln]
        if len(val) != ln:
            break
        yield t[i], val
        i += 1 + lensize + ln


def _p2p_device_info(v: bytes) -> dict:
    """P2P Device Info (attribute 13) -> {addr, config_methods, primary_dev_type, name?}. The device
    name is a nested WPS TLV (type 0x1011, big endian) at the end; its bytes stay raw for fmt."""
    if len(v) < 17:
        return {}
    out = {"addr": _mac(v[:6]), "config_methods": int.from_bytes(v[6:8], "big"),
           "primary_dev_type": {"category": int.from_bytes(v[8:10], "big"),
                                "sub_category": int.from_bytes(v[14:16], "big")}}
    i = 17 + 8 * v[16]                          # skip the secondary device type list
    if len(v) >= i + 4 and int.from_bytes(v[i:i + 2], "big") == 0x1011:
        nlen = int.from_bytes(v[i + 2:i + 4], "big")
        name = v[i + 4:i + 4 + nlen]
        if len(name) == nlen:
            out["name"] = name
    return out


def _p2p(t: bytes) -> dict:
    """Wi-Fi P2P -> the device-identifying attributes (capability bitmaps, device info); other P2P
    attributes are not surfaced. Attribute TLVs use a 2-byte little endian length."""
    out: dict = {}
    for aid, val in _tlvs(t, 2):
        if aid == 2 and len(val) >= 2:
            out["capability"] = {"device": val[0], "group": val[1]}
        elif aid == 13:
            info = _p2p_device_info(val)
            if info:
                out["device_info"] = info
    return out


def _hs20(t: bytes) -> dict:
    """Hotspot 2.0 Indication -> {release, dgaf_disabled, pps_mo_id?, anqp_domain_id?}. `release` is
    the raw 4-bit field (Passpoint release is release+1)."""
    if not t:
        return {}
    cfg = t[0]
    out = {"release": (cfg >> 4) & 0x0f, "dgaf_disabled": bool(cfg & 0x01)}
    i = 1
    if cfg & 0x02 and len(t) >= i + 2:
        out["pps_mo_id"] = int.from_bytes(t[i:i + 2], "little")
        i += 2
    if cfg & 0x04 and len(t) >= i + 2:
        out["anqp_domain_id"] = int.from_bytes(t[i:i + 2], "little")
    return out


def _mbo(t: bytes) -> dict:
    """MBO-OCE -> the steering / capability attributes (AP cellular awareness, association
    disallowed reason, cellular preferences); OCE attributes are not surfaced. TLVs use a 1-byte
    length."""
    out: dict = {}
    for aid, val in _tlvs(t, 1):
        if aid == 1 and val:
            out["ap_cell_aware"] = bool(val[0] & 0x40)
        elif aid == 3 and val:
            out["cell_data_capa"] = val[0]
        elif aid == 4 and val:
            out["assoc_disallowed"] = val[0]
        elif aid == 5 and val:
            out["cell_pref"] = val[0]
    return out


def _vendor(v: bytes) -> dict:
    """Vendor Specific -> {oui, name?, type, type_name?, <decoded>?|data?}. Known types recurse into
    their decoder (WSC, WPA, WMM, P2P, HS20, MBO); other payloads, and any that surface nothing,
    stay raw in data."""
    if len(v) < 4:
        return {"data": v}
    out = {"oui": ":".join(f"{b:02x}" for b in v[:3])}
    name = VENDOR_BY_OUI.get(v[:3].hex().upper())
    if name:
        out["name"] = name
    out["type"] = v[3]
    type_name = _VENDOR_TYPES.get(v[:4])
    if type_name:
        out["type_name"] = type_name
    tail = v[4:]
    if v[:4] == _WPS:
        out["wsc"] = _wsc(tail)
    elif v[:4] == _WPA:
        out["wpa"] = _wpa(tail)
    elif v[:4] == _WMM and _wmm(tail) is not None:
        out["wmm"] = _wmm(tail)
    elif v[:4] == _P2P and _p2p(tail):
        out["p2p"] = _p2p(tail)
    elif v[:4] == _HS20 and _hs20(tail):
        out["hs20"] = _hs20(tail)
    elif v[:4] == _MBO and _mbo(tail):
        out["mbo"] = _mbo(tail)
    elif tail:
        out["data"] = tail
    return out


def decode(key, value: bytes):
    """One IE's bytes -> its plain value. IEs without a decoder fall through to raw bytes. `key` is
    an IE or ExtIE member."""
    if key is IE.SSID:
        return value                       # kept raw (bytes); fmt shows it as text
    if key in (IE.SUPP_RATES, IE.EXT_SUPP_RATES):
        return _rates(value)
    if key is IE.RSN:
        return _rsn(value)
    if key is IE.HT_CAPS:
        return _htcaps(value)
    if key is IE.VHT_CAPS:
        return _vhtcaps(value)
    if key is IE.EXT_CAPS:
        return _extcaps(value)
    if key is IE.VENDOR:
        return _vendor(value)
    if key is ExtIE.HE_CAPS:
        return _hecaps(value)
    if key is ExtIE.HE_OP:
        return _heop(value)
    return value


# ======================================================================================
# IE walk + allowlist: the reusable body -> {key: value} step. `parse_ies` is module level so a
# nested decoder (e.g. a future Multiple BSSID) can call it on the inner IE bytes, and `_walk` is
# reusable for any TLV body (a sub-element namespace walks with _walk but must NOT reuse IE.of).
# ======================================================================================
def _walk(body: bytes):
    """Yield (tag, value) for each TLV in a body, stopping on a truncated tag."""
    i = 0
    while i + 2 <= len(body):
        ln = body[i + 1]
        value = body[i + 2:i + 2 + ln]
        if len(value) != ln:
            break
        yield body[i], value
        i += 2 + ln


def parse_ies(body: bytes) -> dict:
    """Walk an IE body, keep allowlisted elements (element 255 via ExtIE on its extension byte),
    decode each. Returns {IE|ExtIE: value}."""
    ies: dict = {}
    for tag, value in _walk(body):
        if tag == 255 and value:
            key, inner = ExtIE.of(value[0]), value[1:]
        else:
            key, inner = IE.of(tag), value
        if key is not None:
            ies[key] = decode(key, inner)
    return ies


# ======================================================================================
# Frame: a management frame reduced to its allowlisted, decoded IEs. Owns parsing (a factory)
# and diffing (a data operation). Presentation lives in render(), below.
# ======================================================================================
_NAMES = {
    (0, 0): "assoc_req", (0, 1): "assoc_resp", (0, 2): "reassoc_req", (0, 3): "reassoc_resp",
    (0, 4): "probe_req", (0, 5): "probe_resp", (0, 8): "beacon", (0, 10): "disassoc",
    (0, 11): "auth", (0, 12): "deauth", (0, 13): "action",
    (2, 0): "data", (2, 8): "qos_data",
}
# mgmt subtype -> IE section start (24-byte header + the subtype's fixed fields).
_IE_START = {0: 28, 1: 30, 2: 34, 3: 30, 4: 24, 5: 36, 8: 36}


def _mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


class Frame:
    def __init__(self, name: str, src: str, dst: str, ies: dict):
        self.name = name
        self.src = src
        self.dst = dst
        self.ies = ies   # dict[IE | ExtIE, value]

    @classmethod
    def parse(cls, raw: bytes) -> "Frame | None":
        """A raw 802.11 frame -> Frame, or None if it is too short to have a header."""
        if len(raw) < 24:
            return None
        fc0 = raw[0]
        ftype, subtype = (fc0 >> 2) & 0x03, (fc0 >> 4) & 0x0F
        name = _NAMES.get((ftype, subtype), f"type{ftype}_sub{subtype}")
        start = _IE_START.get(subtype) if ftype == 0 else None
        ies = parse_ies(raw[start:]) if start is not None else {}
        return cls(name, _mac(raw[10:16]), _mac(raw[4:10]), ies)

    def diff(self, other: "Frame") -> dict:
        """What changed from self to other: {added, removed, changed}, keyed by IE. Value equality
        is deep (plain types), so a change at any nesting depth is caught."""
        a, b = self.ies, other.ies
        return {
            "added": {k: b[k] for k in b.keys() - a.keys()},
            "removed": {k: a[k] for k in a.keys() - b.keys()},
            "changed": {k: (a[k], b[k]) for k in a.keys() & b.keys() if a[k] != b[k]},
        }


# ======================================================================================
# Presentation: one recursive formatter over the plain-value union, at any depth. This is the
# only place lossy prettifying (bytes as text, zero squeeze) happens.
# ======================================================================================
def _squeeze(v: bytes) -> str:
    """Hex, collapsing a trailing zero run to +Nz (N = zero nibbles) once it saves characters."""
    trimmed = v.rstrip(b"\x00")
    znib = (len(v) - len(trimmed)) * 2
    return trimmed.hex() + f"+{znib}z" if znib > 3 else v.hex()


def _key(k) -> str:
    return k.label if isinstance(k, Labeled) else str(k)


def fmt(v) -> str:
    """A decoded value to a string, recursing through lists and dicts to any depth. Printable
    bytes render as quoted text; other bytes as squeezed hex."""
    if isinstance(v, Labeled):
        return v.label
    if isinstance(v, Flag):
        names, covered = [], 0
        for m in type(v):
            if m in v:
                names.append(m.name)
                covered |= m.value
        residual = v.value & ~covered            # unnamed / multi-bit subfields, kept as hex
        text = "|".join(names)
        if residual:
            text += f"+0x{residual:x}" if text else f"0x{residual:x}"
        return text or "0"
    if isinstance(v, bytes):
        if v and all(0x20 <= b <= 0x7E for b in v):
            return f"'{v.decode('ascii')}'"
        return _squeeze(v)
    if isinstance(v, list):
        return "[" + ",".join(fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{_key(k)}={fmt(x)}" for k, x in v.items()) + "}"
    return str(v)


def render(frame: Frame) -> str | None:
    """`src->dst [name] IE=..., ...`, or None when no allowlisted IE was present."""
    if not frame.ies:
        return None
    body = ",".join(f"{k.label}={fmt(v)}" for k, v in frame.ies.items())
    return f"{frame.src}->{frame.dst} [{frame.name}] {body}"
