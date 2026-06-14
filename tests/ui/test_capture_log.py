"""Unit tests for ui/capture_log.py — the per-frame EAPOL trace markup."""
from wifit3.ui.capture_events import CaptureEvent, CaptureKind
from wifit3.ui.capture_log import eapol_message_markup, short_sta


def _eapol(msg_num, *, has_nonce, has_mic, eapol_complete, useful, has_pmkid=None):
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
        has_pmkid=has_pmkid,
    )


def test_short_sta_trims_to_last_three_octets():
    assert short_sta("11:22:33:44:55:66") == "…44:55:66"
    assert short_sta("") == "…"


def test_line_carries_handshake_context_prefix_with_client_dir():
    # A lone "M2 …" line is cryptic; the dim prefix (handshake + client +
    # direction) makes it self-explanatory.
    m = eapol_message_markup(_eapol(2, has_nonce=True, has_mic=True,
                                    eapol_complete=True, useful=True))
    assert "4-Way Handshake" in m
    assert "…44:55:66→AP" in m                           # M2 travels STA→AP


def test_m1_is_anonce_donor_bold():
    m = eapol_message_markup(_eapol(1, has_nonce=True, has_mic=False,
                                    eapol_complete=False, useful=True))
    assert "[bold cyan]M1[/bold cyan]" in m              # useful → bold
    assert "ANonce[green]✓[/green]" in m
    assert "AP→…44:55:66" in m                           # M1 travels AP→STA
    assert "MIC" not in m                                # M1 carries no MIC


def test_m1_pmkid_tick_present_absent_or_omitted():
    # ✓ when the handshake carries a PMKID — pre-explains the "PMKID captured"
    # banner that follows. ✗ when it doesn't. Omitted when unknown (not parsed).
    yes = eapol_message_markup(_eapol(1, has_nonce=True, has_mic=False,
                                      eapol_complete=False, useful=True,
                                      has_pmkid=True))
    assert "PMKID[green]✓[/green]" in yes
    no = eapol_message_markup(_eapol(1, has_nonce=True, has_mic=False,
                                     eapol_complete=False, useful=True,
                                     has_pmkid=False))
    assert "PMKID[red]✗[/red]" in no
    unknown = eapol_message_markup(_eapol(1, has_nonce=True, has_mic=False,
                                          eapol_complete=False, useful=True))
    assert "PMKID" not in unknown


def test_m2_complete_keystone_all_green_bold():
    m = eapol_message_markup(_eapol(2, has_nonce=True, has_mic=True,
                                    eapol_complete=True, useful=True))
    assert "[bold cyan]M2[/bold cyan]" in m
    assert "SNonce[green]✓[/green]" in m
    assert "MIC[green]✓[/green]" in m
    assert "EAPOL[green]✓[/green]" in m


def test_m2_clipped_is_dim_with_red_eapol():
    # The exact failure that lost a capture: a complete SNonce + MIC but a
    # clipped 802.1X payload → not a usable keystone → dim label, red EAPOL tick.
    m = eapol_message_markup(_eapol(2, has_nonce=True, has_mic=True,
                                    eapol_complete=False, useful=False))
    assert "[dim cyan]M2[/dim cyan]" in m                # degraded → dim
    assert "EAPOL[red]✗[/red]" in m
    assert "[black bold on" not in m                     # never a win chip


def test_m3_shows_anonce_and_mic_no_eapol():
    m = eapol_message_markup(_eapol(3, has_nonce=True, has_mic=True,
                                    eapol_complete=True, useful=True))
    assert "ANonce[green]✓[/green]" in m
    assert "MIC[green]✓[/green]" in m
    assert "EAPOL" not in m                              # M3's own EAPOL is unused


def test_m4_zeroed_nonce_is_dim_with_red_snonce():
    m = eapol_message_markup(_eapol(4, has_nonce=False, has_mic=True,
                                    eapol_complete=True, useful=False))
    assert "[dim cyan]M4[/dim cyan]" in m
    assert "SNonce[red]✗[/red]" in m                     # no echoed SNonce


def test_unclassified_frame_label_only():
    m = eapol_message_markup(_eapol(0, has_nonce=False, has_mic=False,
                                    eapol_complete=False, useful=False))
    # No fields, no direction arrow — just the prefix + bare label.
    assert m == "[dim]4-Way Handshake (…44:55:66):[/dim] [dim cyan]EAPOL-?[/dim cyan]"


def _uncrackable(label):
    return CaptureEvent(
        kind=CaptureKind.EAPOL, bssid="aa:bb:cc:dd:ee:ff",
        client_mac="11:22:33:44:55:66", ssid="Net", msg_num=2,
        has_nonce=True, has_mic=True, eapol_complete=True, useful=True,
        crackable=False, akm_label=label,
    )


def test_uncrackable_frame_badges_akm_reason():
    """An uncrackable EAPOL frame goes red with an orange reason chip (SAE / FT)."""
    sae = eapol_message_markup(_uncrackable("SAE"))
    assert "[bold black on orange1] SAE [/bold black on orange1]" in sae
    assert "[red]M2[/red]" in sae
    ft = eapol_message_markup(_uncrackable("FT"))
    assert "[bold black on orange1] FT [/bold black on orange1]" in ft


def test_crackable_frame_has_no_badge():
    m = eapol_message_markup(_eapol(2, has_nonce=True, has_mic=True,
                                    eapol_complete=True, useful=True))
    assert "orange1" not in m
