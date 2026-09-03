#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_BIN="${CODEX_MCP_NODE_PATH:-/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node}"

if [[ -z "${CODEX_APP_TOOLS_PIPE_PATH:-}" ]]; then
  echo "durable-continue dispatcher requires the Codex Desktop native pipe" >&2
  exit 78
fi
if [[ ! -x "${NODE_BIN}" ]]; then
  echo "durable-continue dispatcher cannot find the Codex Desktop Node runtime" >&2
  exit 78
fi

exec "${NODE_BIN}" "${PLUGIN_ROOT}/mcp/dispatcher.mjs"
