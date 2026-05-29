"""Unit tests for ui/capture_log.py — the per-frame EAPOL trace markup."""
from wifit3.ui.capture_events import CaptureEvent, CaptureKind
from wifit3.ui.capture_log import eapol_message_markup


def _eapol(msg_num, *, has_nonce, has_mic, eapol_complete, useful):
    return CaptureEvent(
        kind=CaptureKind.EAPOL,
        bssid="aa:bb:cc:dd:ee:ff",
        client_mac="11:22:33:44:55:66",
        ssid="Net",
        msg_num=msg_num,
        has_nonce=has_nonce,
        has_mic=has_mic,
        eapol_complete=eapol_complete,
        useful=useful,
    )


def test_m1_is_anonce_donor_bold():
    m = eapol_message_markup(_eapol(1, has_nonce=True, has_mic=False,
                                    eapol_complete=False, useful=True))
    assert m.startswith("[bold cyan]M1[/bold cyan]")     # useful → bold
    assert "ANonce [green]✓[/green]" in m
    assert "MIC" not in m                                # M1 carries no MIC


def test_m2_complete_keystone_all_green_bold():
    m = eapol_message_markup(_eapol(2, has_nonce=True, has_mic=True,
                                    eapol_complete=True, useful=True))
    assert m.startswith("[bold cyan]M2[/bold cyan]")
    assert "SNonce [green]✓[/green]" in m
    assert "MIC [green]✓[/green]" in m
    assert "EAPOL complete [green]✓[/green]" in m


def test_m2_clipped_is_dim_with_red_eapol():
    # The exact failure that lost a capture: a complete SNonce + MIC but a
    # clipped 802.1X payload → not a usable keystone → dim label, red EAPOL tick.
    m = eapol_message_markup(_eapol(2, has_nonce=True, has_mic=True,
                                    eapol_complete=False, useful=False))
    assert m.startswith("[dim cyan]M2[/dim cyan]")       # degraded → dim
    assert "EAPOL clipped [red]✗[/red]" in m
    assert "[black bold on" not in m                     # never a win chip


def test_m3_shows_anonce_and_mic_no_eapol():
    m = eapol_message_markup(_eapol(3, has_nonce=True, has_mic=True,
                                    eapol_complete=True, useful=True))
    assert "ANonce [green]✓[/green]" in m
    assert "MIC [green]✓[/green]" in m
    assert "EAPOL" not in m                              # M3's own EAPOL is unused


def test_m4_zeroed_nonce_is_dim_with_red_snonce():
    m = eapol_message_markup(_eapol(4, has_nonce=False, has_mic=True,
                                    eapol_complete=True, useful=False))
    assert m.startswith("[dim cyan]M4[/dim cyan]")
    assert "SNonce [red]✗[/red]" in m                    # no echoed SNonce


def test_unclassified_frame_label_only():
    m = eapol_message_markup(_eapol(0, has_nonce=False, has_mic=False,
                                    eapol_complete=False, useful=False))
    assert m == "[dim cyan]EAPOL-?[/dim cyan]"
