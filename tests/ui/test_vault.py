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
from textual.widgets import DataTable, Label

from wifit3.campaigns.live_check import LiveKeyVerifier, VerifyResult, VerifyStatus
from wifit3.crack.hc22000_format import hashline_id
from wifit3.models import AccessPoint, CaptureType, PersistedCapture, ToolName
from wifit3.models.jobs import JobState, ToolStatus
from wifit3.persist.ap_state import PasswordStatus
from wifit3.persist.exhaustion import record_exhausted
from wifit3.persist.meta import update_meta
from wifit3.persist.vault import Vault
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault_drawer import VaultDrawer, _ap_contents_text
from wifit3.ui.screens.vault_table import VaultTable
from wifit3.ui.screens.vault_item import (DeleteKeyPressed, VaultItemView, date_text, hashline_groups, _ApStatePanel,
                                          _CrackerButton, _exhausted_count_text, _exhausted_names, _hashline_prompt,
                                          _KeyRow, _no_crackable_text, _UncrackedList)
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
_LIVE_CHECK = {"kind": "WEP", "key": "6162636465", "correct": True}


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


def _write_a_checked_wep_key_and_three_hashlines(tmp_path) -> None:
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", _EAPOL_LINE + _OTHER_CLIENT_LINE + _PMKID_LINE)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    update_meta("HomeNet", "aa:bb:cc:dd:ee:ff", live_verifications=[{**_LIVE_CHECK, "at": 1000}])


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


class _ItemViewScreen(Screen):
    def compose(self) -> ComposeResult:
        yield VaultItemView()


async def _wait_until(pilot, condition: Callable[[], bool]) -> None:
    """Pause until ``condition()`` holds; fail after 5 s instead of hanging."""
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await pilot.pause(0)


async def _open_vault(app, pilot, bssid: str | None = None) -> VaultDrawer:
    """Push the drawer and wait until it has laid out its content, opened and judged its PSKs."""
    drawer = VaultDrawer(bssid)
    await app.push_screen(drawer)
    await _wait_until(pilot, lambda: drawer.has_class("open"))
    await app.workers.wait_for_complete()
    await pilot.pause(0)
    return drawer


async def _open_item_view(app, pilot, bssid: str) -> VaultItemView:
    """Show ``bssid`` in a VaultItemView alone, without the drawer's AP table, tabs and footer."""
    screen = _ItemViewScreen()
    await app.push_screen(screen)
    item = screen.query_one(VaultItemView)
    item.load(bssid, app.vault.ssid(bssid))
    await _wait_until(pilot, lambda: bool(item.query_children(_ApStatePanel)))
    await pilot.pause(0)
    return item


async def _reload_item_view(pilot, item: VaultItemView, bssid: str) -> None:
    """What the drawer does on a Vault change: drop the cached state and reload the AP in view."""
    item.app.vault.notify_changed(bssid)
    item.load(bssid, item.app.vault.ssid(bssid))
    await pilot.pause(0)
    await pilot.pause(0)


@pytest.mark.asyncio
async def test_toast_on_job_completion(tmp_path):
    from unittest.mock import patch

    def make(status, msg):
        return JobState(job_id="j1", tool_name="hashcat", capture_path="x", hashlines_path="x",
                        status=status, progress_msg=msg, display_name="hashcat (ASUS)")

    app = _VaultApp()
    async with app.run_test():
        job = make(ToolStatus.RUNNING, "Running (5%)")
        app.active_jobs = [job]
        with patch.object(app, "notify") as notify:
            app._notify_completions()          # first sight: record RUNNING, no toast
            notify.assert_not_called()
            job.status = ToolStatus.SUCCESS
            job.progress_msg = "Cracked! Key: 0xdeadbeef"
            job.key = "0xdeadbeef"
            app._notify_completions()          # RUNNING -> SUCCESS: toast
            assert notify.call_count == 1
            args, kwargs = notify.call_args
            assert "PSK: 0xdeadbeef" in args[0]
            assert kwargs["title"] == "hashcat (ASUS)"

        # A job already terminal when first seen is not announced.
        app.active_jobs = [JobState(job_id="j2", tool_name="hashcat", capture_path="y", hashlines_path="y",
                                    status=ToolStatus.SUCCESS, progress_msg="Cracked! Key: k",
                                    display_name="hashcat (X)")]
        with patch.object(app, "notify") as notify2:
            app._notify_completions()
            notify2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ToolStatus.FAILURE, ToolStatus.STOPPED, ToolStatus.ERROR])
