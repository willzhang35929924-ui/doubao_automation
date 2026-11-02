#!/bin/sh
SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
/bin/sh "$SCRIPT_DIR/stop_macos.sh"
result=$?
printf '%s\n' '按回车键关闭窗口。'
read -r ignored
exit "$result"
