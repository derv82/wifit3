import struct
from typing import Tuple, Optional

class AthMetadataLayer:
    """
    Handles Layer 3: Atheros Hardware Descriptors.
    Parses metadata (RSSI, rate, length) from RX packets and 
    packs descriptors for TX injection.
    
    Note: Hardware descriptors use Little Endian (LE), 
    while WMI commands use Big Endian (BE).
    """

    # --- RX Descriptor (ath_rx_status) ---
    # Offset 0:  L (4) rs_datalen
    # Offset 4:  B (1) rssi
    # Offset 5:  B (1) rate
    # Offset 6:  B (1) more
    # Offset 7:  B (1) antenna
    # Offset 8:  I (4) tstamp
    # Offset 12: H (2) flags
    # Offset 14: H (2) phyerr
    RX_FMT = "<LBBBBIHH"
    RX_LEN = 16

    # --- TX Descriptor (ath_tx_status) ---
    # Offset 0:  L (4) ts_datalen
    # Offset 4:  B (1) ts_rate
    # Offset 5:  B (1) ts_flags
    # Offset 6:  B (1) ts_retry
    # Offset 7:  B (1) ts_antenna
    # Offset 8:  L (4) ts_tstamp
    # Offset 12: L (4) ts_reserved
    TX_FMT = "<LBBBBLL"
    TX_LEN = 16

    TX_FLAG_NO_ACK = 0x01
    TX_FLAG_RTS = 0x02
    TX_FLAG_HT = 0x04

    @staticmethod
    def parse_rx(data: bytes) -> Tuple[Optional[bytes], Optional[int], int]:
        """
        Parses an RX payload (excluding HTC header).
        Returns: (frame_data, rssi, rs_datalen)
        """
        if len(data) < AthMetadataLayer.RX_LEN:
            return None, None, 0

        desc_raw = data[:AthMetadataLayer.RX_LEN]
        try:
            rs_datalen, rssi, rate, more, ant, tstamp, flags, phyerr = struct.unpack(
                AthMetadataLayer.RX_FMT, desc_raw
            )
        except struct.error:
            return None, None, 0

        # Validate the length
        if rs_datalen == 0 or len(data) < AthMetadataLayer.RX_LEN + rs_datalen:
            return None, rssi, rs_datalen

        frame_data = data[AthMetadataLayer.RX_LEN : AthMetadataLayer.RX_LEN + rs_datalen]
        return frame_data, rssi, rs_datalen

    @staticmethod
    def pack_tx(frame_data: bytes, rate_idx: int = 0x0B, no_ack: bool = True) -> bytes:
        """
        Packs a TX descriptor for packet injection.
        """
        ts_flags = AthMetadataLayer.TX_FLAG_NO_ACK if no_ack else 0
        
        tx_desc = struct.pack(
            AthMetadataLayer.TX_FMT,
            len(frame_data), # ts_datalen
            rate_idx,         # ts_rate
            ts_flags,         # ts_flags
            0,                # ts_retry
            1,                # ts_antenna
            0,                # ts_tstamp
            0                 # ts_reserved
        )
        
        return tx_desc + frame_data
