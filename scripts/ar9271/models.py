import struct

class AR9271Descriptors:
    """
    Handles the parsing and packing of ath9k_htc hardware descriptors.
    Note: Hardware descriptors use Little Endian, while WMI commands use Big Endian.
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
    RX_FMT = "<LBBBBBIHH"
    RX_LEN = 16

    @staticmethod
    def parse_rx(usb_data, htc_header_len=6):
        """
        Parses the Bulk IN payload.
        Returns: (frame_data, rssi, datalen) or (None, None, None) if invalid.
        """
        if len(usb_data) < htc_header_len + AR9271Descriptors.RX_LEN:
            return None, None, None

        desc_raw = usb_data[htc_header_len : htc_header_len + AR9271Descriptors.RX_LEN]
        try:
            rs_datalen, rssi, rate, more, ant, tstamp, flags, phyerr = struct.unpack(AR9271Descriptors.RX_FMT, desc_raw)
        except struct.error:
            return None, None, None

        # Validate the length
        total_expected = htc_header_len + AR9271Descriptors.RX_LEN + rs_datalen
        if rs_datalen == 0 or len(usb_data) < total_expected:
            return None, None, None

        frame_data = usb_data[htc_header_len + AR9271Descriptors.RX_LEN : total_expected]
        return frame_data, rssi, rs_datalen

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
    def pack_tx(frame_bytes, rate_idx=0x0B, use_no_ack=True):
        """
        Creates an injection packet with HTC header and TX descriptor.
        Returns: bytes
        """
        # HTC Header: Endpoint 3 is typically TX Data
        htc_header = b'\x03\x00' + struct.pack(">H", len(frame_bytes) + AR9271Descriptors.TX_LEN) + b'\x00\x00'
        
        tx_flags = AR9271Descriptors.TX_FLAG_NO_ACK if use_no_ack else 0
        
        tx_desc = struct.pack(AR9271Descriptors.TX_FMT,
            len(frame_bytes), # ts_datalen
            rate_idx,         # ts_rate
            tx_flags,         # ts_flags
            0,                # ts_retry (0 for injection)
            1,                # ts_antenna
            0,                # ts_tstamp
            0                 # ts_reserved
        )
        
        return htc_header + tx_desc + frame_bytes
