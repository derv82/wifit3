import struct
from models import AR9271Descriptors

class WMIState:
    """
    Manages the Sequence IDs and formatting for ath9k_htc WMI commands.
    """
    def __init__(self):
        self.seq_id = 1
        
    def next_seq(self):
        current = self.seq_id
        # Safe sequence wrap around
        self.seq_id += 1
        if self.seq_id > 254: 
            self.seq_id = 1
        return current

    def create_reg_write(self, address, value):
        """
        Constructs a WMI_REG_WRITE (0x15) command.
        """
        seq = self.next_seq()
        # The correct HTC header puts length at offset 2, not 6!
        # Endpoint 0x01 (WMI Control), Flags 0x00
        cmd_id = 0x0015
        payload_len = 8 # Addr (4) + Val (4)
        
        # >BBH4sHH: EP(1), Flags(1), Len(2), Pad(4), Cmd(2), Seq(2)
        header = struct.pack(">BBH4sHH", 0x01, 0x00, payload_len + 4, b'\x00\x00\x00\x00', cmd_id, seq)
        payload = struct.pack(">II", address, value)
        
        return header + payload, seq

    def parse_wmi_event(self, usb_data):
        """
        Parses an incoming Bulk IN packet to see if it's a WMI Event or Credit Report.
        Returns:
            dict: {'type': 'credit', 'count': N}
            dict: {'type': 'event', 'event_id': ID, 'seq_id': Seq}
            None: If not a recognized control packet.
        """
        if len(usb_data) < 12:
            return None

        # Check for Credit Report (Often Endpt 0x01, though depends on firmware version)
        # We will use the structural signature you provided: 0x01 0x00 at offset 0
        if usb_data[0:2] == b'\x01\x00':
            if len(usb_data) > 16:
                credits = usb_data[16]
                return {'type': 'credit', 'count': credits}
        
        # Check for standard WMI Event
        # Typical header size + 2 bytes for WMI length, Event ID starts at 8
        try:
            event_id, seq_id = struct.unpack_from(">HH", usb_data, 8)
            return {'type': 'event', 'event_id': event_id, 'seq_id': seq_id}
        except struct.error:
            return None

