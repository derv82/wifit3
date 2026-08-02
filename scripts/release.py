#!/usr/bin/env python3
"""Bump __version__ (the single source of truth), commit, tag, optionally push.

Pushing the vX.Y.Z tag fires .github/workflows/release.yml (build both OSes -> publish).
"""
import re
import subprocess
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parents[1] / "src" / "wifit3" / "__init__.py"
IDX = {"major": 0, "minor": 1, "patch": 2}


def git(*args):
    r = subprocess.run(("git", *args), text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"git {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def main():
    args = sys.argv[1:]
    push = "--push" in args
    part = next((a for a in args if not a.startswith("-")), "patch")
    if part not in IDX:
        sys.exit(f"usage: release.py [major|minor|patch] [--push]  (got {part!r})")

    if git("status", "--porcelain"):
        sys.exit("working tree dirty: commit or stash first")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "master":
        sys.exit("not on master")

    src = INIT.read_text(encoding="utf-8")
    m = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', src)
    if not m:
        sys.exit(f"no __version__ literal in {INIT}")
    old = list(map(int, m.groups()))
    new = old[:]
    new[IDX[part]] += 1
    for j in range(IDX[part] + 1, 3):
        new[j] = 0
    ver = ".".join(map(str, new))
    tag = f"v{ver}"
    if tag in git("tag", "-l").split():
        sys.exit(f"{tag} already exists")

    INIT.write_text(src[: m.start()] + f'__version__ = "{ver}"' + src[m.end() :], encoding="utf-8")
    git("add", str(INIT))
    git("commit", "-m", f"chore(release): wifit3 {ver}")
    git("tag", tag)
    print(f"{'.'.join(map(str, old))} -> {ver}  (tagged {tag})")
    if push:
        git("push", "origin", "master", tag)
        print(f"pushed. Release workflow now building {tag}.")
    else:
        print(f"next: git push origin master {tag}")


if __name__ == "__main__":
    main()