async def test_an_automatic_job_that_finds_no_key_is_not_toasted(tmp_path, status):
    from unittest.mock import patch

    app = _VaultApp()
    async with app.run_test():
        job = JobState(job_id="j1", tool_name="cpu_crack", capture_path="x", hashlines_path="x",
                       status=ToolStatus.RUNNING, progress_msg="Running", auto_crack=True)
        app.active_jobs = [job]
        with patch.object(app, "notify") as notify:
            app._notify_completions()
            job.status = status
            app._notify_completions()
            notify.assert_not_called()


@pytest.mark.asyncio
async def test_an_automatic_job_success_toasts_and_posts_job_cracked_to_the_screen(tmp_path):
    from unittest.mock import patch

    app = _VaultApp()
    async with app.run_test():
        job = JobState(job_id="j1", tool_name="cpu_crack", capture_path="x", hashlines_path="x",
                       status=ToolStatus.RUNNING, progress_msg="Running", auto_crack=True)
        app.active_jobs = [job]
        with patch.object(app, "notify") as notify, \
                patch.object(app.screen, "post_message") as post_message:
            app._notify_completions()
            job.status = ToolStatus.SUCCESS
            job.key = "password1"
            app._notify_completions()
            assert "PSK: password1" in notify.call_args.args[0]
            posted = post_message.call_args.args[0]
            assert isinstance(posted, WifiteApp.JobCracked) and posted.job is job


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
async def test_add_key_adds_a_psk_no_saved_capture_confirms_as_not_verified(tmp_path):
    right = _pmkid_line("rightpass", "aabbccddeeff")
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", right)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-fe.hc22000", _pmkid_line("rightpass", "aabbccddeefe"))
    write_hashline_dates("HomeNet", "aa:bb:cc:dd:ee:ff", {right: 1_700_000_000})
    app = _VaultApp()
    async with app.run_test() as pilot:
        drawer = await _open_vault(app, pilot, "aa:bb:cc:dd:ee:ff")

        drawer._credential_added(ManualCredential(CaptureType.WPA_PSK, "HomeNet", "aa:bb:cc:dd:ee:ff", "wrongpass"))
        await app.workers.wait_for_complete()
        assert app.notified[-1] == "Credential added, not verified"
        state = app.vault.ap_state("aa:bb:cc:dd:ee:ff")
        assert [k.value for k in state.keys] == ["wrongpass"]
        assert state.keys[0].manual_capture_names

        drawer._credential_added(ManualCredential(CaptureType.WPA_PSK, "HomeNet", "aa:bb:cc:dd:ee:ff", "rightpass"))
        await _wait_until(pilot, lambda: app.notified[-1] == "PSK also verified and saved for 1 related APs")
        assert app.notified[-2] == "Credential verified and added to Vault"
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


@pytest.mark.asyncio
async def test_a_vault_change_from_another_thread_refreshes_the_drawer(mocker):
    app = _VaultApp()
    async with app.run_test() as pilot:
        drawer = _DrawerWithoutWidgets()
        await app.push_screen(drawer)
        vault_changed = mocker.patch.object(drawer, "vault_changed")

        await asyncio.to_thread(app.vault.notify_changed, "aa:bb:cc:dd:ee:01")
        await pilot.pause(0)

        vault_changed.assert_called_once_with(False)


def _save_wep_aps(vault: Vault, count: int) -> list[AccessPoint]:
    """``count`` APs with a WEP key each, Net-00 first in the table."""
    aps = [AccessPoint(bssid=f"02:00:00:00:00:{n:02x}", ssid=f"Net-{n:02d}") for n in range(count)]
    for ap in aps:
        vault.save_wep_key(ap, b"abcde")
    return aps


async def _open_vault_scrolled_to(app, pilot, row: int) -> DataTable:
    drawer = await _open_vault(app, pilot)
    table = drawer.query_one("#vault-aps", DataTable)
    table.move_cursor(row=row)
    item = drawer.query_one(VaultItemView)
    await _wait_until(pilot, lambda: table.scroll_y > 0 and item._bssid == _cursor_bssid(table))
    await pilot.pause(0)
    return table


def _cursor_bssid(table: DataTable) -> str:
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


