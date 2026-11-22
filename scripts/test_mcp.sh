#!/usr/bin/env bash
# Run the real MCP + git + web round-trip probe against a live capforge server.
#
#   CAPFORGE_MCP_TOKEN=... scripts/test_mcp.sh
#
# Override CAPFORGE_URL / CAPFORGE_WEB_PROBE_URL when the server is not on the
# default local ports. The probe creates temporary capabilities and deletes them.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -z "${CAPFORGE_MCP_TOKEN:-}" ]]; then
  echo "CAPFORGE_MCP_TOKEN is required for the HTTP MCP probe" >&2
  exit 2
fi

exec uv run --locked python tools/mcp_probe.py "$@"
