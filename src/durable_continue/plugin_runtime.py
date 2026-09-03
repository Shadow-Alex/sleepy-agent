from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_NAME = "sleepy-agent"
DESKTOP_BACKEND = "codex_desktop_native_pipe"
_CHATGPT_APP = Path("/Applications/ChatGPT.app")
_HEARTBEAT_MAX_AGE_SECONDS = 20


def installed_plugin_root(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return base / "plugins" / PLUGIN_NAME


def dispatcher_supported(
    *,
    home: Path | None = None,
    app_path: Path = _CHATGPT_APP,
) -> tuple[bool, str]:
    root = installed_plugin_root(home)
    required = (
        root / ".codex-plugin" / "plugin.json",
        root / ".mcp.json",
        root / "mcp" / "dispatcher.mjs",
        root / "scripts" / "launch_dispatcher.sh",
    )
    if not app_path.is_dir():
        return False, f"Codex Desktop is not installed at {app_path}"
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return False, "Sleepy Agent Desktop plugin is missing: " + ", ".join(
            missing
        )
    launcher = root / "scripts" / "launch_dispatcher.sh"
    if not os.access(launcher, os.X_OK):
        return False, f"Desktop dispatcher launcher is not executable: {launcher}"
    return (
        True,
        "Desktop-owned MCP dispatcher is installed; only the App-authorized native pipe can deliver",
    )


def active_dispatchers(
    *,
    state_root: Path | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    root = state_root or Path(
        os.environ.get(
            "DURABLE_CONTINUE_HOME", str(Path.home() / ".codex" / "durable-continue")
        )
    )
    directory = root.expanduser().absolute() / "dispatchers"
    if not directory.is_dir():
        return []
    current = time.time() if now is None else now
    instances: list[dict[str, Any]] = []
    try:
        candidates = sorted(directory.glob("*.json"))
    except OSError:
        return []
    for candidate in candidates:
        try:
            if candidate.is_symlink() or candidate.stat().st_size > 64 * 1024:
                continue
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            updated_at = _timestamp(payload["updatedAt"])
            if (
                pid <= 0
                or payload.get("backend") != DESKTOP_BACKEND
                or payload.get("nativePipeAvailable") is not True
                or payload.get("stateRuntimeAvailable") is not True
                or current - updated_at > _HEARTBEAT_MAX_AGE_SECONDS
                or updated_at - current > 5
                or not _pid_alive(pid)
            ):
                continue
            instances.append(
                {
                    "pid": pid,
                    "parent_pid": int(payload.get("parentPid") or 0),
                    "version": str(payload.get("version") or ""),
                    "started_at": str(payload.get("startedAt") or ""),
                    "updated_at": str(payload["updatedAt"]),
                }
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return instances


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("heartbeat timestamp must be a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
