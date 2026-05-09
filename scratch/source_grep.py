import argparse
import re
from pathlib import Path
import ast

def find_hex(file_path):
    try:
        text = file_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.search(r'\b0x[0-9a-fA-F]+\b', line):
                print(f"{file_path}:{i+1}: {line.strip()}")
    except Exception:
        pass

def find_token(file_path, token):
    try:
        text = file_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if token in line:
                print(f"{file_path}:{i+1}: {line.strip()}")
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser(description="Deterministic source code grep for subagents")
    parser.add_argument("path", help="Directory or file to search")
    parser.add_argument("--token", help="Exact or partial token to find")
    parser.add_argument("--hex", action="store_true", help="Find all hex constants")
    
    args = parser.parse_args()
    
    target_path = Path(args.path)
    if not target_path.exists():
        print(f"Path not found: {args.path}")
        return

    files = []
    if target_path.is_dir():
        files.extend(target_path.rglob("*.py"))
        files.extend(target_path.rglob("*.c"))
        files.extend(target_path.rglob("*.h"))
    else:
        files.append(target_path)
    
    for f in files:
        if "initvals" in f.name.lower() or "eeprom" in f.name.lower():
            continue
        if args.hex:
            find_hex(f)
        if args.token:
            find_token(f, args.token)

if __name__ == '__main__':
    main()
