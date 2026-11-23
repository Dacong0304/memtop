#!/bin/bash
# 构建 MemTopBar.app（无需 Xcode 工程，只要 Command Line Tools）
set -euo pipefail
cd "$(dirname "$0")"

APP="MemTopBar.app"
BIN="$APP/Contents/MacOS/MemTopBar"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "编译 main.swift …"
swiftc -O -swift-version 5 \
  -framework AppKit \
  -o "$BIN" main.swift

cp Info.plist "$APP/Contents/Info.plist"
if [ -f AppIcon.icns ]; then
  cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
fi

# 本地临时签名，避免 Gatekeeper 对未签名包的骚扰（无证书也能过）
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "完成：$(pwd)/$APP"
echo "试用：open '$APP'"
