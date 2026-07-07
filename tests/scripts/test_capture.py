import pytest
import subprocess
import re
from unittest.mock import patch, MagicMock
from wifit3.scripts.capture import Capture, LogHelper


def _capture(tmp_path):
    """Construct a Capture with its TemporaryDirectory pointed at tmp_path (so
    no real temp dir is created and logs land where the test can read them)."""
    with patch('wifit3.scripts.capture.tempfile.TemporaryDirectory') as mock_tempdir:
        mock_tempdir.return_value.name = str(tmp_path)
        return Capture()


def test_log_helper(tmp_path):
    logger = LogHelper(tmp_path)

    with patch('time.time', return_value=12345.678):
        logger.log_cmd(["echo", "hello"], "hello\n", 0, 12345.178, 0.5)

    log_file = tmp_path / "echo.log"
    assert log_file.exists()
    content = log_file.read_text()

    assert "-----------------------------------" in content
    assert "[12345.178] Executing: echo hello" in content
    assert "hello\n" in content
    assert "[12345.678] Execution completed in 0.500s, return code: 0" in content


# --- run_cmd: no scheduling, non-fatal by default, 1 s gap --------------------

def test_run_cmd_logs_and_returns_output(tmp_path):
    cap = _capture(tmp_path)
    mock_res = MagicMock(stdout="success_output", stderr="", returncode=0)

    with patch('subprocess.run', return_value=mock_res) as mock_run, \
         patch('time.sleep') as mock_sleep:
        output = cap.run_cmd(["fake_cmd"], timeout=5)

    assert output == "success_output"
    mock_run.assert_called_once_with(["fake_cmd"], capture_output=True, text=True, timeout=5)
    # The `Running: <cmd>` line (with a leading epoch) is the main.log contract.
    assert "Running: fake_cmd" in (tmp_path / "main.log").read_text()
    mock_sleep.assert_called_once_with(1)


def test_run_cmd_nonfatal_timeout_continues(tmp_path):
    cap = _capture(tmp_path)

    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(["fake"], 5)), \
         patch('time.sleep'), \
         patch.object(cap, 'throw') as mock_throw:
        output = cap.run_cmd(["fake_cmd"], fatal=False, timeout=5)

    mock_throw.assert_not_called()
    assert "TIMEOUT" in output


def test_run_cmd_fatal_timeout_throws(tmp_path):
    cap = _capture(tmp_path)

    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(["fake"], 5)), \
         patch('time.sleep'), \
         patch.object(cap, 'throw', side_effect=SystemExit) as mock_throw:
        with pytest.raises(SystemExit):
            cap.run_cmd(["fake_cmd"], fatal=True, timeout=5)
        mock_throw.assert_called_once()


# ---------------------------------------------------------------------------
# Pure text parsers — extracted as Capture staticmethods so they're callable
# without constructing Capture (which spins up a TemporaryDirectory). Fixtures
# are sanitized samples from usb_dumps/captures_*/ (no real BSSIDs).
# ---------------------------------------------------------------------------

# airmon-ng status table: PHY  Interface  Driver  Chipset
AIRMON_NG = """\
PHY\tInterface\tDriver\t\tChipset

phy0\twlan0\t\tiwlwifi\t\tIntel Corporation Wi-Fi 7 (802.11be)
phy4\twlan1\t\tath9k_htc\tQualcomm Atheros Communications AR9271 802.11n
"""

# Same table, base_iface absent (card never bound).
AIRMON_NG_NO_IFACE = """\
PHY\tInterface\tDriver\t\tChipset

phy0\twlan0\t\tiwlwifi\t\tIntel Corporation Wi-Fi 7 (802.11be)
"""

IW_DEV = """\
phy#4
\tInterface wlan1mon
\t\tifindex 6
\t\ttype monitor
\tInterface wlan1
\t\tifindex 5
\t\ttype managed
phy#0
\tInterface wlan0
\t\tifindex 3
\t\ttype managed
"""

# base_iface present, but no monitor vif on its phy.
IW_DEV_NO_MONITOR = """\
phy#4
\tInterface wlan1
\t\tifindex 5
\t\ttype managed
phy#0
\tInterface wlan0
\t\tifindex 3
\t\ttype managed
"""

IWLIST_5G = """\
wlan1mon  35 channels in total; available frequencies :
          Channel 01 : 2.412 GHz
          Channel 06 : 2.437 GHz
          Channel 36 : 5.18 GHz
          Channel 149 : 5.745 GHz
"""

IWLIST_24_ONLY = """\
wlan1mon  13 channels in total; available frequencies :
          Channel 01 : 2.412 GHz
          Channel 06 : 2.437 GHz
          Channel 11 : 2.462 GHz
"""


def test_parse_chipset_returns_driver_column():
    assert Capture.parse_chipset(AIRMON_NG, "wlan1") == "ath9k_htc"