@pytest.mark.asyncio
async def test_another_aps_new_key_redraws_only_its_row_and_keeps_the_cursor_scroll_and_item_pane():
    app = _VaultApp()
    aps = _save_wep_aps(app.vault, 40)
    async with app.run_test(size=(80, 24)) as pilot:
        table = await _open_vault_scrolled_to(app, pilot, 30)
        scroll_y = table.scroll_y
        cells = {ap.bssid: table.get_cell(ap.bssid, "ap") for ap in aps}
        item = app.screen.query_one(VaultItemView)
        state = item._state

        app.vault.save_wps_pin(aps[5], "12345670", None)
        await _wait_until(pilot, lambda: "✓PIN" in table.get_cell(aps[5].bssid, "ap").plain)
        await pilot.pause(0)

        assert [bssid for bssid, cell in cells.items() if table.get_cell(bssid, "ap") is not cell] == [aps[5].bssid]
        assert (table.cursor_row, table.scroll_y) == (30, scroll_y)
        assert item._state is state
        assert app.screen.query_one("#vault-tabs").get_tab("tab-pin") is not None


@pytest.mark.asyncio
async def test_a_new_ap_above_the_cursor_keeps_the_cursors_ap_at_the_same_height_without_highlighting(mocker):
    app = _VaultApp()
    aps = _save_wep_aps(app.vault, 40)
    async with app.run_test(size=(80, 24)) as pilot:
        table = await _open_vault_scrolled_to(app, pilot, 30)
        scroll_y = table.scroll_y
        schedule_widget_load = mocker.patch.object(app.screen, "_schedule_widget_load")

        app.vault.save_wep_key(AccessPoint(bssid="02:00:00:00:01:00", ssid="Aardvark"), b"abcde")
        await _wait_until(pilot, lambda: table.row_count == 41)
        await pilot.pause(0)

        assert table.get_row_index("02:00:00:00:01:00") == 0
        assert _cursor_bssid(table) == aps[30].bssid
        assert (table.cursor_row, table.scroll_y) == (31, scroll_y + 1)
        schedule_widget_load.assert_not_called()


@pytest.mark.asyncio
async def test_a_nickname_redraws_only_its_row_and_keeps_the_cursor_and_scroll():
    app = _VaultApp()
    aps = _save_wep_aps(app.vault, 40)
    async with app.run_test(size=(80, 24)) as pilot:
        table = await _open_vault_scrolled_to(app, pilot, 30)
        scroll_y = table.scroll_y
        cells = {ap.bssid: table.get_cell(ap.bssid, "ap") for ap in aps}

        update_meta(aps[29].ssid, aps[29].bssid, nickname="Upstairs")
        app.screen.query_one(VaultTable).reload_nicknames()
        await pilot.pause(0)

        assert "Upstairs" in table.get_cell(aps[29].bssid, "ap").plain
        assert [bssid for bssid, cell in cells.items() if table.get_cell(bssid, "ap") is not cell] == [aps[29].bssid]
        assert (table.cursor_row, table.scroll_y) == (30, scroll_y)


@pytest.mark.asyncio
async def test_deleting_another_ap_keeps_the_cursor_on_its_ap():
    app = _VaultApp()
    aps = _save_wep_aps(app.vault, 40)
    async with app.run_test(size=(80, 24)) as pilot:
        table = await _open_vault_scrolled_to(app, pilot, 30)

        app.vault.delete_ap(aps[2].bssid)
        await _wait_until(pilot, lambda: table.row_count == 39)
        await pilot.pause(0)

        assert _cursor_bssid(table) == aps[30].bssid


