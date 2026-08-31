#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_ROOT="${DURABLE_CONTINUE_HOME:-${HOME}/.codex/durable-continue}"
INSTALL_SOURCE="${STATE_ROOT}/source"
VENV="${STATE_ROOT}/venv"
SKILL_DIR="${HOME}/.codex/skills/durable-continue"
BIN_DIR="${HOME}/.local/bin"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST="${PLIST_DIR}/io.github.shadow-alex.durable-continue.plist"
LOG_DIR="${STATE_ROOT}/logs"
CODEX_HOME_VALUE="${CODEX_HOME:-${HOME}/.codex}"
PYTHON_BIN="${DURABLE_CONTINUE_PYTHON:-$(command -v python3)}"

"${PYTHON_BIN}" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"durable-continue requires Python 3.10+, found {sys.version.split()[0]}")
PY

mkdir -p "${STATE_ROOT}" "${INSTALL_SOURCE}" "${SKILL_DIR}/agents" \
  "${BIN_DIR}" "${PLIST_DIR}" "${LOG_DIR}"
chmod 700 "${STATE_ROOT}" "${LOG_DIR}"

rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' \
  --exclude 'build' --exclude 'dist' --exclude '*.egg-info' \
  "${SOURCE_DIR}/" "${INSTALL_SOURCE}/"

"${PYTHON_BIN}" -m venv "${VENV}"
SITE_PACKAGES="$("${VENV}/bin/python" - <<'PY'
import site
print(site.getsitepackages()[0])
PY
)"
printf '%s\n' "${INSTALL_SOURCE}/src" > "${SITE_PACKAGES}/durable_continue.pth"

cat > "${BIN_DIR}/durable-continue" <<WRAPPER
#!/usr/bin/env bash
exec "${VENV}/bin/python" -m durable_continue.cli "\$@"
WRAPPER
chmod 755 "${BIN_DIR}/durable-continue"

install -m 644 "${INSTALL_SOURCE}/skills/durable-continue/SKILL.md" "${SKILL_DIR}/SKILL.md"
install -m 644 "${INSTALL_SOURCE}/skills/durable-continue/agents/openai.yaml" "${SKILL_DIR}/agents/openai.yaml"

"${PYTHON_BIN}" - \
  "${INSTALL_SOURCE}/launchd/io.github.shadow-alex.durable-continue.plist.in" \
  "${PLIST}" "${VENV}/bin/python" "${HOME}" "${CODEX_HOME_VALUE}" \
  "${STATE_ROOT}" "${LOG_DIR}" "${INSTALL_SOURCE}/src" "${INSTALL_SOURCE}" <<'PY'
from html import escape
from pathlib import Path
import sys

src, dst, python, home, codex_home, state_root, log_dir, pythonpath, source_dir = sys.argv[1:]
path = f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
values = {
    "__PYTHON__": python,
    "__HOME__": home,
    "__CODEX_HOME__": codex_home,
    "__STATE_ROOT__": state_root,
    "__PATH__": path,
    "__PYTHONPATH__": pythonpath,
    "__LOG_DIR__": log_dir,
    "__SOURCE_DIR__": source_dir,
}
text = Path(src).read_text(encoding="utf-8")
for key, value in values.items():
    text = text.replace(key, escape(value, quote=True))
Path(dst).write_text(text, encoding="utf-8")
PY

plutil -lint "${PLIST}"
launchctl bootout "gui/${UID}" "${PLIST}" >/dev/null 2>&1 \
  || launchctl bootout "gui/${UID}/io.github.shadow-alex.durable-continue" >/dev/null 2>&1 \
  || true
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
  if ! launchctl print "gui/${UID}/io.github.shadow-alex.durable-continue" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

loaded=false
for _attempt in 1 2 3 4 5; do
  if launchctl bootstrap "gui/${UID}" "${PLIST}"; then
    loaded=true
    break
  fi
  sleep 0.25
done
if [[ "${loaded}" != true ]]; then
  echo "failed to bootstrap io.github.shadow-alex.durable-continue" >&2
  exit 1
fi
launchctl kickstart -k "gui/${UID}/io.github.shadow-alex.durable-continue"

echo "installed CLI: ${BIN_DIR}/durable-continue"
echo "installed skill: ${SKILL_DIR}"
echo "installed source: ${INSTALL_SOURCE}"
echo "state root: ${STATE_ROOT}"
echo "LaunchAgent: ${PLIST}"
"${BIN_DIR}/durable-continue" doctor
