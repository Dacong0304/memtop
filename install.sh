#!/bin/bash
# 一键安装：命令行 + Web 仪表盘 + 菜单栏 App
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/2 命令行 / TUI / Web"
(cd memtop && ./install.sh)

echo
echo "==> 2/2 菜单栏 App"
(cd memtop-bar && ./install.sh)

echo
echo "全部完成。运行：memtop        或        open ~/Applications/MemTopBar.app"