@pytest.mark.asyncio
async def test_a_second_ap_with_an_ssid_adds_the_bssid_to_both_rows():
    app = _VaultApp()
    aps = _save_wep_aps(app.vault, 3)
    async with app.run_test(size=(80, 24)) as pilot:
        drawer = await _open_vault(app, pilot)
        table = drawer.query_one("#vault-aps", DataTable)
        twin = AccessPoint(bssid="02:00:00:00:01:00", ssid=aps[1].ssid)

        app.vault.save_wep_key(twin, b"abcde")
        await _wait_until(pilot, lambda: table.row_count == 4)
        await pilot.pause(0)

        assert aps[1].bssid in table.get_cell(aps[1].bssid, "ap").plain
        assert twin.bssid in table.get_cell(twin.bssid, "ap").plain
        assert aps[0].bssid not in table.get_cell(aps[0].bssid, "ap").plain


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
async def test_a_refresh_shows_the_new_state_and_keeps_expansion_ticks_and_scroll(tmp_path):
    _write_a_checked_wep_key_and_three_hashlines(tmp_path)
    app = _VaultApp()
    async with app.run_test(size=(80, 16)) as pilot:
        item = await _open_item_view(app, pilot, "aa:bb:cc:dd:ee:ff")
        item.query_one(_KeyRow).toggle_button.press()
        uncracked = item.query_one(_UncrackedList)
        uncracked.deselect(uncracked.groups()[0])
        await pilot.pause(0)
        scrolled = item.max_scroll_y
        assert scrolled > 0
        item.scroll_to(y=scrolled, animate=False)
        await pilot.pause(0)

        update_meta("HomeNet", "aa:bb:cc:dd:ee:ff",
                    live_verifications=[{**_LIVE_CHECK, "at": 1000}, {**_LIVE_CHECK, "at": 2000}])
        await _reload_item_view(pilot, item, "aa:bb:cc:dd:ee:ff")

        row = item.query_one(_KeyRow)
        assert row.has_class("-expanded")
        assert len(row.query(".history-row")) == 2
        uncracked = item.query_one(_UncrackedList)
        assert [group in uncracked.selected for group in uncracked.groups()] == [False, True, True]
        assert "2 captures selected" in str(item.query_one(".selected-count", Label).content)
        assert item.scroll_y == scrolled


@pytest.mark.asyncio
async def test_a_refresh_keeps_the_uncracked_lists_scroll_focus_and_highlighted_row(tmp_path):
    lines = "".join(_EAPOL_LINE.replace("112233445566", f"1122334455{n:02x}") for n in range(12))
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", lines)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    app = _VaultApp()
    async with app.run_test() as pilot:
        item = await _open_item_view(app, pilot, "aa:bb:cc:dd:ee:ff")
        uncracked = item.query_one(_UncrackedList)
        uncracked.focus()
        uncracked.action_last()
        await pilot.pause(0)
        scrolled, highlighted = uncracked.scroll_y, uncracked.highlighted_group()
        assert scrolled > 0 and highlighted == uncracked.groups()[-1]

        update_meta("HomeNet", "aa:bb:cc:dd:ee:ff", live_verifications=[{**_LIVE_CHECK, "at": 2000}])
        await _reload_item_view(pilot, item, "aa:bb:cc:dd:ee:ff")

        uncracked = item.query_one(_UncrackedList)
        assert uncracked.scroll_y == scrolled
        assert app.focused is uncracked and uncracked.highlighted_group() == highlighted


@pytest.mark.asyncio
async def test_switching_aps_starts_the_uncracked_list_at_its_top(tmp_path):
    for bssid in ("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:fe"):
        lines = "".join(_EAPOL_LINE.replace("aabbccddeeff", bssid.replace(":", ""))
                        .replace("112233445566", f"1122334455{n:02x}") for n in range(12))
        _write(tmp_path, f"HomeNet_{bssid.replace(':', '-')}.hc22000", lines)
    app = _VaultApp()
    async with app.run_test() as pilot:
        item = await _open_item_view(app, pilot, "aa:bb:cc:dd:ee:ff")
        uncracked = item.query_one(_UncrackedList)
        uncracked.focus()
        uncracked.action_last()
        await pilot.pause(0)
        assert uncracked.scroll_y > 0

        item.load("aa:bb:cc:dd:ee:fe", "HomeNet")
        await _wait_until(pilot, lambda: item.query_one(_UncrackedList).groups()[0][0].line.mac_ap == "aabbccddeefe")
        await pilot.pause(0)

        uncracked = item.query_one(_UncrackedList)
        assert (uncracked.scroll_y, uncracked.highlighted) == (0, 0)


def _write_a_pmkid_ap(tmp_path, ssid: str, bssid: str) -> None:
    _write(tmp_path, f"{ssid}_{bssid.replace(':', '-')}.hc22000",
           _PMKID_LINE.replace("aabbccddeeff", bssid.replace(":", "")))


@pytest.mark.asyncio
async def test_a_key_press_shows_its_ap_at_once_and_held_keys_show_the_last_ap_once_they_stop(
        tmp_path, monkeypatch):
    for ssid, bssid in (("Bbb", "aa:bb:cc:dd:ee:01"), ("Ccc", "aa:bb:cc:dd:ee:02"), ("Ddd", "aa:bb:cc:dd:ee:03")):
        _write_a_pmkid_ap(tmp_path, ssid, bssid)
    monkeypatch.setattr(VaultDrawer, "_SELECTION_DELAY", 0.3)
    app = _VaultApp()
    async with app.run_test() as pilot:
        drawer = await _open_vault(app, pilot)
        item = drawer.query_one(VaultItemView)

        await pilot.press("down")
        assert item._bssid == "aa:bb:cc:dd:ee:02"
        await pilot.press("down")
        assert item._bssid == "aa:bb:cc:dd:ee:02"
        await _wait_until(pilot, lambda: item._bssid == "aa:bb:cc:dd:ee:03")


