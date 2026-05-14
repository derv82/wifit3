import re
import sys
from pathlib import Path

def read_file_safe(path):
    for enc in ['utf-8', 'utf-16', 'latin-1', 'cp1252']:
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return ""

def find_definition(root_path, search_term):
    root = Path(root_path)
    is_hex = search_term.startswith("0x")
    
    # If hex, we want to match the literal string "0x0400" or "0x400" 
    # but NOT "0x00000400" if the user didn't ask for it.
    if is_hex:
        # Strip 0x and leading zeros to get the "Core" value
        core_hex = search_term[2:].lstrip('0').lower()
        if not core_hex: core_hex = "0"
        # Match 0x followed by any number of zeros, then our core hex
        pattern_str = rf'#define\s+([A-Za-z0-9_]+)\b.*0x0*{core_hex}\b'
    else:
        pattern_str = rf'#define\s+({search_term})\b(.*)'

    pattern = re.compile(pattern_str, re.IGNORECASE)
    matches = []
    files = list(root.rglob("*.[ch]"))
    
    for f in files:
        content = read_file_safe(f)
        if not content: continue
            
        lines = content.splitlines()
        for i, line in enumerate(lines):
            m = pattern.search(line)
            if not m: continue
            
            token = m.group(1)
            # For hex search, we want to make sure the value part is actually JUST the hex 
            # or wrapped in a simple macro.
            val_part = line.split(token)[-1].strip()
            val_part = re.split(r'//|/\*', val_part)[0].strip()

            # Double-check the hex literal in the line matches the "Exactness"
            if is_hex:
                # Find all hex literals in the line
                found_hexes = re.findall(r'0x([0-9a-fA-F]+)\b', line)
                exact_match = False
                for fh in found_hexes:
                    # Literal check: The hex in the file must have the same "magnitude" 
                    # as the search term (e.g. 0x0400 doesn't match 0x00000400)
                    # We compare the length of the hex string (excluding leading zeros)
                    if fh.lstrip('0').lower() == core_hex:
                        # Optional: If you want to be even stricter, compare lengths
                        # but for now, this avoids bitmask collisions.
                        if len(fh) <= 4 or len(search_term[2:]) > 4:
                            exact_match = True
                            break
                if not exact_match: continue

            line_no = i + 1
            parent_reg = None
            context_start = max(0, i - 2)
            
            # Context walk-back
            is_likely_bitfield = any(kw in val_part for kw in ["FIELD32", "BIT", "<<"])
            if is_likely_bitfield:
                for j in range(i - 1, max(0, i - 30), -1):
                    prev_line = lines[j].strip()
                    if "#define" in prev_line:
                        p_match = re.search(r'#define\s+(\w+)\s+(0x[0-9a-fA-F]{3,})\b', prev_line)
                        if p_match:
                            parent_reg = p_match.group(0)
                            context_start = j
                            break
            
            block_lines = []
            context_end = min(len(lines), i + 15)
            for k in range(context_start, context_end):
                l = lines[k].strip()
                if "#define" in l:
                    block_lines.append(f"{k+1:4}: {l}")
                elif l == "" and k > i: break
                elif k > i and not (l.startswith("//") or l.startswith("/*")): break

            matches.append({
                "file": f.relative_to(root),
                "line": line_no,
                "token": token,
                "value": val_part,
                "context": block_lines,
                "parent_hint": parent_reg
            })
            
    return matches

def main():
    if len(sys.argv) < 3:
        print("Usage: python source_intel.py <source_dir> <hex_or_token>")
        return

    src_dir = sys.argv[1]
    search_term = sys.argv[2]
    results = find_definition(src_dir, search_term)
    
    if not results:
        print(f"No definition found for '{search_term}'")
        return

    if len(results) > 10:
        print(f"Found {len(results)} matches for '{search_term}'. Showing summary:\n")
        print(f"{'FILE':<30} | {'LINE':<5} | {'TOKEN':<30} | {'VALUE'}")
        print("-" * 85)
        for res in results:
            val_trunc = (res['value'][:40] + '..') if len(res['value']) > 40 else res['value']
            print(f"{str(res['file']):<30} | {res['line']:<5} | {res['token']:<30} | {val_trunc}")
    else:
        for res in results:
            print(f"\n--- Found in {res['file']}:{res['line']} ---")
            if res['parent_hint']:
                print(f"Parent Register? {res['parent_hint']}")
            print(f"Definition: {res['token']} = {res['value']}")
            print("Block Context:")
            for line in res['context']:
                marker = ">>" if f"{res['line']}:" in line else "  "
                print(f"{marker} {line}")

if __name__ == "__main__":
    main()
