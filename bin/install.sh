#!/usr/bin/env bash
# Create a local virtualenv, install lmlog and link the CLI into ~/.local/bin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${1:-$HOME/.local/bin}"
VENV="${VENV:-$ROOT/.venv}"

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -e "$ROOT"

mkdir -p "$DEST_DIR"
ln -sf "$VENV/bin/lmlog" "$DEST_DIR/lmlog"

echo "Installed: $DEST_DIR/lmlog"
if ! echo ":$PATH:" | grep -q ":$DEST_DIR:"; then
    echo "WARNING: $DEST_DIR is not in PATH. Add it, for example:"
    echo "  export PATH=\"$DEST_DIR:\$PATH\""
fi
echo "Run: lmlog"
