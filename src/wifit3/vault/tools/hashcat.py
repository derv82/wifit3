from typing import Dict, Any

from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus, CaptureType
from wifit3.vault.tools.base import VaultTool


class HashcatTool(VaultTool):
    name = "hashcat"
    capabilities = ToolCapability.KILLABLE | ToolCapability.PAUSABLE | ToolCapability.RESUMABLE

    def can_crack(self, capture: PersistedCapture) -> bool:
        return capture.type in (CaptureType.HS, CaptureType.PMKID)

    def launch(self, capture: PersistedCapture, config: Dict[str, Any]) -> Dict[str, Any]:
        return {"pid": 9999, "log_path": "dummy.log"}

    def poll_status(self, tracking_data: Dict[str, Any]) -> ToolResult:
        return ToolResult(status=ToolStatus.RUNNING, value="Placeholder Hashcat Tool Running...")