def test_parse_chipset_missing_iface_returns_none():
    assert Capture.parse_chipset(AIRMON_NG_NO_IFACE, "wlan1") is None


def test_parse_chipset_folds_slash():
    line = "phy1\twlan1\trtl8821au/8811au\tRealtek"
    assert Capture.parse_chipset(line, "wlan1") == "rtl8821au_8811au"


def test_parse_chipset_empty_input():
    assert Capture.parse_chipset("", "wlan1") is None


def test_parse_monitor_iface_finds_monitor_on_same_phy():
    assert Capture.parse_monitor_iface(IW_DEV, "wlan1") == "wlan1mon"


def test_parse_monitor_iface_none_when_no_monitor():
    assert Capture.parse_monitor_iface(IW_DEV_NO_MONITOR, "wlan1") is None


def test_detect_5g_true_when_present():
    assert Capture.detect_5g(IWLIST_5G) is True


def test_detect_5g_false_for_24_only():
    assert Capture.detect_5g(IWLIST_24_ONLY) is False


def test_next_capture_paths_empty_dir(tmp_path):
    pcap, logs = Capture.next_capture_paths(tmp_path)
    assert pcap == tmp_path / "capture-1.pcap"
    assert logs == tmp_path / "capture-1_logs"


def test_next_capture_paths_increments_past_existing(tmp_path):
    for n in (1, 2, 3):
        (tmp_path / f"capture-{n}.pcap").touch()
    pcap, logs = Capture.next_capture_paths(tmp_path)
    assert pcap == tmp_path / "capture-4.pcap"
    assert logs == tmp_path / "capture-4_logs"


def test_main_log_line_format(tmp_path):
    # The leading `[<epoch>.3f]` is the format pcap_slicer.parse_log depends on.
    LogHelper(tmp_path).log_main("hello world")
    line = (tmp_path / "main.log").read_text().splitlines()[0]
    assert re.match(r"^\[\d+\.\d{3}\] hello world$", line)


# --- modinfo + lsusb parsing (driver/firmware/source extraction) -------------

MODINFO = """\
filename:       /lib/modules/6.12.0/updates/dkms/8814au.ko
version:        5.8.5.1
srcversion:     ABCDEF0123456789
firmware:       rtw88/rtw8814au_fw.bin
vermagic:       6.12.0-kali1-amd64 SMP preempt mod_unload modversions
depends:        cfg80211
"""

MODINFO_MULTI_FW = """\
filename:       /lib/modules/6.12.0/kernel/drivers/net/wireless/foo.ko
firmware:       foo/main.bin
firmware:       foo/cal.bin
vermagic:       6.12.0-kali1-amd64
"""

# Generic Realtek VID:PID (public hardware id, not a network identifier).
LSUSB_BEFORE = """\
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 003: ID 8087:0026 Intel Corp. AX211 Bluetooth
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
"""
LSUSB_AFTER = LSUSB_BEFORE + "Bus 001 Device 007: ID 0bda:8813 Realtek RTL8814AU\n"


def test_parse_modinfo_fields():
    fields = Capture.parse_modinfo(MODINFO)
    assert fields["version"] == "5.8.5.1"
    assert fields["srcversion"] == "ABCDEF0123456789"
    assert fields["filename"].endswith("8814au.ko")
    assert fields["vermagic"].startswith("6.12.0-kali1-amd64")


def test_parse_modinfo_firmware_single():
    assert Capture.parse_modinfo_firmware(MODINFO) == ["rtw88/rtw8814au_fw.bin"]


def test_parse_modinfo_firmware_multiple():
    assert Capture.parse_modinfo_firmware(MODINFO_MULTI_FW) == ["foo/main.bin", "foo/cal.bin"]


def test_parse_modinfo_firmware_none():
    assert Capture.parse_modinfo_firmware("filename: x\nvermagic: y\n") == []


def test_lsusb_diff_reports_new_device():
    assert Capture.lsusb_diff(LSUSB_BEFORE, LSUSB_AFTER) == [
        "Bus 001 Device 007: ID 0bda:8813 Realtek RTL8814AU"
    ]


def test_lsusb_diff_empty_when_unchanged():
    assert Capture.lsusb_diff(LSUSB_BEFORE, LSUSB_BEFORE) == []


# --- cleanup save decision: no more captures_unknown/ ------------------------

# --- DKMS source resolution (ties source to the BOUND module) ----------------

DKMS_CONF_8188EUS = """\
PACKAGE_NAME="rtl8188eus"
PACKAGE_VERSION="5.3.9"
BUILT_MODULE_NAME[0]="8188eu"
DEST_MODULE_LOCATION[0]="/updates/dkms"
AUTOINSTALL="yes"
"""


def test_dkms_conf_ids_collects_package_and_module(tmp_path):
    conf = tmp_path / "dkms.conf"
    conf.write_text(DKMS_CONF_8188EUS)
    # package name != built .ko name — we need both to match the bound module.
    assert Capture._dkms_conf_ids(conf) == {"rtl8188eus", "8188eu"}


