import sys
import subprocess
import threading
import time
from typing import List, Optional
from scapy.all import conf
from loguru import logger

# Import platform-specific discovery
if sys.platform == "win32":
    from .manager_win import get_windows_interfaces
else:
    def get_windows_interfaces(): return []

class Interface:
    def __init__(self, name: str, description: str, guid: Optional[str] = None):
        self.name = name
        self.description = description
        self.guid = guid
        self.is_monitor = False
        self.scapy_iface = None
        
        # Match with Scapy's interface objects
        for iface in conf.ifaces.values():
            if guid and hasattr(iface, 'guid') and guid.lower() in iface.guid.lower():
                self.scapy_iface = iface
                break
            if name == iface.name or description == iface.description:
                self.scapy_iface = iface
                break

    def can_monitor(self) -> bool:
        if sys.platform != "win32" or not self.guid:
            return False 
        
        # WlanHelper wants the raw GUID string (no braces)
        raw_guid = self.guid.strip("{}")
        try:
            result = subprocess.run(
                ["C:\\Windows\\System32\\Npcap\\WlanHelper.exe", raw_guid, "modes"],
                capture_output=True, text=True, check=False
            )
            return "monitor" in result.stdout.lower()
        except Exception:
            return False

    def set_monitor(self, enable: bool) -> bool:
        if sys.platform != "win32" or not self.guid:
            return False
            
        raw_guid = self.guid.strip("{}")
        mode = "monitor" if enable else "managed"
        modes_to_try = [mode, "1" if enable else "0"]
        
        logger.debug(f"Setting Monitor Mode ({enable}) for {self.description}")
        for m in modes_to_try:
            try:
                logger.debug(f"Attempting WlanHelper.exe {raw_guid} mode {m} (shell=True)")
                result = subprocess.run(
                    ["C:\\Windows\\System32\\Npcap\\WlanHelper.exe", raw_guid, "mode", m],
                    check=True, capture_output=True, text=True, shell=True
                )
                logger.info(f"SUCCESS: {self.description} -> {m}")
                self.is_monitor = enable
                return True
            except subprocess.CalledProcessError as e:
                logger.warning(f"FAILED mode {m}: Out='{e.stdout.strip()}' Err='{e.stderr.strip()}'")
            except Exception as e:
                logger.error(f"Unexpected error: {str(e)}")
        
        return False

class InterfaceManager:
    def __init__(self):
        self.interfaces: List[Interface] = []
        self._hopper_thread: Optional[threading.Thread] = None
        self._stop_hopping = threading.Event()
        self.current_channel = 1

    def refresh(self):
        self.interfaces = []
        if sys.platform == "win32":
            win_ifaces = get_windows_interfaces()
            for w in win_ifaces:
                scapy_name = None
                for s_name, s_iface in conf.ifaces.items():
                    if hasattr(s_iface, 'guid') and w['guid'].lower() in s_iface.guid.lower():
                        scapy_name = s_name
                        break
                
                if scapy_name:
                    self.interfaces.append(Interface(
                        name=scapy_name,
                        description=w['description'],
                        guid=w['guid']
                    ))
        logger.info(f"Discovered {len(self.interfaces)} wireless interfaces.")

    def start_hopping(self, interface: Interface, channels: List[int] = None, interval: float = 0.5):
        if not channels:
            channels = [1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 5, 10]
            
        self._stop_hopping.clear()
        self._hopper_thread = threading.Thread(
            target=self._hop_loop, 
            args=(interface, channels, interval),
            daemon=True
        )
        self._hopper_thread.start()

    def _hop_loop(self, interface: Interface, channels: List[int], interval: float):
        import itertools
        channel_cycle = itertools.cycle(channels)
        
        raw_guid = interface.guid.strip("{}") if interface.guid else interface.name
        logger.debug(f"Starting hopper loop on {interface.description} (GUID: {raw_guid})")
        
        while not self._stop_hopping.is_set():
            channel = next(channel_cycle)
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["C:\\Windows\\System32\\Npcap\\WlanHelper.exe", raw_guid, "channel", str(channel)],
                        check=True, capture_output=True, shell=True
                    )
                self.current_channel = channel
                logger.debug(f"Hopped to channel {channel}")
            except Exception as e:
                logger.warning(f"Failed to hop to channel {channel}: {e}")
            
            time.sleep(interval)

    def stop_hopping(self):
        self._stop_hopping.set()
        if self._hopper_thread:
            self._hopper_thread.join(timeout=1.0)
