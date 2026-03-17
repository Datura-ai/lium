#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Building lium binary ==="
uv run pyinstaller lium.spec --clean

echo ""
echo "=== Output ==="
ls -lh dist/lium
echo ""
echo "Test with: ./dist/lium --version"
