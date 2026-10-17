from textual.app import ComposeResult
from textual.widgets import Label

from wifit3.ui.vault.modals.wordlists import WordlistConfigModal

_ABOUT = ("[b]Note:[/b] CPU Crack is wifit3's built-in WPA cracker.\n"
          "      May cause battery drain. Use at your own risk.")


class CpuCrackConfigModal(WordlistConfigModal):
    """Configuration modal for CPU Crack jobs."""

    dialog_title = "Launch CPU Crack"

    DEFAULT_CSS = """
    CpuCrackConfigModal #cpu-crack-about { margin: 1 0; }
    """

    def _compose_fields(self) -> ComposeResult:
        yield Label(_ABOUT, id="cpu-crack-about")
        yield from self._compose_wordlists()
