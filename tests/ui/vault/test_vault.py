"""Vault drawer and item pane text: the delete prompt and the uncracked-hashline rows."""
from wifit3.crack.hc22000_format import hashline_id
from wifit3.persist.exhaustion import record_exhausted
from wifit3.persist.vault import Vault
from wifit3.ui.screens.vault.drawer import _ap_contents_text
from wifit3.ui.screens.vault.hashlines import (exhausted_count_text,
                                               hashline_groups, hashline_prompt, _exhausted_names)

_EAPOL_LINE = ("WPA*02*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431*" + "a" * 64 + "*"
               + "00" * 99 + "*02\n")
_PMKID_LINE = "WPA*01*" + "0" * 32 + "*aabbccddeeff*112233445566*5465737431***\n"
_OTHER_CLIENT_LINE = _EAPOL_LINE.replace("112233445566", "112233445577")
_SAME_CLIENT_LINE = _EAPOL_LINE.replace("0" * 32, "1" * 32, 1)     # another MIC, grouped with _EAPOL_LINE
_WEP_TXT = "SSID: HomeNet\nBSSID: aa:bb:cc:dd:ee:ff\nWEP key (hex):   6162636465\n"


def _write(d, name, content):
    (d / name).write_text(content, encoding="utf-8")


def test_the_delete_prompt_counts_an_aps_captures_and_keys(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff_1000_wep_key.txt", _WEP_TXT)
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", _PMKID_LINE)
    vault = Vault()

    assert _ap_contents_text(vault.persisted("aa:bb:cc:dd:ee:ff"), vault.ap_state("aa:bb:cc:dd:ee:ff")) == \
        "1 capture, 1 key"


def test_each_uncracked_row_reads_its_pair_client_verdict_and_ssid(tmp_path):
    _write(tmp_path, "HomeNet_aa-bb-cc-dd-ee-ff.hc22000", _EAPOL_LINE + _OTHER_CLIENT_LINE + _PMKID_LINE)
    groups = hashline_groups(Vault().ap_state("aa:bb:cc:dd:ee:ff").uncracked)
    prompts = [" ".join(str(hashline_prompt(group, True, True, "", 0)).split()) for group in groups]

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

    assert [exhausted_count_text(g, state.exhausted_wordlists) for g in groups] == [
        "2 wordlists exhausted (1 on some lines)", "", "1 wordlist exhausted"]
    assert [_exhausted_names(g, state.exhausted_wordlists) for g in groups] == [
        ["default.txt", "rockyou.txt"], [], ["default.txt"]]


