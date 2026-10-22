#!/usr/bin/env bash
# 一键安装依赖（Linux/macOS）。用法: bash install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY=${PYTHON:-python3}
if command -v uv >/dev/null 2>&1; then
  echo ">> 用 uv 创建虚拟环境 .venv"
  uv venv .venv --python 3.13 || uv venv .venv
  VIRTUAL_ENV="$HERE/.venv" uv pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
else
  echo ">> 用 venv + pip 创建虚拟环境 .venv"
  "$PY" -m venv .venv
  ./.venv/bin/pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
  ./.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
fi
echo
echo "安装完成。运行方式："
echo "  MULTISIGN_PYTHON=$HERE/.venv/bin/python ./run.sh --help"
echo "  或 source .venv/bin/activate 后 python multisign.py --help"