def test_dkms_conf_ids_skips_variable_refs(tmp_path):
    conf = tmp_path / "dkms.conf"
    conf.write_text('PACKAGE_NAME="88x2bu"\nBUILT_MODULE_NAME[0]="$PACKAGE_NAME"\n')
    assert Capture._dkms_conf_ids(conf) == {"88x2bu"}


def test_best_dkms_match_exact_built_module_over_other_packages():
    # Bound module 8188eu must select rtl8188eus, NOT the first-listed 8812au —
    # the multi-DKMS-installed case the old code got wrong.
    from pathlib import Path
    cands = [
        (Path("/usr/src/rtl8812au-5.6.4.2"), {"rtl8812au", "88xxau"}),
        (Path("/usr/src/rtl8188eus-5.3.9"), {"rtl8188eus", "8188eu"}),
    ]
    assert Capture._best_dkms_match("8188eu", cands) == Path("/usr/src/rtl8188eus-5.3.9")


def test_best_dkms_match_substring_when_driver_name_differs():
    # Bound driver name `rtl8812au` matches package `8812au` (built `88XXau`).
    from pathlib import Path
    cands = [
        (Path("/usr/src/8812au-5.6.4.2"), {"8812au", "88xxau"}),
        (Path("/usr/src/rtl8188eus-5.3.9"), {"rtl8188eus", "8188eu"}),
    ]
    assert Capture._best_dkms_match("rtl8812au", cands) == Path("/usr/src/8812au-5.6.4.2")


def test_best_dkms_match_none_when_unrelated():
    from pathlib import Path
    assert Capture._best_dkms_match("8188eu", [(Path("/usr/src/nvidia-535"), {"nvidia"})]) is None


def test_cleanup_skips_save_when_unknown_and_no_pcap(tmp_path):
    cap = _capture(tmp_path)
    cap.chipset = "unknown"  # airmon never bound the card
    with patch('subprocess.run'), patch('time.sleep'), \
         patch.object(cap, '_save_artifacts') as mock_save:
        cap.cleanup()
    mock_save.assert_not_called()


def test_cleanup_saves_to_repo_when_chipset_known(tmp_path):
    cap = _capture(tmp_path)
    cap.chipset = "ath9k_htc"
    with patch('subprocess.run'), patch('time.sleep'), \
         patch.object(cap, '_save_artifacts') as mock_save, \
         patch.object(cap, 'collect_driver_artifacts'):
        cap.cleanup()
    mock_save.assert_called_once()
    dest_dir = mock_save.call_args[0][0]
    assert dest_dir.name == "captures_ath9k_htc"


# --- injection plan: per-band passes + the 5 GHz supports_5g gate -------------
# Fake BSSIDs (AA:BB:... — not network identifiers).

def test_injection_plan_legacy_target_pins_ch1(tmp_path):
    cap = _capture(tmp_path)
    cap.target_bssid, cap.client_bssid = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
    assert cap._injection_plan() == [(1, "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02", "CH1")]


def test_injection_plan_5g_dropped_without_support(tmp_path):
    # --bssid5g passed but the card is 2.4-only → the 5 GHz pass is a no-op,
    # while the 2.4 GHz pass still runs.
    cap = _capture(tmp_path)
    cap.bssid2g, cap.channel2g, cap.client2g = "AA:BB:CC:DD:EE:01", 6, None
    cap.bssid5g, cap.channel5g, cap.client5g = "AA:BB:CC:DD:EE:03", 157, "AA:BB:CC:DD:EE:04"
    cap.supports_5g = False
    plan = cap._injection_plan()
    assert (6, "AA:BB:CC:DD:EE:01", None, "2G") in plan
    assert all(label != "5G" for *_, label in plan)


def test_injection_plan_5g_included_when_supported(tmp_path):
    cap = _capture(tmp_path)
    cap.bssid5g, cap.channel5g, cap.client5g = "AA:BB:CC:DD:EE:03", 157, "AA:BB:CC:DD:EE:04"
    cap.supports_5g = True
    assert (157, "AA:BB:CC:DD:EE:03", "AA:BB:CC:DD:EE:04", "5G") in cap._injection_plan()


def test_injection_plan_empty_with_no_targets(tmp_path):
    assert _capture(tmp_path)._injection_plan() == []


def test_parse_wifi_ifaces_excludes_p2p():
    txt = "phy#0\n\tInterface wlan0\n\t\ttype managed\nphy#4\n\tInterface p2p-dev-wlan0\n\tInterface wlan1\n"
    assert Capture.parse_wifi_ifaces(txt) == {"wlan0", "wlan1"}


def test_appeared_iface_picks_the_new_one():
    after = Capture.parse_wifi_ifaces("\tInterface wlan0\n\tInterface wlan1\n")
    assert sorted(after - {"wlan0"})[-1] == "wlan1"
