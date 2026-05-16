#!/usr/bin/env bash
# 纯 cURL 快速体验所有核心端点 — 适合不写代码也想验证 xGate 部署的场景
#
# 用法:
#   export XGATE_API_KEY="你的-api-key"
#   ./examples/curl_quickstart.sh

set -e

BASE="${XGATE_BASE_URL:-http://127.0.0.1:8024}"
KEY="${XGATE_API_KEY:?请先 export XGATE_API_KEY}"
H=(-H "Authorization: Bearer $KEY")
JSON=(-H "Content-Type: application/json")

section() { echo; echo "═══ $1 ═══"; }

section "1. health（无需 auth）"
curl -s "$BASE/health" | python3 -m json.tool

section "2. 列模型"
curl -s "${H[@]}" "$BASE/v1/models" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for m in d["data"][:6]:
    print(f"  - {m[\"id\"]}")
'

section "3. 列账号池"
curl -s "${H[@]}" "$BASE/admin/accounts" | python3 -c '
import json, sys
for a in json.load(sys.stdin)["accounts"]:
    print(f"  {a[\"label\"]:15s} prio={a[\"priority\"]} status={a[\"status\"]}")
'

section "4. quota 当前账号"
curl -s -X POST "${H[@]}" "$BASE/v1/quota" | python3 -m json.tool

section "5. per_account quota 快照（dashboard）"
curl -s -X POST "${H[@]}" "$BASE/admin/dashboard" | python3 -c '
import json, sys
d = json.load(sys.stdin)
pa = d.get("per_account", {})
print(f"  账号数: {len(pa)}")
for label, items in pa.items():
    print(f"  - {label}: {len(items)} 模型")
'

section "6. chat completions (非流式，节省 token 限制 50)"
curl -s -X POST "${H[@]}" "${JSON[@]}" "$BASE/v1/chat/completions" -d '{
  "model": "grok-4.20-fast",
  "messages": [{"role": "user", "content": "用一句话回答：今天天气怎么样？"}],
  "max_tokens": 50
}' | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "error" in d:
    print(f"  ERROR: {d[\"error\"]}")
else:
    print(f"  回复: {d[\"choices\"][0][\"message\"][\"content\"]}")
    if "usage" in d:
        u = d["usage"]
        print(f"  用量: {u[\"total_tokens\"]} tokens (prompt {u[\"prompt_tokens\"]} + completion {u[\"completion_tokens\"]})")
'

section "7. X-Account-Label 强制走 default"
curl -s -X POST "${H[@]}" -H "X-Account-Label: default" "$BASE/v1/quota" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "error" in d:
    print(f"  ERROR: {d[\"error\"]}")
else:
    for mode, q in d.get("quotas", {}).items():
        print(f"  default mode={mode}: {q[\"remaining\"]}/{q[\"total\"]}")
'

section "8. 日志统计"
curl -s "${H[@]}" "$BASE/v1/logs/stats" | python3 -m json.tool

echo
echo "完成。"
