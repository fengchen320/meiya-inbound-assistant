#!/usr/bin/env bash
# 美亚入库助手 V1.0 —— Linux / macOS 启动脚本
set -euo pipefail
cd "$(dirname "$0")/.."

PY=python3
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi

PORT="${PORT:-8799}"
exec "$PY" tools/ui.py --port "$PORT" --open
