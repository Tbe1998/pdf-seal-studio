#!/usr/bin/env bash
# 启动器：./run.sh [参数]   可用 MULTISIGN_PYTHON 指定解释器
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${MULTISIGN_PYTHON:-}" ]; then
  PY="$MULTISIGN_PYTHON"
elif [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
elif [ -x "$HOME/.venv-sign/bin/python" ]; then
  PY="$HOME/.venv-sign/bin/python"
else
  PY=python3
fi
exec "$PY" "$HERE/multisign.py" "$@"
