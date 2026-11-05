#!/bin/sh
SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
/bin/sh "$SCRIPT_DIR/start_macos.sh"
result=$?
if [ "$result" -ne 0 ]; then
  printf '%s\n' '启动失败，请阅读上方错误及日志位置。'
  if [ -t 0 ]; then
    printf '%s\n' '按回车键关闭窗口。'
    read -r ignored
  fi
fi
exit "$result"
