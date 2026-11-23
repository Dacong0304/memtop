#!/bin/bash
# 编译 MemTopBar 并安装到 ~/Applications（不需要 sudo）
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

DEST="$HOME/Applications"
mkdir -p "$DEST"
rm -rf "$DEST/MemTopBar.app"
cp -R "MemTopBar.app" "$DEST/"

# 去掉隔离属性，避免本机自编译产物被 Gatekeeper 拦
xattr -dr com.apple.quarantine "$DEST/MemTopBar.app" 2>/dev/null || true

echo
echo "已安装：$DEST/MemTopBar.app"
echo "启动：  open \"$DEST/MemTopBar.app\""
echo
if [ ! -x "$HOME/.local/bin/memtop" ]; then
  echo "⚠ 没找到 ~/.local/bin/memtop，先执行：cd ../memtop && ./install.sh"
fi
