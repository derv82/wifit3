import time
from datetime import datetime
from pathlib import Path

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label

import wifit3.ui.vault.modals.hashcat as mod
from wifit3.crack.passwords import DEFAULT_PASSWORDS_PATH, read_passwords, ssid_passwords, ssid_variants
from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.config import Config, ConfigError, HashcatSettings
from wifit3.persist.meta import update_meta
from wifit3.ui.vault.modals.cpu_crack import CpuCrackConfigModal
from wifit3.ui.vault.modals.hashcat import HashcatConfigModal, _default_hashcat_path
from wifit3.ui.vault.modals.wordlists import _RemoveButton, _UseCheckbox, _exhausted_tooltip
from wifit3.vault.exhaustion import Exhaustion, hashline_id, wordlist_identity


@pytest.fixture(autouse=True)
def _unset_saved_paths(monkeypatch):
    """These live on Config as process-wide state; pin them so test order can't matter."""
    monkeypatch.setattr(Config.hashcat, "path", None)
    monkeypatch.setattr(Config.hashcat, "wordlists", [])
    monkeypatch.setattr(Config.hashcat, "args", None)


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


@pytest.mark.asyncio
@pytest.mark.parametrize("modal_type", [HashcatConfigModal, CpuCrackConfigModal])
async def test_cracker_modal_fits_viewport_and_scrolls_fields_inside(modal_type):
    app = _Host()
    async with app.run_test(size=(40, 9)) as pilot:
        app.push_screen(modal_type())
        await pilot.pause()

        dialog = app.screen.query_one("#wordlist-dialog")
        body = app.screen.query_one("#wordlist-body", VerticalScroll)
        title = app.screen.query_one("#wordlist-title", Label)
        buttons = app.screen.query_one("#wordlist-buttons")
        assert dialog.region.x >= 0 and dialog.region.right <= app.size.width
        assert dialog.region.y >= 0 and dialog.region.bottom <= app.size.height
        assert body.virtual_size.height > body.size.height
        assert body.region.bottom == dialog.content_region.bottom
        assert body.parent is dialog
        assert title.parent is body and buttons.parent is body
        body.scroll_end(animate=False)
        await pilot.pause()
        assert buttons.region.bottom <= body.content_region.bottom


def test_default_path_prefers_which(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/somewhere/hashcat")
    assert _default_hashcat_path() == "/somewhere/hashcat"


def test_default_path_falls_back_per_os(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert _default_hashcat_path() == "/usr/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert _default_hashcat_path() == "/opt/homebrew/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert _default_hashcat_path() == r"C:\hashcat\hashcat.exe"


def test_saved_path_outranks_a_hashcat_found_on_path(monkeypatch):
    """Someone who points wifit3 at a specific build must not be silently overridden by
    whatever happens to be on $PATH."""
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/somewhere/hashcat")
    monkeypatch.setattr(Config.hashcat, "path", r"D:\tools\hashcat\hashcat.exe")
    assert _default_hashcat_path() == r"D:\tools\hashcat\hashcat.exe"


def test_launching_remembers_the_launch_settings():
    """Straight through the real Config.save(): tests/ui/conftest.py points it at a tmp file."""
    modal = HashcatConfigModal(probed_ssids=["CafeGuest"])
    modal._wordlists = [r"D:\rockyou.txt", r"D:\top29m.txt"]
    modal._remember({"hashcat_exe": r"D:\hashcat.exe",
                     "wordlists": [r"D:\rockyou.txt", r"D:\top29m.txt"],
                     "hashcat_args": "-w 3", "default_passwords": False,
                     "ap_ssid_passwords": True, "probed_ssid_passwords": False})
    Config.hashcat = HashcatSettings()
    Config.load()

    assert Config.hashcat == HashcatSettings(
        path=r"D:\hashcat.exe", args="-w 3", wordlists=[r"D:\rockyou.txt", r"D:\top29m.txt"],
        default_passwords=False, ap_ssid_passwords=True, probed_ssid_passwords=False)


def test_an_unwritable_config_warns_but_keeps_the_paths(monkeypatch):
    """A failed save must not cost the user their launch."""
    def _boom(cls):
        raise ConfigError("disk full")
    monkeypatch.setattr(Config, "save", classmethod(_boom))

    modal = HashcatConfigModal()
    warnings = []
    monkeypatch.setattr(modal, "notify", lambda *a, **k: warnings.append((a, k)))
    modal._remember({"hashcat_exe": r"D:\hashcat.exe", "wordlists": [r"D:\rockyou.txt"],
                     "hashcat_args": "", "default_passwords": True, "ap_ssid_passwords": True,
                     "probed_ssid_passwords": True})

    assert Config.hashcat.path == r"D:\hashcat.exe"
    assert warnings and warnings[0][1].get("title") == "Config Error"


async def test_the_self_test_checkbox_edits_the_args_text():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashcatConfigModal())
        await pilot.pause(0)
        args = app.screen.query_one("#hashcat-args", Input)
        args.value = "-w 3"
        checkbox = app.screen.query_one("#hashcat-self-test-disable", Checkbox)

        checkbox.value = True
        await pilot.pause(0)
        assert args.value == "-w 3 --self-test-disable"

        checkbox.value = False
        await pilot.pause(0)
        assert args.value == "-w 3"


async def test_saved_args_prefill_the_modal_and_come_back_on_launch():
    Config.hashcat.args = "-w 3 --self-test-disable"
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(probed_ssids=["CafeGuest"]), dismissed.append)
        await pilot.pause(0)
        assert app.screen.query_one("#hashcat-self-test-disable", Checkbox).value
        app.screen.query_one("#hashcat-exe", Input).value = r"D:\hashcat.exe"
        await app.screen._wordlists_chosen([Path(r"D:\rockyou.txt")])
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

    assert dismissed == [{"hashcat_exe": r"D:\hashcat.exe", "wordlists": [r"D:\rockyou.txt"],
                          "default_passwords": True, "ap_ssid_passwords": True,
                          "probed_ssid_passwords": True, "hashcat_args": "-w 3 --self-test-disable"}]


