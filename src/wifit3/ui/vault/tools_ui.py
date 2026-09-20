from typing import Dict, Type
from textual.screen import ModalScreen

from wifit3.ui.vault.modals.hashcat import HashcatConfigModal

UI_TOOLS: Dict[str, Type[ModalScreen]] = {
    "hashcat": HashcatConfigModal
}