@pytest.mark.asyncio
async def test_a_reload_that_keeps_the_selected_ap_leaves_the_next_key_press_shown_at_once(
        tmp_path, monkeypatch):
    for ssid, bssid in (("Bbb", "aa:bb:cc:dd:ee:01"), ("Ccc", "aa:bb:cc:dd:ee:02"), ("Ddd", "aa:bb:cc:dd:ee:03")):
        _write_a_pmkid_ap(tmp_path, ssid, bssid)
    monkeypatch.setattr(VaultDrawer, "_SELECTION_DELAY", 0.3)
    app = _VaultApp()
    async with app.run_test() as pilot:
        drawer = await _open_vault(app, pilot, "aa:bb:cc:dd:ee:02")
        await pilot.pause(VaultDrawer._SELECTION_DELAY)
        item = drawer.query_one(VaultItemView)

        _write_a_pmkid_ap(tmp_path, "Aaa", "aa:bb:cc:dd:ee:00")
        app.vault.refresh()
        await _wait_until(pilot, lambda: drawer.query_one("#vault-aps", DataTable).row_count == 4)
        await pilot.pause(0)
        assert item._bssid == "aa:bb:cc:dd:ee:02"

        await pilot.press("down")
        assert item._bssid == "aa:bb:cc:dd:ee:03"


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


class _HeldVerifier(LiveKeyVerifier):
    """Answers SUCCESS once ``release`` is set; records each capture it was asked to check."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.checked: list[PersistedCapture] = []
        self.cancelled = False

    async def verify_credential(self, capture, array, target_type=None) -> VerifyResult:
        self.checked.append(capture)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return VerifyResult(VerifyStatus.SUCCESS, "Verified", "still works")


@pytest.mark.asyncio
async def test_one_live_check_runs_at_a_time_even_across_a_refresh(tmp_path):
    _write_a_checked_wep_key_and_three_hashlines(tmp_path)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_2000_wep_key.txt", _WEP_TXT.replace("6162636465", "6162636466"))
    app = _VaultApp()
    async with app.run_test() as pilot:
        item = await _open_item_view(app, pilot, "aa:bb:cc:dd:ee:ff")
        item.key_verifier = verifier = _HeldVerifier()

        def rows() -> list[_KeyRow]:
            return sorted(item.query(_KeyRow).results(_KeyRow), key=lambda r: r.key.value)

        rows()[0].verify_button.press()
        await _wait_until(pilot, lambda: bool(verifier.checked))
        update_meta("HomeNet", "aa:bb:cc:dd:ee:ff",
                    live_verifications=[{**_LIVE_CHECK, "at": 1000}, {**_LIVE_CHECK, "at": 2000}])
        await _reload_item_view(pilot, item, "aa:bb:cc:dd:ee:ff")

        assert [(str(r.verify_button.label), r.verify_button.disabled) for r in rows()] == [
            ("Verifying…", True), ("Verify", True)]
        rows()[1].verify_button.press()
        await pilot.pause(0)
        assert len(verifier.checked) == 1

        verifier.release.set()
        await app.workers.wait_for_complete()
        await _reload_item_view(pilot, item, "aa:bb:cc:dd:ee:ff")
        assert [(str(r.verify_button.label), r.verify_button.disabled) for r in rows()] == [
            ("Verify", False), ("Verify", False)]
        assert len(rows()[0].key.live_verifications) == 3


@pytest.mark.asyncio
async def test_removing_the_pane_cancels_its_running_live_check(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    app = _VaultApp()
    async with app.run_test() as pilot:
        item = await _open_item_view(app, pilot, "aa:bb:cc:dd:ee:ff")
        item.key_verifier = verifier = _HeldVerifier()
        item.query_one(_KeyRow).verify_button.press()
        await _wait_until(pilot, lambda: bool(verifier.checked))

        await app.pop_screen()
        await _wait_until(pilot, lambda: item.verifying is None)

        assert verifier.cancelled
        assert app.vault.ap_state("aa:bb:cc:dd:ee:ff").keys[0].live_verifications == ()
