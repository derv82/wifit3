from typing import Dict, Type
from textual.screen import ModalScreen

from wifit3.models import ToolName
from wifit3.ui.vault.modals.cpu_crack import CpuCrackConfigModal
from wifit3.ui.vault.modals.hashcat import HashcatConfigModal
from wifit3.ui.vault.modals.hashtopolis import HashtopolisConfigModal

UI_TOOLS: Dict[ToolName, Type[ModalScreen]] = {
    ToolName.HASHCAT: HashcatConfigModal,
    ToolName.CPU_CRACK: CpuCrackConfigModal,
    ToolName.HASHTOPOLIS: HashtopolisConfigModal,
}
