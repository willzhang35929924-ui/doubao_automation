#!/bin/sh
set -eu
export PYTHONDONTWRITEBYTECODE=1
if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' '此入口仅用于 macOS。' >&2
  exit 1
fi
PROJECT_ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PROJECT_PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PROJECT_PYTHON" ]; then
  printf '%s\n' '未找到项目虚拟环境，无法确认和关闭服务。' >&2
  exit 1
fi
cd "$PROJECT_ROOT"
exec "$PROJECT_PYTHON" -u "$PROJECT_ROOT/scripts/app_control.py" stop
