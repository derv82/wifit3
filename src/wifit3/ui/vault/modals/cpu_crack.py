from textual.app import ComposeResult

from wifit3.persist.config import Config, CpuCrackSettings
from wifit3.ui.vault.modals.wordlists import WordlistConfigModal


class CpuCrackConfigModal(WordlistConfigModal):
    """Configuration modal for CPU Crack jobs."""

    dialog_title = "CPU Crack"

    def _settings(self) -> CpuCrackSettings:
        return Config.cpu_crack

    def _compose_fields(self) -> ComposeResult:
        yield from self._compose_wordlists()
        yield from self._compose_password_sources()
