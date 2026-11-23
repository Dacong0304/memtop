#!/bin/bash
# 安装 memtop：命令行/TUI + Web 仪表盘（不需要 sudo）
set -euo pipefail
cd "$(dirname "$0")"

BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share/memtop"

mkdir -p "$BIN" "$SHARE"
install -m 755 memtop.py "$BIN/memtop"
rm -rf "$SHARE/web"
cp -R web "$SHARE/web"

echo "已安装："
echo "  $BIN/memtop"
echo "  $SHARE/web"
echo
echo "试用："
echo "  memtop                 # 交互界面"
echo "  memtop web             # 浏览器仪表盘"
echo
case ":$PATH:" in
  *":$BIN:"*) echo "$BIN 已在 PATH 中" ;;
  *) echo "提示：把 $BIN 加进 PATH，例如 echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
esac
