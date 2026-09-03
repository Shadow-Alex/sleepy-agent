import json
import os
from datetime import datetime, timezone
from pathlib import Path

from durable_continue.plugin_runtime import (
    DESKTOP_BACKEND,
    active_dispatchers,
    dispatcher_supported,
)


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def test_dispatcher_support_requires_complete_executable_plugin(
    tmp_path: Path,
) -> None:
    app = tmp_path / "ChatGPT.app"
    app.mkdir()
    root = tmp_path / "plugins" / "sleepy-agent"
    for relative in (
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "mcp/dispatcher.mjs",
        "scripts/launch_dispatcher.sh",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    (root / "scripts" / "launch_dispatcher.sh").chmod(0o755)

    supported, _detail = dispatcher_supported(home=tmp_path, app_path=app)

    assert supported is True


def test_active_dispatchers_accepts_only_fresh_live_heartbeat(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "dispatchers"
    directory.mkdir()
    payload = {
        "backend": DESKTOP_BACKEND,
        "pid": os.getpid(),
        "parentPid": os.getppid(),
        "version": "0.4.0",
        "startedAt": timestamp(900),
        "updatedAt": timestamp(995),
        "nativePipeAvailable": True,
        "stateRuntimeAvailable": True,
    }
    (directory / f"{os.getpid()}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    (directory / "stale.json").write_text(
        json.dumps({**payload, "updatedAt": timestamp(900)}), encoding="utf-8"
    )
    (directory / "malformed.json").write_text("not json", encoding="utf-8")

    instances = active_dispatchers(state_root=tmp_path, now=1000)

    assert instances == [
        {
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "version": "0.4.0",
            "started_at": timestamp(900),
            "updated_at": timestamp(995),
        }
    ]
