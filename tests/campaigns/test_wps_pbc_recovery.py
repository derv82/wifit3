"""WPS-PBC 5 GHz stall fix: the client-leaving deauth + the patient msg_timeout.

A stalled attempt used to abandon ~1.6s before a slow AP's M6 (~4.6s on 5 GHz) and
then rotate to a fresh MAC, orphaning the AP's EAP session: it retransmits the
in-flight WSC message to the dead MAC and locks out the next attempt at Identity.
Fixes: (1) WpsEnrollee.msg_timeout 3->5s so a slow AP gets time to finish; (2) a
client→AP "leaving" deauth on teardown so the AP drops the session before the next
attempt's MAC.
"""
import struct

from wifit3.campaigns.auth_assoc import build_client_leaving
from wifit3.campaigns.wps.enrollee import WpsEnrollee

_AP = bytes.fromhex("3421090001ff")
_US = bytes.fromhex("02aabbccddee")


def test_build_client_leaving_deauth_frame():
    f = build_client_leaving(_AP, _US)             # deauth (default)
    assert f[0] == 0xC0                            # mgmt, subtype DEAUTH (0x0C << 4)
    assert f[1] == 0x00
    assert f[4:10] == _AP                          # addr1 = AP (receiver)
    assert f[10:16] == _US                         # addr2 = us (sender)
    assert f[16:22] == _AP                         # addr3 = AP (BSSID)
    assert struct.unpack("<H", f[24:26])[0] == 3   # reason 3 = STA leaving
    assert len(f) == 26


def test_build_client_leaving_disassoc_variant():
    f = build_client_leaving(_AP, _US, deauth=False)
    assert f[0] == 0xA0                            # subtype DISASSOC (0x0A << 4)
    assert struct.unpack("<H", f[24:26])[0] == 8   # reason 8 = STA leaving (disassoc)


def test_enrollee_is_patient_by_default():
    # 5s covers a slow 5 GHz AP's ~4.6s M6 without abandoning it mid-exchange.
    e = WpsEnrollee(transport=None, bssid=_AP, our_mac=_US)
    assert e.msg_timeout == 5.0
