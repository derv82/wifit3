"""Unit tests for ui/pmkid_log.py — the active PMKID harvest tree markup."""
from wifit3.campaigns.pmkid import PmkidFail
from wifit3.ui.pmkid_log import render_failure, render_success

_ESSID = "TESTNET"
_HEADER = ("[bold]PMKID[/bold] [dim]·[/dim] [bold]Harvesting[/bold] from "
           "[bold cyan]TESTNET[/bold cyan]")


def test_success_tree_full_action_trace():
    lines = render_success(_ESSID, "[dim]saved: captures/TESTNET_…_pmkid.hc22000[/dim]")
    assert lines[0] == _HEADER
    assert "Auth request (Client→AP)" in lines[1]
    assert "Assoc. request (Client→AP)" in lines[2]
    assert "M1 Message (AP→Client) PMKID[green]✓[/green]" in lines[3]
    assert "├─►" in lines[4] and "✓ PMKID Extracted" in lines[4]
    assert "└─►" in lines[5] and "saved: captures/" in lines[5]


def test_success_without_save_hint_closes_on_the_chip():
    lines = render_success(_ESSID, None)
    assert len(lines) == 5                                      # no save leaf
    assert "└─►" in lines[4] and "PMKID Extracted" in lines[4]


def test_no_kde_echoes_tx_then_red_m1_and_bold_orange_leaf():
    lines = render_failure(_ESSID, PmkidFail.NO_KDE)
    assert lines[0] == _HEADER
    assert "Auth request" in lines[1] and "Assoc. request" in lines[2]
    assert "M1 Message (AP→Client) PMKID[red]✗[/red]" in lines[3]
    assert "[red]╳[/red]" in lines[4]                           # leaf_fail terminal
    assert "[bold orange1]No PMKID in M1[/bold orange1]" in lines[4]
    assert "AP does not send PMKID in M1" in lines[4]


def test_no_response_echoes_tx_but_has_no_m1():
    lines = render_failure(_ESSID, PmkidFail.NO_RESPONSE)
    assert "Auth request" in lines[1] and "Assoc. request" in lines[2]
    assert not any("M1 Message" in ln for ln in lines)         # no M1 ever arrived
    assert "[bold orange1]No M1 received[/bold orange1]" in lines[-1]
    assert "AP did not respond" in lines[-1]


def test_pmf_and_no_psk_are_header_plus_leaf_only():
    pmf = render_failure(_ESSID, PmkidFail.PMF_REQUIRED)
    assert len(pmf) == 2                                        # bailed before any TX
    assert "[bold orange1]PMF Required[/bold orange1]" in pmf[1]
    no_psk = render_failure(_ESSID, PmkidFail.NO_PSK_AKM)
    assert len(no_psk) == 2
    assert "No PSK AKM" in no_psk[1]


def test_unknown_reason_falls_back():
    lines = render_failure(_ESSID, None)
    assert "Harvest failed" in lines[-1]
