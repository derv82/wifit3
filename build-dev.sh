#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT_DIR"

VERSION_FILE=src/wifit3/__init__.py
ORIGINAL_CONTENT=$(cat "$VERSION_FILE")
BASE_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$VERSION_FILE" | head -n 1)
if [ -z "$BASE_VERSION" ]; then
    echo "could not read __version__ from $VERSION_FILE" >&2
    exit 1
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    COMMIT=$(git rev-parse --short HEAD)
else
    COMMIT=nogit
fi

DEV_VERSION="$BASE_VERSION-dev-$COMMIT"
ARTIFACT_NAME="wifit3-linux-x64-$DEV_VERSION"

restore_version() {
    printf '%s' "$ORIGINAL_CONTENT" > "$VERSION_FILE"
}
trap restore_version EXIT INT TERM

python3 - "$VERSION_FILE" "$BASE_VERSION" "$DEV_VERSION" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
base = sys.argv[2]
dev = sys.argv[3]
text = path.read_text()
old = f'__version__ = "{base}"'
new = f'__version__ = "{dev}"'
if old not in text:
    raise SystemExit(f"version line not found: {old}")
path.write_text(text.replace(old, new, 1))
PY

uv sync --group dev
uv run pyinstaller wifit3.spec --noconfirm --clean

chmod +x dist/wifit3 || true
dist/wifit3 --version
dist/wifit3 --smoke

mkdir -p out
cp dist/wifit3 "out/$ARTIFACT_NAME"

echo "built out/$ARTIFACT_NAME"
