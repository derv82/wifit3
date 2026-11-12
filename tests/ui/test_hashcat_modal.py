import time
from datetime import datetime

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox

import wifit3.ui.vault.modals.hashcat as mod
from wifit3.crack.passwords import (DEFAULT_PASSWORDS_PATH, all_ssid_variants, read_passwords, ssid_passwords,
                                    ssid_variants)
from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.config import Config, ConfigError, HashcatSettings
from wifit3.persist.meta import update_meta
from wifit3.ui.vault.modals.cpu_crack import CpuCrackConfigModal
from wifit3.ui.vault.modals.hashcat import HashcatConfigModal, _default_hashcat_path
from wifit3.ui.vault.modals.wordlists import _UseCheckbox, _exhausted_tooltip
from wifit3.crack.hc22000_format import hashline_id, parse_hc22000
from wifit3.persist.ap_state import DatedHashline
from wifit3.persist.exhaustion import Exhaustion, wordlist_identity
from tests.ui.wait_until import wait_until


@pytest.fixture(autouse=True)
def _unset_saved_paths(monkeypatch):
    """These live on Config as process-wide state; pin them so test order can't matter."""
    monkeypatch.setattr(Config.hashcat, "path", None)
    monkeypatch.setattr(Config.hashcat, "wordlists", [])
    monkeypatch.setattr(Config.hashcat, "args", None)


class _Host(App):
    """A bare app to host the modal (no USB, no splash)."""


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


def _dated(raw: str) -> DatedHashline:
    return DatedHashline(hashline_id(raw), raw, parse_hc22000(raw), ())


async def test_password_source_labels_count_the_chosen_hashlines_ssids_or_one_ssid_and_the_probed(tmp_path):
    chosen = f"WPA*01*{'00' * 16}*020000000001*020000000002*6162***"
    capture = tmp_path / "ab_02-00-00-00-00-01.hc22000"
    capture.write_text(f"{chosen}\n"
                       f"WPA*01*{'00' * 16}*020000000001*020000000003*6364***\n", encoding="utf-8")
    persisted = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=str(capture),
                                 bssid="02:00:00:00:00:01")
    default_count = sum(1 for _ in read_passwords(DEFAULT_PASSWORDS_PATH))
    app = _Host()
    async with app.run_test() as pilot:
        for modal, ssid_label in (
                (CpuCrackConfigModal(bssid=persisted.bssid, probed_ssids=["CafeGuest"], hashlines=[_dated(chosen)]),
                 f"This AP's SSID variants ({len(all_ssid_variants([('ab', '02:00:00:00:00:01')]))})"),
                (HashcatConfigModal(confirm_label="Save", probed_ssids=["CafeGuest"]),
                 f"This AP's SSID variants (about {len(ssid_variants('HomeWifi')):,} per SSID)")):
            await app.push_screen(modal)
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
        modal = CpuCrackConfigModal(bssid=persisted.bssid, hashlines=[_dated(line) for line in lines])
        await app.push_screen(modal)
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
        modal = CpuCrackConfigModal(bssid=persisted.bssid, hashlines=[_dated(line)])
        await app.push_screen(modal)
        await app.workers.wait_for_complete()
        await pilot.pause()
        exhausted_box, fresh_box = modal.query(_UseCheckbox)
        assert (exhausted_box.value, fresh_box.value) == (False, True)
        await wait_until(pilot, lambda: modal._launch_config()["wordlists"] == [str(fresh_list)])
        exhausted_box.value = True
        await wait_until(pilot, lambda: len(modal._launch_config()["wordlists"]) == 2)
        await modal._show_wordlists()
        assert modal.query(_UseCheckbox).first().value is True
        assert modal._launch_config()["wordlists"] == [str(exhausted_list), str(fresh_list)]


async def test_deselecting_a_wordlist_skips_it_in_the_launch_but_keeps_it_saved():
    Config.cpu_crack.wordlists = [r"D:\a.txt", r"D:\b.txt"]
    app = _Host()
    async with app.run_test() as pilot:
        modal = CpuCrackConfigModal()
        await app.push_screen(modal)
        await pilot.pause()
        modal.query(_UseCheckbox).first().value = False
        await pilot.pause()
        config = modal._launch_config()
        modal._remember(config)
    assert config["wordlists"] == [r"D:\b.txt"]
    assert Config.cpu_crack.wordlists == [r"D:\a.txt", r"D:\b.txt"]


def test_exhausted_today_is_not_zero_days_ago():
    tooltip = _exhausted_tooltip(Exhaustion("9:100", ("a.txt",), 1, 2, int(time.time()) - 60))
    assert "(today)" in tooltip and "days ago" not in tooltip
