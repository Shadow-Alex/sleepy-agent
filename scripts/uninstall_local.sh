#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${DURABLE_CONTINUE_HOME:-${HOME}/.codex/durable-continue}"
PLIST="${HOME}/Library/LaunchAgents/io.github.shadow-alex.durable-continue.plist"
CLI="${HOME}/.local/bin/durable-continue"
SKILL_DIR="${HOME}/.codex/skills/sleepy-agent"

launchctl bootout "gui/${UID}" "${PLIST}" >/dev/null 2>&1 \
  || launchctl bootout "gui/${UID}/io.github.shadow-alex.durable-continue" >/dev/null 2>&1 \
  || true
rm -f -- "${PLIST}" "${CLI}"
rm -rf -- "${SKILL_DIR}"

if [[ "${1:-}" == "--purge" ]]; then
  case "${STATE_ROOT}" in
    */.codex/durable-continue) rm -rf -- "${STATE_ROOT}" ;;
    *) echo "refusing to purge unexpected state root: ${STATE_ROOT}" >&2; exit 2 ;;
  esac
  echo "removed implementation and state: ${STATE_ROOT}"
else
  echo "daemon, CLI, and skill removed; durable state preserved at ${STATE_ROOT}"
fi
