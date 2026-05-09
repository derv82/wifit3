import re
import pprint

def extract_enum(filepath, enum_name):
    with open(filepath, 'r') as f:
        content = f.read()

    # Find the enum block
    pattern = r"enum\s+" + enum_name + r"\s*\{([^}]+)\}"
    match = re.search(pattern, content)
    if not match:
        return {}

    enum_body = match.group(1)
    
    constants = {}
    current_val = 0
    
    for line in enum_body.splitlines():
        line = line.strip()
        # Remove inline comments
        line = re.sub(r'//.*', '', line)
        line = re.sub(r'/\*.*?\*/', '', line).strip()
        
        if not line or line.startswith('#'):
            continue
            
        parts = line.split(',')
        for part in parts:
            part = part.strip()
            if not part:
                continue
                
            if '=' in part:
                name, val_str = [p.strip() for p in part.split('=')]
                if val_str.startswith('0x'):
                    current_val = int(val_str, 16)
                else:
                    try:
                        current_val = int(val_str)
                    except ValueError:
                        pass # Handle defines if needed, but usually simple ints
                constants[name] = current_val
                current_val += 1
            else:
                name = part
                constants[name] = current_val
                current_val += 1
                
    return constants

def main():
    wmi_cmds = extract_enum('data_dumps/ath9k-source-v6.8/wmi.h', 'wmi_cmd_id')
    wmi_evts = extract_enum('data_dumps/ath9k-source-v6.8/wmi.h', 'wmi_event_id')
    htc_eps = extract_enum('data_dumps/ath9k-source-v6.8/htc_hst.h', 'htc_endpoint_id')
    htc_msgs = extract_enum('data_dumps/ath9k-source-v6.8/htc_hst.h', 'htc_msg_id')
    
    print("# Auto-extracted from ath9k_htc C source code")
    print("\nWMI_COMMANDS = {")
    for k, v in wmi_cmds.items():
        print(f"    0x{v:04X}: '{k}',")
    print("}")

    print("\nWMI_EVENTS = {")
    for k, v in wmi_evts.items():
        print(f"    0x{v:04X}: '{k}',")
    print("}")
    
    print("\nHTC_ENDPOINTS = {")
    for k, v in htc_eps.items():
        if v >= 0:
            print(f"    {v}: '{k}',")
    print("}")

    print("\nHTC_MESSAGES = {")
    for k, v in htc_msgs.items():
        print(f"    0x{v:04X}: '{k}',")
    print("}")

if __name__ == '__main__':
    main()
