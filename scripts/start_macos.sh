#!/bin/sh
set -eu
export PYTHONDONTWRITEBYTECODE=1
if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' '此入口仅用于 macOS；Windows 请使用 scripts/start_windows.bat。' >&2
  exit 1
fi
ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  printf '%s\n' '未找到项目虚拟环境，请按照 docs/macos.md 创建并安装依赖。' >&2
  exit 1
fi
# Finder may not inherit the Homebrew shell environment.
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
cd "$ROOT"
exec "$PYTHON" -u "$ROOT/scripts/app_control.py" start
