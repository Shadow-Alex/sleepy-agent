#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${DURABLE_CONTINUE_HOME:-${HOME}/.codex/durable-continue}"
PLIST="${HOME}/Library/LaunchAgents/io.github.shadow-alex.durable-continue.plist"
CLI="${HOME}/.local/bin/durable-continue"
SKILL_DIR="${HOME}/.codex/skills/sleepy-agent"
MARKETPLACE_FILE="${HOME}/.agents/plugins/marketplace.json"
PLUGIN_CREATOR_SCRIPTS="${CODEX_HOME:-${HOME}/.codex}/skills/.system/plugin-creator/scripts"

launchctl bootout "gui/${UID}" "${PLIST}" >/dev/null 2>&1 \
  || launchctl bootout "gui/${UID}/io.github.shadow-alex.durable-continue" >/dev/null 2>&1 \
  || true

if command -v codex >/dev/null 2>&1 \
  && [[ -f "${MARKETPLACE_FILE}" ]] \
  && [[ -f "${PLUGIN_CREATOR_SCRIPTS}/read_marketplace_name.py" ]]; then
  marketplace_name="$(
    python3 "${PLUGIN_CREATOR_SCRIPTS}/read_marketplace_name.py" \
      --marketplace-path "${MARKETPLACE_FILE}"
  )"
  codex plugin remove "sleepy-agent@${marketplace_name}" --json || true
fi
rm -f -- "${PLIST}" "${CLI}"
rm -rf -- "${SKILL_DIR}"

if [[ "${1:-}" == "--purge" ]]; then
  case "${STATE_ROOT}" in
    */.codex/durable-continue) rm -rf -- "${STATE_ROOT}" ;;
    *) echo "refusing to purge unexpected state root: ${STATE_ROOT}" >&2; exit 2 ;;
  esac
  echo "removed implementation and state: ${STATE_ROOT}"
else
  echo "daemon, CLI, skill, and plugin registration removed; durable state preserved at ${STATE_ROOT}"
fi
