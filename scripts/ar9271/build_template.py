import struct

def parse_and_generate_template(pcap_path, start_time, end_time, output_name):
    print(f"[*] Extracting WMI sequence for '{output_name}' ({start_time}s - {end_time}s)")
    
    sequence_blocks = []
    found_synth_write = False
    
    with open(pcap_path, 'rb') as f:
        f.read(24) # PCAP Header
        first_ts = None
        
        while True:
            hdr = f.read(16)
            if not hdr or len(hdr) < 16: break
            ts_sec, ts_usec, incl_len, _ = struct.unpack('<IIII', hdr)
            pkt_data = f.read(incl_len)
            
            if first_ts is None: first_ts = ts_sec
            rel_ts = (ts_sec - first_ts) + (ts_usec / 1000000.0)
            
            if start_time <= rel_ts <= end_time:
                # Filter for EP4 OUT Submit
                if len(pkt_data) > 64 and pkt_data[8] == 0x53 and pkt_data[10] == 0x04:
                    payload = pkt_data[64:]
                    if len(payload) >= 12:
                        wmi_cmd = struct.unpack_from('>H', payload, 8)[0]
                        
                        # We only care about WMI_REG_WRITE (0x15) and WMI_REG_RMW (0x20)
                        # but we need to keep all of them in order for the template.
                        
                        is_synth_packet = False
                        
                        # Scan for 0x9874 (Synthesizer) in this packet
                        if wmi_cmd == 0x0015:
                            # It's a Reg Write. Check the addresses (start at offset 12, stride 8)
                            for offset in range(12, len(payload), 8):
                                if offset + 4 <= len(payload):
                                    addr = struct.unpack_from('>I', payload, offset)[0]
                                    if addr == 0x9874:
                                        is_synth_packet = True
                                        found_synth_write = True
                                        break
                                        
                        # Strip the Sequence ID (Offset 10) by replacing it with 0x0000
                        # We use bytearray so we can mutate it
                        clean_payload = bytearray(payload)
                        struct.pack_into(">H", clean_payload, 10, 0) 
                        
                        sequence_blocks.append({
                            'cmd_id': wmi_cmd,
                            'is_synth': is_synth_packet,
                            'raw_bytes': bytes(clean_payload)
                        })

    print(f"[+] Extracted {len(sequence_blocks)} commands.")
    if found_synth_write:
        print(f"[+] Found Synthesizer Control Register (0x9874) write!")
    else:
        print(f"[-] WARNING: Did not find Synthesizer Control Register write.")

    # Write out the Python code
    out = f"def get_{output_name}(target_channel=6):\n"
    out += "    '''\n    Golden Sequence. Sequence IDs are 0x0000.\n"
    out += "    Must be dynamically updated before sending.\n    '''\n"
    out += "    seq = []\n"
    
    for i, block in enumerate(sequence_blocks):
        out += f"    # Command {i:03d} (0x{block['cmd_id']:04x})\n"
        if block['is_synth']:
            out += "    # --- SYNTHESIZER INJECTION --- \n"
            out += "    synth_packet = bytearray(b'" + "".join(f"\\x{b:02x}" for b in block['raw_bytes']) + "')\n"
            out += "    # Map channels to their Synthesizer Word (fractional-n math pre-calculated)\n"
            out += "    synth_words = { 1: 0x30a0cccc, 6: 0x30a27777 }\n"
            out += "    word = synth_words.get(target_channel, 0x30a27777)\n"
            
            # Find the offset of 0x9874 to overwrite the value
            # The structure is repeating [Addr(4)] [Val(4)] starting at offset 12
            raw = block['raw_bytes']
            for offset in range(12, len(raw), 8):
                 if struct.unpack_from('>I', raw, offset)[0] == 0x9874:
                     val_offset = offset + 4
                     out += f"    struct.pack_into('>I', synth_packet, {val_offset}, word)\n"
                     break
                     
            out += "    seq.append(bytes(synth_packet))\n"
        else:
             out += "    seq.append(b'" + "".join(f"\\x{b:02x}" for b in block['raw_bytes']) + "')\n"
             
    out += "    return seq\n\n"
    return out

if __name__ == "__main__":
    pcap = "usb_dumps/ar9271/awus036nha_1.pcap"
    
    code = "import struct\n\n"
    code += parse_and_generate_template(pcap, 16.31, 16.35, "channel_hop_template")
    
    with open("ar9271_golden_template.py", "w") as f:
        f.write(code)
    print("\n[+] Golden template generated to ar9271_golden_template.py")
