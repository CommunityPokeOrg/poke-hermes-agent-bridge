#!/usr/bin/env bash
# Usage: BRIDGE_URL=http://127.0.0.1:8700 BRIDGE_KEY=<key> ./curl.sh
set -euo pipefail

BASE="${BRIDGE_URL:-http://127.0.0.1:8700}"
KEY="${BRIDGE_KEY:?set BRIDGE_KEY to a BRIDGE_API_KEYS entry}"

echo "== health"
curl -sf "$BASE/health"; echo

echo "== ready"
curl -s "$BASE/health/ready"; echo

echo "== capabilities"
curl -sf -H "Authorization: Bearer $KEY" "$BASE/v1/capabilities"; echo

echo "== sync task"
curl -sf -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "say hi in one word"}' "$BASE/v1/tasks"; echo

echo "== async task"
TASK=$(curl -sf -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "[tools] build a thing", "mode": "async"}' "$BASE/v1/tasks")
echo "$TASK"
ID=$(echo "$TASK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== events for $ID"
curl -sN -H "Authorization: Bearer $KEY" "$BASE/v1/tasks/$ID/events" &
CURL_PID=$!
sleep 2
kill $CURL_PID 2>/dev/null || true

echo "== final task"
curl -sf -H "Authorization: Bearer $KEY" "$BASE/v1/tasks/$ID"; echo
