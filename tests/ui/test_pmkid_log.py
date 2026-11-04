"""Unit tests for ui/pmkid_log.py: the active PMKID harvest tree markup."""
from wifit3.campaigns.pmkid import PmkidFail
from wifit3.ui.pmkid_log import render_failure, render_success

_ESSID = "TESTNET"
_HEADER = "[bold]Zbieranie PMKID[/bold] z [bold cyan]TESTNET[/bold cyan]"


def test_success_tree_full_action_trace():
    lines = render_success(_ESSID, "[dim]saved: captures/TESTNET_…_pmkid.hc22000[/dim]")
    assert lines[0] == _HEADER
    assert "Żądanie auth (klient→AP)" in lines[1]
    assert "Żądanie asocjacji (klient→AP)" in lines[2]
    assert "Komunikat M1 (AP→klient) PMKID[green]✓[/green]" in lines[3]
    assert "├─►" in lines[4] and "✓ PMKID wyodrębniony" in lines[4]
    assert "└─►" in lines[5] and "saved: captures/" in lines[5]


def test_success_without_save_hint_closes_on_the_chip():
    lines = render_success(_ESSID, None)
    assert len(lines) == 5                                      # no save leaf
    assert "└─►" in lines[4] and "PMKID wyodrębniony" in lines[4]


def test_no_kde_echoes_tx_then_red_m1_and_orange_chip_leaf():
    lines = render_failure(_ESSID, PmkidFail.NO_KDE)
    assert lines[0] == _HEADER
    assert "Żądanie auth" in lines[1] and "Żądanie asocjacji" in lines[2]
    assert "Komunikat M1 (AP→klient) PMKID[red]✗[/red]" in lines[3]
    assert "└─►" in lines[4]                                    # neutral leaf, not └─╳
    assert "[black bold on orange1] Brak PMKID w M1 [/black bold on orange1]" in lines[4]
    assert "AP nie wysyła PMKID w M1" in lines[4]


def test_no_response_echoes_tx_but_has_no_m1():
    lines = render_failure(_ESSID, PmkidFail.NO_RESPONSE)
    assert "Żądanie auth" in lines[1] and "Żądanie asocjacji" in lines[2]
    assert not any("Komunikat M1" in ln for ln in lines)         # no M1 ever arrived
    assert "└─►" in lines[-1]                                   # neutral leaf, not └─╳
    assert "[bold bright_red]Nie otrzymano M1[/bold bright_red]" in lines[-1]
    assert "Brak odpowiedzi AP" in lines[-1]


def test_pmf_and_no_psk_are_header_plus_leaf_only():
    pmf = render_failure(_ESSID, PmkidFail.PMF_REQUIRED)
    assert len(pmf) == 2                                        # bailed before any TX
    assert "[bold orange1]Wymagany PMF[/bold orange1]" in pmf[1]
    invalid = render_failure(_ESSID, PmkidFail.PMF_INVALID)
    assert len(invalid) == 2
    assert "[bold orange1]Błędny PMF[/bold orange1]" in invalid[1]
    no_psk = render_failure(_ESSID, PmkidFail.NO_PSK_AKM)
    assert len(no_psk) == 2
    assert "Brak PSK AKM" in no_psk[1]


def test_unknown_reason_falls_back():
    lines = render_failure(_ESSID, None)
    assert "Nie udało się" in lines[-1]
