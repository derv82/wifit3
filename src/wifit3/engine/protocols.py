from typing import Protocol, Callable, Optional, List, Any
import asyncio

class ProgressCallback(Protocol):
    def __call__(self, percentage: float, message: str) -> None: ...

class WlanDriver(Protocol):
    """
    Protocol definition for wifit3 hardware drivers.
    Ensures consistent interaction across different chipsets.
    """
    mac_address: Optional[str]
    is_warm: bool
    
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None: ...
    
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """
        Initializes the hardware.
        Should call progress_cb(0.0 to 1.0, "Message") to provide UI feedback.
        """
        ...
        
    async def set_channel(self, channel: int) -> bool: ...
    
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool: ...
    
    async def close(self) -> None: ...
