"""Vault drawer overlay and VaultItemView detail pane, on a WifiteApp that skips its mount work.
The autouse _captures_to_tmp fixture points Config.captures_dir at tmp_path, and WifiteApp builds its Vault from there.
"""
import asyncio
import threading
import time
from typing import Callable

import pytest
from textual import events
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Label

from wifit3.crack.hc22000_format import hashline_id
from wifit3.models import AccessPoint, CaptureType, ToolName
from wifit3.persist.ap_state import PasswordStatus
from wifit3.persist.exhaustion import record_exhausted
from wifit3.persist.vault import Vault
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault_drawer import VaultDrawer, _ap_contents_text
from wifit3.ui.screens.vault_table import VaultTable
from wifit3.ui.screens.vault_item import (DeleteKeyPressed, VaultItemView, date_text, hashline_groups, _ApStatePanel,
                                          _CrackerButton, _exhausted_count_text, _exhausted_names, _hashline_prompt,
                                          _no_crackable_text, _UncrackedList)
from wifit3.ui.vault.modals.add_credential import ManualCredential
from wifit3.crack import wpa_psk
from wifit3.dot11.wpa import derive_pmk
from tests.hashline_dates import write_hashline_dates

_EAPOL_LINE = ("WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431*" + "a" * 64 + "*"
               + "00" * 99 + "*02\n")
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_OTHER_CLIENT_LINE = _EAPOL_LINE.replace("112233445566", "112233445577")
_SAME_CLIENT_LINE = _EAPOL_LINE.replace("0" * 32, "1" * 32, 1)     # another MIC, grouped with _EAPOL_LINE
_WEP_TXT = "SSID: HomeNet\nBSSID: aa:bb:cc:dd:ee:ff\nWEP key (hex):   6162636465\n"
def _write(d, name, content):
    (d / name).write_text(content, encoding="utf-8")


def _pmkid_line(psk: str, mac_ap: str) -> str:
    """A HomeNet PMKID line that ``psk`` cracks."""
    pmkid = wpa_psk.pmkid(derive_pmk(psk, b"HomeNet"), bytes.fromhex(mac_ap), bytes.fromhex("021122334455"))
    return f"WPA*01*{pmkid.hex()}*{mac_ap}*021122334455*{b'HomeNet'.hex()}***\n"


def _m1m2_line(psk: str) -> str:
    """A HomeNet M1+M2 EAPOL line whose MIC ``psk`` makes."""
    from wifit3.dot11.eapol import eapol_key
    from wifit3.dot11.wpa import KCK_LEN, derive_ptk, eapol_mic_hmac_md5

    aa, spa, anonce, snonce = (bytes.fromhex(h) for h in ("aabbccddeeff", "021122334455", "a0" * 32, "5b" * 32))
    eapol = eapol_key(key_info=0x0109, key_len=0, replay=1, nonce=snonce,
                      key_data=bytes.fromhex("30140100000fac040100000fac040100000fac060000"))
    mic = eapol_mic_hmac_md5(derive_ptk(derive_pmk(psk, b"HomeNet"), aa, spa, anonce, snonce, KCK_LEN), eapol)
    return f"WPA*02*{mic.hex()}*{aa.hex()}*{spa.hex()}*{b'HomeNet'.hex()}*{anonce.hex()}*{eapol.hex()}*00\n"


class _VaultApp(WifiteApp):
    """WifiteApp without its splash, device watch or drawer slide; records what it copies (never
    touching the system clipboard) and each notification."""

    CSS = "VaultDrawer #vault-content { offset-y: 0; }"

    def __init__(self) -> None:
        super().__init__()
        self.copied: list[str] = []
        self.notified: list[str] = []

    def on_mount(self, event: events.Mount) -> None:
        event.prevent_default()

    def copy_to_clipboard(self, text: str) -> None:
        self.copied.append(text)

    def notify(self, message, *args, **kwargs) -> None:
        self.notified.append(str(message))
        super().notify(message, *args, **kwargs)


class _DrawerWithoutWidgets(VaultDrawer):
    """The drawer's handlers without its widgets, whose layout costs ~100 ms a test."""

    def compose(self) -> ComposeResult:
        yield from ()


