from collections.abc import Sequence
from typing import Dict, Optional, Protocol

from textual.screen import ModalScreen

from wifit3.models import PersistedCapture, ToolName
from wifit3.ui.vault.modals.cpu_crack import CpuCrackConfigModal
from wifit3.ui.vault.modals.hashcat import HashcatConfigModal
from wifit3.ui.vault.modals.hashtopolis import HashtopolisConfigModal


class LaunchModalConstructor(Protocol):
    """The constructor every Vault tool's launch modal shares."""

    def __call__(self, confirm_label: str = "Launch", capture: Optional[PersistedCapture] = None,
                 probed_ssids: Sequence[str] = ()) -> ModalScreen[dict]: ...


UI_TOOLS: Dict[ToolName, LaunchModalConstructor] = {
    ToolName.HASHCAT: HashcatConfigModal,
    ToolName.CPU_CRACK: CpuCrackConfigModal,
    ToolName.HASHTOPOLIS: HashtopolisConfigModal,
}
