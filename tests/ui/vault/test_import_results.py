from wifit3.persist.capture_import import CaptureImport
from wifit3.ui.screens.vault.modals.import_results import import_summary


def test_the_summary_right_aligns_the_non_zero_counts_and_colours_them():
    result = CaptureImport(handshakes=16, pmkids=134, duplicates=80, files=3, bssids={"aa:bb:cc:dd:ee:ff"},
                           problems=[("a.meta", "unreadable .meta")])
    assert import_summary(result).split("\n") == [
        "[bold]Imported 3 files:[/]",
        "  [$text-success]  1 Access Point[/]",
        "  [$text-success] 16 Handshakes[/]",
        "  [$text-success]134 PMKIDs[/]",
        "   80 Already in Vault",
        "  [$text-error]  1 Error[/]",
    ]
