import sys

def parse_int(s):
    if not s:
        return 0
    if s.startswith('0x'):
        return int(s, 16)
    return int(s, 10)

def parse_tshark_line(line):
    parts = line.strip().split('\t')
    if len(parts) < 6:
        return None
    
    frame_num = parts[0]
    bmReqType = parse_int(parts[1])
    bReq = parse_int(parts[2])
    wValue = parse_int(parts[3])
    wIndex = parse_int(parts[4])
    wLength = parse_int(parts[5])
    
    data = ""
    if len(parts) > 6:
        data = parts[6]
        
    return {
        "frame": frame_num,
        "type": hex(bmReqType),
        "req": bReq,
        "val": hex(wValue),
        "idx": hex(wIndex),
        "len": wLength,
        "data": data
    }

def main():
    for i, line in enumerate(sys.stdin):
        try:
            parsed = parse_tshark_line(line)
            if not parsed:
                continue
                
            direction = "WRITE" if parsed["type"] == "0x40" else "READ"
            print(f"{parsed['frame']:>5} {direction} Req:{parsed['req']} Val:{parsed['val']} Idx:{parsed['idx']} Len:{parsed['len']} Data:{parsed['data']}")
        except Exception as e:
            sys.stderr.write(f"Error on line {i+1}: {e}\nRaw line: {line}\n")
            continue

if __name__ == "__main__":
    main()