async def test_escape_dismisses_without_saving():
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause(0)
        app.screen.query_one("#hashcat-args", Input).value = "-w 3"
        await pilot.press("escape")
        await pilot.pause(0)

        assert not isinstance(app.screen, HashcatConfigModal)

    assert dismissed == [None]
    assert Config.hashcat.args is None


async def test_the_modal_reads_back_the_wordlists_it_was_given():
    Config.hashcat.wordlists = [r"D:\a.txt", r"D:\b.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashcatConfigModal())
        await pilot.pause(0)
        shown = [str(box.label) for box in app.screen.query(_UseCheckbox)]
        assert len(shown) == 2
        assert "a.txt" in shown[0] and "b.txt" in shown[1]

        await app.screen._wordlists_chosen(None)        # a cancelled picker keeps the selection
        await pilot.pause(0)
        assert app.screen._wordlists == [r"D:\a.txt", r"D:\b.txt"]


async def test_launch_is_refused_without_a_wordlist_or_password_source():
    Config.hashcat.default_passwords = False
    Config.hashcat.ap_ssid_passwords = False
    Config.hashcat.probed_ssid_passwords = False
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause(0)
        refusals = []
        app.screen.notify = lambda *a, **k: refusals.append(a)
        app.screen.query_one("#hashcat-exe", Input).value = r"D:\hashcat.exe"
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

        assert refusals and not dismissed
        assert isinstance(app.screen, HashcatConfigModal)


async def test_x_removes_one_wordlist_and_clear_removes_them_all(tmp_path):
    Config.hashcat.wordlists = [str(tmp_path / "a.txt"), str(tmp_path / "b.txt"), str(tmp_path / "c.txt")]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause()
        modal = app.screen
        rows = modal.query_one("#wordlists")
        clear = modal.query_one("#wordlist-clear", Button)
        assert clear.display

        next(b for b in modal.query(_RemoveButton) if b.path.endswith("b.txt")).press()
        await pilot.pause()
        names = [str(box.label) for box in rows.query(_UseCheckbox)]
        assert len(names) == 2
        assert names[0].startswith("a.txt") and names[1].startswith("c.txt")
        assert str(tmp_path) in names[0], "the parent directory tells same-named files apart"

        clear.press()
        await pilot.pause()
        assert modal._wordlists == []
        assert not clear.display
        assert "none chosen" in str(rows.query_one(Label).content)
        assert Config.hashcat.wordlists == [str(tmp_path / "a.txt"), str(tmp_path / "b.txt"),
                                         str(tmp_path / "c.txt")], "nothing is saved before Launch"


async def test_launch_saves_what_is_left_after_a_removal(tmp_path):
    Config.hashcat.wordlists = [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause()
        modal = app.screen
        modal.query_one("#hashcat-exe", Input).value = str(tmp_path / "hashcat")
        modal.query(_RemoveButton).first().press()
        await pilot.pause(0)
        modal.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

    assert dismissed[0]["wordlists"] == [str(tmp_path / "b.txt")]
    assert Config.hashcat.wordlists == [str(tmp_path / "b.txt")]


async def test_clear_is_hidden_when_no_wordlist_is_chosen():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(HashcatConfigModal())
        await pilot.pause(0)
        assert not app.screen.query_one("#wordlist-clear", Button).display


async def test_launch_is_refused_without_the_executable():
    Config.hashcat.wordlists = [r"D:\rockyou.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(HashcatConfigModal(), dismissed.append)
        await pilot.pause(0)
        refusals = []
        app.screen.notify = lambda *a, **k: refusals.append(a)
        app.screen.query_one("#hashcat-exe", Input).value = ""
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

        assert refusals and not dismissed


async def test_cpu_crack_launches_with_only_the_wordlists_and_remembers_them():
    Config.cpu_crack.wordlists = [r"D:\a.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(CpuCrackConfigModal(probed_ssids=["CafeGuest"]), dismissed.append)
        await pilot.pause()
        assert str(app.screen.query_one("#wordlist-title", Label).content) == "CPU Crack"
        assert not app.screen.query("#hashcat-exe")
        await app.screen._wordlists_chosen([Path(r"D:\a.txt"), Path(r"D:\b.txt")])
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

    assert dismissed == [{"wordlists": [r"D:\a.txt", r"D:\b.txt"], "default_passwords": True,
                          "ap_ssid_passwords": True, "probed_ssid_passwords": True}]
    assert Config.cpu_crack.wordlists == [r"D:\a.txt", r"D:\b.txt"]
    assert Config.hashcat.wordlists == []


async def test_no_probed_ssids_disables_their_box_but_keeps_the_saved_choice():
    Config.cpu_crack.probed_ssid_passwords = True
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(CpuCrackConfigModal(confirm_label="Save"), dismissed.append)
        await pilot.pause(0)
        probed = app.screen.query_one("#probed-ssid-passwords", Checkbox)
        assert (probed.value, probed.disabled, str(probed.label)) == (False, True, "Probed SSIDs (0)")
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

    assert dismissed[0]["probed_ssid_passwords"] is False
    assert Config.cpu_crack.probed_ssid_passwords is True


async def test_cpu_crack_refuses_to_launch_without_a_wordlist_or_password_source():
    Config.cpu_crack.wordlists = []
    Config.cpu_crack.default_passwords = False
    Config.cpu_crack.ap_ssid_passwords = False
    Config.cpu_crack.probed_ssid_passwords = False
    app = _Host()
    async with app.run_test() as pilot:
        dismissed = []
        app.push_screen(CpuCrackConfigModal(), dismissed.append)
        await pilot.pause(0)
        refusals = []
        app.screen.notify = lambda *a, **k: refusals.append(a)
        app.screen.query_one("#wordlist-launch", Button).press()
        await pilot.pause(0)

        assert refusals and not dismissed


class _HostWithGlobalButtonWidth(App):
    CSS = "Button { min-width: 12; }"


async def test_remove_and_clear_shrink_below_the_global_button_width_in_both_modals():
    Config.hashcat.wordlists = [r"D:\a.txt"]
    Config.cpu_crack.wordlists = [r"D:\a.txt"]
    for modal_type in (HashcatConfigModal, CpuCrackConfigModal):
        app = _HostWithGlobalButtonWidth()
        async with app.run_test() as pilot:
            app.push_screen(modal_type())
            await pilot.pause()
            assert app.screen.query_one(_RemoveButton).size.width == 3
            assert app.screen.query_one("#wordlist-clear", Button).size.width == 7


async def test_password_source_labels_count_the_capture_or_one_ssid_and_the_probed(tmp_path):
    capture = tmp_path / "ab_02-00-00-00-00-01.hc22000"
    capture.write_text(f"WPA*01*{'00' * 16}*020000000001*020000000002*6162***\n"
                       f"WPA*01*{'00' * 16}*020000000001*020000000003*6162***\n", encoding="utf-8")
    persisted = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=str(capture),
                                 bssid="02:00:00:00:00:01")
    default_count = sum(1 for _ in read_passwords(DEFAULT_PASSWORDS_PATH))
    app = _Host()
    async with app.run_test() as pilot:
        for modal, ssid_label in (
                (CpuCrackConfigModal(capture=persisted, probed_ssids=["CafeGuest"]),
                 f"This AP's SSID variants ({len(ssid_variants('ab'))})"),
                (HashcatConfigModal(confirm_label="Save", probed_ssids=["CafeGuest"]),
                 f"This AP's SSID variants (about {len(ssid_variants('HomeWifi')):,} per SSID)")):
            app.push_screen(modal)
            await app.workers.wait_for_complete()
            await pilot.pause()
            labels = [str(checkbox.label) for checkbox in modal.query(Checkbox)
                      if checkbox.id != "hashcat-self-test-disable"]
            assert labels == [f"Default passwords ({default_count:,})", ssid_label,
                              f"Probed SSIDs ({len(ssid_passwords(['CafeGuest']))})"]
            app.pop_screen()


async def test_exhausted_wordlists_and_sources_are_marked_with_when_and_on_how_many(tmp_path):
    lines = [f"WPA*01*{'00' * 16}*020000000001*02000000000{n}*6162***" for n in (2, 3)]
    capture = tmp_path / "ab_02-00-00-00-00-01.hc22000"
    capture.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    persisted = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=str(capture),
                                 bssid="02:00:00:00:00:01", ssid="ab")
    exhausted_list, fresh_list = tmp_path / "exhausted.txt", tmp_path / "fresh.txt"
    exhausted_list.write_text("password1\n", encoding="utf-8")
    fresh_list.write_text("password2\npassword3\n", encoding="utf-8")
    Config.cpu_crack.wordlists = [str(exhausted_list), str(fresh_list)]
    when = int(time.time()) - 3 * 86400 - 60
    ids = [hashline_id(line) for line in lines]
    update_meta("ab", "02:00:00:00:00:01", exhausted={
        wordlist_identity(exhausted_list): {ids[0]: when},
        wordlist_identity(DEFAULT_PASSWORDS_PATH): {ids[0]: when - 60, ids[1]: when}})
    date = datetime.fromtimestamp(when).strftime("%Y-%m-%d")
    app = _Host()
    async with app.run_test() as pilot:
        modal = CpuCrackConfigModal(capture=persisted)
        app.push_screen(modal)
        await app.workers.wait_for_complete()
        await pilot.pause()
        exhausted_row, fresh_row = modal.query(".wordlist-row")
        default = modal.query_one("#default-passwords", Checkbox)
        assert exhausted_row.has_class("exhausted") and default.has_class("exhausted")
        assert not fresh_row.has_class("exhausted")
        assert exhausted_row.styles.background != fresh_row.styles.background
        assert not modal.query_one("#ap-ssid-passwords", Checkbox).has_class("exhausted")
        assert exhausted_row.tooltip == f"Exhausted {date} (3 days ago) on 1/2 hashlines"
        assert default.tooltip == f"Exhausted {date} (3 days ago) on 2/2 hashlines"


async def test_exhausted_rows_start_unchecked_and_one_click_restores_them(tmp_path):
    line = f"WPA*01*{'00' * 16}*020000000001*020000000002*6162***"
    capture = tmp_path / "ab_02-00-00-00-00-01.hc22000"
    capture.write_text(f"{line}\n", encoding="utf-8")
    persisted = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=str(capture),
                                 bssid="02:00:00:00:00:01", ssid="ab")
    exhausted_list, fresh_list = tmp_path / "exhausted.txt", tmp_path / "fresh.txt"
    exhausted_list.write_text("password1\n", encoding="utf-8")
    fresh_list.write_text("password2\n", encoding="utf-8")
    Config.cpu_crack.wordlists = [str(exhausted_list), str(fresh_list)]
    update_meta("ab", "02:00:00:00:00:01", exhausted={
        wordlist_identity(exhausted_list): {hashline_id(line): int(time.time())}})
    app = _Host()
    async with app.run_test() as pilot:
        modal = CpuCrackConfigModal(capture=persisted)
        app.push_screen(modal)
        await app.workers.wait_for_complete()
        await pilot.pause()
        exhausted_box, fresh_box = modal.query(_UseCheckbox)
        assert (exhausted_box.value, fresh_box.value) == (False, True)
        assert modal._launch_config()["wordlists"] == [str(fresh_list)]
        exhausted_box.value = True
        await pilot.pause()
        await modal._show_wordlists()
        assert modal.query(_UseCheckbox).first().value is True
        assert modal._launch_config()["wordlists"] == [str(exhausted_list), str(fresh_list)]


async def test_deselecting_a_wordlist_skips_it_in_the_launch_but_keeps_it_saved():
    Config.cpu_crack.wordlists = [r"D:\a.txt", r"D:\b.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        modal = CpuCrackConfigModal()
        app.push_screen(modal)
        await pilot.pause()
        modal.query(_UseCheckbox).first().value = False
        await pilot.pause()
        config = modal._launch_config()
        modal._remember(config)
    assert config["wordlists"] == [r"D:\b.txt"]
    assert Config.cpu_crack.wordlists == [r"D:\a.txt", r"D:\b.txt"]


def test_exhausted_today_is_not_zero_days_ago():
    tooltip = _exhausted_tooltip(Exhaustion(1, 2, int(time.time()) - 60))
    assert "(today)" in tooltip and "days ago" not in tooltip
