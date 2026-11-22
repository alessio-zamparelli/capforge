#!/usr/bin/env bash
# mcp-header-probe.sh — MCP handshake + header probe against a FastMCP server.
# Usage: ./mcp-header-probe.sh [URL]
# Requires: bash, curl, python3. No files created.
set -euo pipefail

URL="${1:-http://serben.wdomain:8081/mcp}"
PROTO="2025-03-26"

post() {
  local sid="$1" payload="$2"; shift 2
  local args=(-s -X POST "$URL"
    -H 'Content-Type: application/json'
    -H 'Accept: application/json, text/event-stream')
  [ -n "$sid" ] && args+=(-H "mcp-session-id: $sid")
  args+=("$@")
  curl "${args[@]}" -d "$payload"
}

# 1. initialize -> capture mcp-session-id
SID=$(curl -sD- -o /dev/null -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"$PROTO\",\"capabilities\":{},\"clientInfo\":{\"name\":\"probe\",\"version\":\"0\"}}}" \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
[ -n "$SID" ] || { echo "ERROR: no mcp-session-id from $URL" >&2; exit 1; }
echo "session: $SID"

# 2. required notification
curl -s -o /dev/null -w 'initialized: HTTP %{http_code}\n' -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

show() {
  python3 -c 'import json,sys
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("data: "): continue
    msg = json.loads(line[6:])
    res = msg.get("result", msg)
    print(json.dumps(res.get("structuredContent", res), indent=2, sort_keys=True))'
}

# 3. identity headers, like Open WebUI with ENABLE_FORWARD_USER_INFO_HEADERS=true
echo; echo "== whoami (Open WebUI-style headers) =="
post "$SID" '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"whoami","arguments":{}}}' \
  -H 'X-OpenWebUI-User-Id: test-user-123' \
  -H 'X-OpenWebUI-User-Email: demo@example.test' \
  -H 'X-OpenWebUI-User-Role: admin' \
  -H 'X-OpenWebUI-Chat-Id: test-chat-1' \
  -H 'X-Template-Test: {{USER_ID}}' \
  -H 'Authorization: Bearer demo-token' | show

# 4. same call with no identity headers (what a non-Open-WebUI client sends)
echo; echo "== whoami (bare MCP client) =="
post "$SID" '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"whoami","arguments":{}}}' | show

# 5. every header on the request
echo; echo "== echo_headers (all headers) =="
post "$SID" '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"echo_headers","arguments":{}}}' \
  -H 'X-OpenWebUI-User-Id: test-user-123' | show
