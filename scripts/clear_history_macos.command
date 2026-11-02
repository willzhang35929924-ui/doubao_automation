#!/bin/bash
set -u
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' '未找到项目虚拟环境。请先按文档创建并安装依赖。'
  read -r -p '按回车退出...'
  exit 1
fi
.venv/bin/python scripts/app_control.py clear-history
result=$?
printf '%s\n' '当前仅提供只读预览，历史清理尚不可用。'
read -r -p '按回车退出...'
exit "$result"
