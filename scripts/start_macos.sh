#!/bin/sh
set -eu
export PYTHONDONTWRITEBYTECODE=1
if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' '此入口仅用于 macOS；Windows 请使用 scripts/start_windows.bat。' >&2
  exit 1
fi
# Finder may not inherit the Homebrew shell environment.
PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
export PATH
ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PYTHON="$ROOT/.venv/bin/python"
if [ -x "$PYTHON" ]; then
  if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    printf '%s\n' '现有 .venv 需要 Python 3.10 或更新版本；请备份后按 docs/macos.md 重建虚拟环境。' >&2
    exit 1
  fi
else
  PYTHON=""
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    found=$(command -v "$candidate" 2>/dev/null || true)
    if [ -n "$found" ] && "$found" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PYTHON="$found"
      break
    fi
  done
  if [ -z "$PYTHON" ]; then
    printf '%s\n' '找不到 Python 3.10 或更新版本。请先安装 Python（例如 brew install python@3.12），再运行此入口。' >&2
    exit 1
  fi
fi
cd "$ROOT"
exec "$PYTHON" -u "$ROOT/scripts/macos_bootstrap.py"
