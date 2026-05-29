import logging
import usb.core

logger = logging.getLogger(__name__)

class FirmwareLoader:
    """
    Handles uploading the ath9k_htc firmware to a "cold" AR9271 device.
    Transitions the device from BootROM to active firmware execution.
    """

    LOAD_ADDRESS = 0x501000
    CHUNK_SIZE = 512  # Manually chunked for Windows/WinUSB stability

    # Standard Atheros firmware upload request parameters
    BM_REQ_VENDOR_OUT = 0x40
    FIRMWARE_DOWNLOAD = 0x30
    FIRMWARE_DOWNLOAD_COMP = 0x31

    @staticmethod
    def load(dev: usb.core.Device, firmware_data: bytes) -> bool:
        """
        Uploads firmware in chunks and triggers execution.
        [Driver Source Reference](/data_dumps/ath9k-source-v6.8/hif_usb.c#1070).
        """
        logger.info(f"Starting firmware upload ({len(firmware_data)} bytes)...")
        
        offset = 0
        total_size = len(firmware_data)
        
        while offset < total_size:
            chunk = firmware_data[offset : offset + FirmwareLoader.CHUNK_SIZE]
            
            # Address calculation for wValue/wIndex
            current_addr = FirmwareLoader.LOAD_ADDRESS + offset
            wValue = (current_addr >> 8) & 0xFFFF
            wIndex = (current_addr >> 24) & 0xFF
            
            try:
                dev.ctrl_transfer(
                    FirmwareLoader.BM_REQ_VENDOR_OUT,
                    FirmwareLoader.FIRMWARE_DOWNLOAD,
                    wValue,
                    wIndex,
                    chunk,
                    timeout=2000
                )
            except usb.core.USBError as e:
                logger.error(f"USBError during firmware upload at offset {offset}: {e}")
                return False

            offset += len(chunk)

        logger.info("Firmware upload complete. Triggering boot...")
        return FirmwareLoader.trigger_boot(dev)

    @staticmethod
    def trigger_boot(dev: usb.core.Device) -> bool:
        """
        Sends the specific commands to jump to the firmware entry point.
        [Driver Source Reference](/data_dumps/ath9k-source-v6.8/hif_usb.c#1102).
        """
        import usb.util
        try:
            # 1. Firmware Download Complete / Boot Trigger
            # Found in PCAP: bRequest=0x31, wValue=0x9030 (Execution address)
            dev.ctrl_transfer(
                FirmwareLoader.BM_REQ_VENDOR_OUT,
                FirmwareLoader.FIRMWARE_DOWNLOAD_COMP,
                0x9030, # FIRMWARE_TEXT >> 8
                0x0000,
                b'',
                timeout=1000
            )
            logger.info("Boot command sent (FIRMWARE_DOWNLOAD_COMP).")
            
        except usb.core.USBError as e:
            # Note: On Windows, the boot command often triggers a device reset,
            # which might manifest as a USBError (Pipe error or Timeout).
            # This is usually a sign of success.
            logger.warning(f"Device reset triggered during boot (Expected USBError): {e}")
            
        finally:
            # The device must be released so Windows can process the soft-reset
            # and transition the endpoints from Interrupt OUT to Bulk OUT
            usb.util.dispose_resources(dev)
            
        return True
