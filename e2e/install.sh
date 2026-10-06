#!/usr/bin/env bash
# This checkout's CLI + SDK (editable) plus pytest into e2e/.venv. uv when available (CI installs it), pip otherwise.
set -euo pipefail
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  uv venv -q --clear .venv
  uv pip install -q --python .venv/bin/python -e .. -r requirements.txt
else
  python3 -m venv .venv
  .venv/bin/pip install -q -e .. -r requirements.txt
fi
.venv/bin/lium --version
