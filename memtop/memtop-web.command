#!/bin/bash
# 双击启动 Web 仪表盘（会自动打开浏览器）
BIN="$HOME/.local/bin/memtop"
cd "$(dirname "$0")"
if [ ! -x "$BIN" ]; then
  echo "还没安装 memtop，先运行：./install.sh"
  read -r -p "按回车关闭…" _
  exit 1
fi
exec "$BIN" web
