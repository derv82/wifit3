import threading
import queue
import time
import usb.core
from wmi_state import WMIState
from models import AR9271Descriptors

class USBManager:
    """
    Handles all raw USB reading and writing. 
    Implements a single-consumer thread for the Bulk IN endpoint to prevent race conditions.
    """
    def __init__(self, dev, ep_out, ep_in):
        self.dev = dev
        self.ep_out = ep_out
        self.ep_in = ep_in
        
        self.rx_queue = queue.Queue(maxsize=1000)
        self.event_queue = queue.Queue(maxsize=100)
        
        self.running = False
        self.reader_thread = None
        self.wmi_state = WMIState()
        
        # Start with a safe assumption for credits
        self.credits = 10 
        self.credit_lock = threading.Lock()

    def start(self):
        if self.running:
            return
        
        # Try to clear halts
        try:
            self.dev.clear_halt(self.ep_in.bEndpointAddress)
            self.dev.clear_halt(self.ep_out.bEndpointAddress)
        except Exception:
            pass

        self.running = True
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def stop(self):
        self.running = False
        if self.reader_thread:
            self.reader_thread.join(timeout=1.0)

    def _reader_loop(self):
        """Single consumer for the Bulk IN endpoint."""
        buffer_size = 4096
        
        while self.running:
            try:
                # Keep timeout short to allow checking self.running
                raw_array = self.dev.read(self.ep_in.bEndpointAddress, buffer_size, timeout=100)
                data = bytes(raw_array)
                
                if not data:
                    continue
                    
                print(f"DEBUG IN: {data[:16].hex(' ')}")

                # 1. Is it a WMI Control Event / Credit Report?
                # WMI events usually start with specific HTC routing bytes (e.g., 0x01)
                event_info = self.wmi_state.parse_wmi_event(data)
                if event_info:
                    if event_info['type'] == 'credit':
                        with self.credit_lock:
                            # ath9k_htc often sends absolute available credits
                            self.credits = event_info['count'] 
                    else:
                        try:
                            self.event_queue.put_nowait(event_info)
                        except queue.Full:
                            pass # Drop old events if queue is blocked
                    continue

                # 2. Is it Wireless RX Data?
                # Usually routed differently in HTC header, but we rely on the parser to validate
                frame, rssi, length = AR9271Descriptors.parse_rx(data)
                if frame:
                    try:
                        self.rx_queue.put_nowait((frame, rssi, time.time()))
                    except queue.Full:
                        pass # Drop frames if processing is too slow

            except usb.core.USBError as e:
                # Ignore timeouts
                if e.errno not in (110, 10060):
                    print(f"    [!] Reader Thread USB Error: {e}")
                    # If it's a pipe error, try to clear halt
                    if e.errno == 32 or 'pipe' in str(e).lower():
                        try:
                            self.dev.clear_halt(self.ep_in.bEndpointAddress)
                        except Exception:
                            pass
                    time.sleep(0.1) # Prevent tight error loop
            except Exception:
                pass

    def wait_for_credits(self, min_credits=1, timeout=1.0):
        start = time.time()
        while time.time() - start < timeout:
            with self.credit_lock:
                if self.credits >= min_credits:
                    return True
            time.sleep(0.001) # Yield
        return False

    def send_wmi_command(self, raw_payload, wait_for_ack=True):
        """
        Injects a sequence ID, waits for HTC credits, sends the packet, 
        and optionally waits for the matching ACK from the event_queue.
        """
        # Inject the sequence ID
        seq = self.wmi_state.next_seq()
        cmd_data = bytearray(raw_payload)
        import struct
        struct.pack_into(">H", cmd_data, 10, seq)

        # Wait for credit
        if not self.wait_for_credits():
            print(f"[-] TX Buffer Full. Dropping sequence {seq}")
            return False

        try:
            self.dev.write(self.ep_out.bEndpointAddress, cmd_data, timeout=1000)
            with self.credit_lock:
                self.credits -= 1 # Consume a credit
        except usb.core.USBError as e:
            print(f"[-] USB Write Error (Seq {seq}): {e}")
            return False

        if not wait_for_ack:
            return True

        # Wait for ACK from the single-consumer queue
        start = time.time()
        while time.time() - start < 1.0: # 1 sec max wait
            try:
                # Check for 10ms
                event = self.event_queue.get(timeout=0.01) 
                if event['seq_id'] == seq:
                    return True
            except queue.Empty:
                continue

        # print(f"    [!] Timeout waiting for ACK on Seq {seq}")
        return False
