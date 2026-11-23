#!/bin/bash
# 由 1024 PNG 生成 AppIcon.icns（输出到 memtop-bar/AppIcon.icns）
set -euo pipefail
cd "$(dirname "$0")"

SRC="${1:-out/icon-b.png}"
NAME="AppIcon"
SET="$NAME.iconset"

rm -rf "$SET"
mkdir -p "$SET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$SRC" --out "$SET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$SRC" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
done
# 1024 作为 512@2x
sips -z 1024 1024 "$SRC" --out "$SET/icon_512x512@2x.png" >/dev/null

iconutil -c icns "$SET" -o ../AppIcon.icns
rm -rf "$SET"
echo "built ../AppIcon.icns"
