#!/bin/bash
# 非破坏性自检：只验证读取/排序/保护/演练，不实际结束任何进程
set -uo pipefail
cd "$(dirname "$0")"
BIN="${BIN:-./memtop.py}"
fail=0
ok() { printf "  ✓ %s\n" "$1"; }
bad() { printf "  ✗ %s\n" "$1"; fail=1; }

echo "== 语法"
python3 -m py_compile "$BIN" && ok "编译通过" || bad "编译失败"

echo "== 快照"
out=$(python3 "$BIN" -1 --limit 5)
echo "$out" | grep -q "已用" && ok "内存汇总" || bad "缺少内存汇总"
echo "$out" | grep -q "进程" && ok "进程列表" || bad "缺少进程列表"

echo "== 排序（前一行内存应 >= 后一行）"
python3 "$BIN" --json | python3 -c '
import json,sys
d=json.load(sys.stdin)
fps=[g["footprint"] for g in d["groups"]]
assert fps==sorted(fps, reverse=True), "未按内存降序"
print("  ✓ 降序正确（%d 组）" % len(fps))
' || bad "排序检查失败"

echo "== 保护规则"
python3 "$BIN" kill 1 --yes >/dev/null 2>&1
[ $? -eq 2 ] && ok "拒绝结束 launchd" || bad "未拒绝 pid 1"

echo "== 演练不杀进程"
sleep 60 & dummy=$!
python3 "$BIN" kill "$dummy" --dry-run >/dev/null 2>&1
if kill -0 "$dummy" 2>/dev/null; then ok "dry-run 未影响进程"; else bad "dry-run 误杀"; fi
kill "$dummy" 2>/dev/null

echo "== Web 仪表盘"
PORT=$((20000 + RANDOM % 20000))
python3 "$BIN" web --port "$PORT" --no-open >/tmp/memtop-selftest-web.log 2>&1 &
srv=$!
cleanup() { kill "$srv" 2>/dev/null; wait "$srv" 2>/dev/null; }
trap cleanup EXIT
for _ in $(seq 1 40); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/api/snapshot" && break
  sleep 0.25
done
curl -s "http://127.0.0.1:$PORT/api/snapshot" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["ok"] and d["groups"], "快照为空"
assert d["memory"]["total"] > 0
print("  ✓ 快照接口（%d 组）" % len(d["groups"]))
' || bad "快照接口失败"

code=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" \
  -d '{"pids":[1]}' "http://127.0.0.1:$PORT/api/kill")
[ "$code" = "403" ] && ok "写接口无 token 被拒（403）" || bad "写接口鉴权异常：$code"

curl -s "http://127.0.0.1:$PORT/" | grep -q "memtop-token" && ok "首页注入会话 token" || bad "首页缺少 token"

cleanup
trap - EXIT

echo
[ $fail -eq 0 ] && echo "全部通过" || echo "有失败项"
exit $fail