class _DrawerWithOnlyItsApTable(VaultDrawer):
    """The drawer with its AP table but no item pane; records each AP it would show there."""

    def __init__(self, preselected_bssid: str | None = None) -> None:
        super().__init__(preselected_bssid)
        self.shown_bssids: list[str] = []

    def compose(self) -> ComposeResult:
        yield VaultTable(id="vault-table")

    def _load_widget(self, bssid: str) -> None:
        self.shown_bssids.append(bssid)


class _ItemViewScreen(Screen):
    def compose(self) -> ComposeResult:
        yield VaultItemView()


async def _wait_until(pilot, condition: Callable[[], bool]) -> None:
    """Pause until ``condition()`` holds; fail after 5 s instead of hanging."""
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await pilot.pause(0)


async def _open_item_view(app, pilot, bssid: str) -> VaultItemView:
    """Show ``bssid`` in a VaultItemView alone, without the drawer's AP table, tabs and footer."""
    screen = _ItemViewScreen()
    await app.push_screen(screen)
    item = screen.query_one(VaultItemView)
    item.load(bssid, app.vault.ssid(bssid))
    await _wait_until(pilot, lambda: bool(item.query_children(_ApStatePanel)))
    await pilot.pause(0)
    return item


def test_the_delete_prompt_counts_an_aps_captures_and_keys(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", _PMKID_LINE)
    vault = Vault()

    assert _ap_contents_text(vault.persisted("aa:bb:cc:dd:ee:ff"), vault.ap_state("aa:bb:cc:dd:ee:ff")) == \
        "1 capture, 1 key"


def test_an_ap_with_only_pcaps_says_how_many_captures_none_crackable(tmp_path):
    (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000000_handshake.pcap").write_bytes(b"")
    (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000500_pmkid.pcap").write_bytes(b"")
    files = Vault().ap_state("aa:bb:cc:dd:ee:ff").handshake_and_pmkid_files

    assert _no_crackable_text(files) == f"2 captures, none crackable ({date_text(1700000500)})"


def test_each_uncracked_row_reads_its_pair_client_verdict_and_ssid(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", _EAPOL_LINE + _OTHER_CLIENT_LINE + _PMKID_LINE)
    groups = hashline_groups(Vault().ap_state("aa:bb:cc:dd:ee:ff").uncracked)
    prompts = [" ".join(str(_hashline_prompt(group, True, True, "", 0)).split()) for group in groups]

    assert prompts == ["no date M2+M3 …55:66 authorized Test1", "no date M2+M3 …55:77 authorized Test1",
                       "no date PMKID …55:66 authorized Test1"]


def test_each_uncracked_row_counts_and_names_its_exhausted_wordlists(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000",
           _EAPOL_LINE + _SAME_CLIENT_LINE + _OTHER_CLIENT_LINE + _PMKID_LINE)
    record_exhausted("HomeNet", "aa:bb:cc:dd:ee:ff", "9:100", "rockyou.txt", [hashline_id(_EAPOL_LINE)])
    record_exhausted("HomeNet", "aa:bb:cc:dd:ee:ff", "8:800", "default.txt",
                     [hashline_id(_EAPOL_LINE), hashline_id(_SAME_CLIENT_LINE), hashline_id(_PMKID_LINE)])
    state = Vault().ap_state("aa:bb:cc:dd:ee:ff")
    groups = hashline_groups(state.uncracked)

    assert [_exhausted_count_text(g, state.exhausted_wordlists) for g in groups] == [
        "2 wordlists exhausted (1 on some lines)", "", "1 wordlist exhausted"]
    assert [_exhausted_names(g, state.exhausted_wordlists) for g in groups] == [
        ["default.txt", "rockyou.txt"], [], ["default.txt"]]


@pytest.mark.asyncio
async def test_add_key_adds_a_psk_no_saved_capture_confirms_as_not_verified(tmp_path, mocker):
    right = _pmkid_line("rightpass", "aabbccddeeff")
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", right)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-fe.hc22000", _pmkid_line("rightpass", "aabbccddeefe"))
    write_hashline_dates("HomeNet", "aa:bb:cc:dd:ee:ff", {right: 1_700_000_000})
    app = _VaultApp()
    propagate = mocker.spy(app.vault, "propagate_verified_psk")
    async with app.run_test() as pilot:
        drawer = _DrawerWithOnlyItsApTable()
        await app.push_screen(drawer)

        drawer._credential_added(ManualCredential(CaptureType.WPA_PSK, "HomeNet", "aa:bb:cc:dd:ee:ff", "wrongpass"))
        assert app.notified[-1] == "Credential added, not verified"
        state = app.vault.ap_state("aa:bb:cc:dd:ee:ff")
        assert [k.value for k in state.keys] == ["wrongpass"]
        assert state.keys[0].manual_capture_names

        drawer._credential_added(ManualCredential(CaptureType.WPA_PSK, "HomeNet", "aa:bb:cc:dd:ee:ff", "rightpass"))
        await _wait_until(pilot, lambda: propagate.call_args is not None and "rightpass" in propagate.call_args.args)
        await asyncio.wrap_future(propagate.spy_return)
        await app.workers.wait_for_complete()
        assert app.notified[-2:] == ["Credential verified and added to Vault",
                                     "PSK also verified and saved for 1 related APs"]
        assert app.vault.ap_state("aa:bb:cc:dd:ee:ff").status is PasswordStatus.CURRENT


@pytest.mark.asyncio
async def test_add_key_adds_a_psk_as_not_verified_when_the_ap_has_nothing_to_crack(tmp_path):
    (tmp_path / "Other_02-00-00-00-00-20_1000_handshake.pcap").write_bytes(b"")
    app = _VaultApp()
    async with app.run_test():
        drawer = _DrawerWithoutWidgets()
        await app.push_screen(drawer)

        drawer._credential_added(ManualCredential(CaptureType.WPA_PSK, "Other", "02:00:00:00:00:20", "anything"))
        await app.workers.wait_for_complete()

        assert app.notified[-1] == "Credential added, not verified"
        assert app.vault.ap_state("02:00:00:00:00:20").status is PasswordStatus.NOT_VERIFIED

        drawer._credential_added(ManualCredential(CaptureType.WPS_PIN, "Other", "02:00:00:00:00:20", "1234"))
        pin = next(c for c in app.vault.persisted("02:00:00:00:00:20") if c.type == CaptureType.WPS_PIN)
        assert pin.pin == "1234" and pin.value is None


@pytest.mark.asyncio
async def test_add_key_adds_a_psk_only_an_m1m2_line_confirms_as_not_verified(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", _m1m2_line("rightpass"))
    app = _VaultApp()
    async with app.run_test():
        drawer = _DrawerWithoutWidgets()
        await app.push_screen(drawer)

        drawer._credential_added(ManualCredential(CaptureType.WPA_PSK, "HomeNet", "aa:bb:cc:dd:ee:ff", "wrongpass"))
        await app.workers.wait_for_complete()
        assert app.notified[-1] == "Credential added, not verified"
        drawer._credential_added(ManualCredential(CaptureType.WPA_PSK, "HomeNet", "aa:bb:cc:dd:ee:ff", "rightpass"))
        await app.workers.wait_for_complete()

        assert app.notified[-1] == "Credential added, not verified"
        assert app.vault.ap_state("aa:bb:cc:dd:ee:ff").status is PasswordStatus.NOT_VERIFIED


async def _delete_key_prompt(app, pilot, key_value: str) -> str:
    """Ask the drawer to delete the key holding ``key_value`` and return the ConfirmModal's prompt."""
    drawer = _DrawerWithoutWidgets()
    await app.push_screen(drawer)
    key = next(k for k in app.vault.ap_state("aa:bb:cc:dd:ee:ff").keys if k.value == key_value)
    drawer.post_message(DeleteKeyPressed(key))
    await _wait_until(pilot, lambda: bool(app.screen.query("#prompt")))
    return str(app.screen.query_one("#prompt", Label).content)


@pytest.mark.asyncio
async def test_deleting_a_wps_psk_asks_naming_the_pin_that_goes_with_it():
    app = _VaultApp()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    app.vault.save_wps_pin(ap, "12345670", "hunter22")
    app.vault.save_wep_key(ap, b"abcde")
    async with app.run_test() as pilot:
        assert await _delete_key_prompt(app, pilot, "hunter22") == \
            "Delete PSK hunter22? Also removes WPS PIN 12345670."


@pytest.mark.asyncio
async def test_deleting_a_psk_does_not_name_a_pin_another_file_still_holds():
    app = _VaultApp()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    app.vault.save_wps_pin(ap, "12345670", "oldpass")
    app.vault.save_wps_pin(ap, "12345670", "newpass")
    async with app.run_test() as pilot:
        assert await _delete_key_prompt(app, pilot, "oldpass") == "Delete PSK oldpass?"


@pytest.mark.asyncio
async def test_a_burst_of_vault_changes_refreshes_the_drawer_once(mocker):
    app = _VaultApp()
    async with app.run_test() as pilot:
        drawer = _DrawerWithoutWidgets()
        await app.push_screen(drawer)
        vault_changed = mocker.patch.object(drawer, "vault_changed")

        for bssid in ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"):
            app.vault.notify_changed(bssid)
        await pilot.pause(0)

        vault_changed.assert_called_once_with(False)


def test_a_vault_change_from_another_thread_after_quit_logs_no_error(caplog):
    app = _VaultApp()

    async def run_and_quit() -> None:
        async with app.run_test():
            pass

    asyncio.run(run_and_quit())
    notice = threading.Thread(target=app.vault.notify_changed, args=("aa:bb:cc:dd:ee:01",))
    notice.start()
    notice.join()

    assert not [record for record in caplog.records if record.levelname == "ERROR"]


@pytest.mark.asyncio
async def test_a_tick_handled_after_switching_aps_is_kept_for_its_own_hashlines(tmp_path):
    for bssid in ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:fe"):
        _write(tmp_path, f"HomeNet_{bssid.replace(':', '-')}.hc22000",
               _EAPOL_LINE.replace("aabbccddeeff", bssid.replace(":", "")))
    app = _VaultApp()
    async with app.run_test() as pilot:
        item = await _open_item_view(app, pilot, "aa:bb:cc:dd:ee:ff")
        uncracked = item.query_one(_UncrackedList)
        group = uncracked.groups()[0]

        uncracked.toggle(group)
        item.load("aa:bb:cc:dd:ee:fe", "HomeNet")
        await _wait_until(pilot, lambda: uncracked.groups()[0][0].line.mac_ap == "aabbccddeefe")
        await pilot.pause(0)

        assert item.unticked_hashline_ids == {group[0].id}


@pytest.mark.asyncio
async def test_unchecked_hashlines_are_left_out_of_the_crack_job(tmp_path, mocker):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", _EAPOL_LINE + _OTHER_CLIENT_LINE + _PMKID_LINE)
    app = _VaultApp()
    async with app.run_test() as pilot:
        item = await _open_item_view(app, pilot, "aa:bb:cc:dd:ee:ff")
        submit = mocker.patch.object(app.vault.manager, "submit_job", return_value="job")
        mocker.patch.object(app, "push_screen", side_effect=lambda modal, callback: callback({"wordlists": []}))
        uncracked = item.query_one(_UncrackedList)
        groups = uncracked.groups()
        assert "3 captures selected" in str(item.query_one(".selected-count", Label).content)

        uncracked.deselect(groups[0])
        await pilot.pause(0)
        assert "2 captures selected" in str(item.query_one(".selected-count", Label).content)
        next(b for b in item.query(_CrackerButton).results(_CrackerButton) if b.tool.name == ToolName.CPU_CRACK).press()
        await pilot.pause(0)

        chosen = submit.call_args.kwargs["hashlines"]
        assert len(chosen) == 2 and groups[0][0].raw not in chosen

        uncracked.deselect_all()
        await pilot.pause(0)
        assert all(button.disabled for button in item.query(_CrackerButton).results(_CrackerButton))
