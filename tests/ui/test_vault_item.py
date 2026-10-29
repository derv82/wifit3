import time

from wifit3.crack.hc22000_format import hashline_id, parse_hc22000
from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.ap_state import ApState, DatedHashline, Key, PasswordStatus
from wifit3.ui.screens.vault_item import _KeyRow, date_text, hashline_groups, key_text

_LINE = "WPA*02*{mic}*aabbccddeeff*{sta}*486f6d65*" + "a" * 64 + "*" + "00" * 99 + "*{pair}"


def _hashline(captured: int, sta: str = "112233445566", pair: str = "02") -> DatedHashline:
    raw = _LINE.format(mic=f"{captured:032x}", sta=sta, pair=pair)
    return DatedHashline(hashline_id(raw), raw, parse_hc22000(raw), (captured,))


def _key(capture_type: CaptureType, value: str) -> Key:
    capture = PersistedCapture(type=CaptureType.WPA_PSK, timestamp=1, path="x.txt",
                               bssid="aa:bb:cc:dd:ee:ff", value=value)
    return Key(capture_type, value, (capture,), frozenset(), ())


def test_date_text_says_today_and_drops_the_time_for_another_year():
    now = int(time.time())
    assert date_text(now).startswith("today ")
    assert date_text(None) == "no date"
    assert date_text(now - 2 * 366 * 86400)[-4:].isdigit()


def test_key_text_shows_a_printable_wep_key_as_ascii_and_an_empty_pin_by_label():
    assert key_text(_key(CaptureType.WEP, "6162636465")) == "abcde"
    assert key_text(_key(CaptureType.WEP, "00ff00ff00")) == "00ff00ff00"
    assert key_text(_key(CaptureType.WPS_PIN, "")) != ""
    assert key_text(_key(CaptureType.WPA_PSK, "hunter22")) == "hunter22"


def test_hashline_groups_join_runs_from_one_minute_pair_and_client():
    base = 1_700_000_040
    lines = (_hashline(base + 10), _hashline(base + 5), _hashline(base, sta="112233445577"),
             _hashline(base, pair="00"))
    assert [len(group) for group in hashline_groups(lines)] == [2, 1, 1]


async def _row(key: Key, state: ApState) -> _KeyRow:
    row = _KeyRow()
    await row.show(key, state, show_client=False, show_ssid=False, expanded=False, verifying=None)
    return row


async def test_key_row_takes_its_keys_status_and_says_why():
    hashlines = (_hashline(1_700_000_000),)
    current = Key(CaptureType.WPA_PSK, "hunter22", (PersistedCapture(type=CaptureType.WPS_PBC, timestamp=1,
                                                                      path="x.txt", bssid="aa:bb:cc:dd:ee:ff",
                                                                      value="hunter22"),), frozenset(), ())
    guess = _key(CaptureType.WPA_PSK, "guess")
    state = ApState("aa:bb:cc:dd:ee:ff", (), hashlines, (current, guess), PasswordStatus.CURRENT, current,
                    {current.id: PasswordStatus.CURRENT, guess.id: PasswordStatus.NOT_VERIFIED}, None, hashlines, (), ())
    current_row, guess_row = await _row(current, state), await _row(guess, state)

    assert current_row.has_class("-current") and guess_row.has_class("-not-verified")
    assert "(0/1 captures, verified)" in current_row._summary_markup()
    assert "(0/1 captures, not verified yet)" in guess_row._summary_markup()
