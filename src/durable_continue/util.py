from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_ts() -> float:
    return time.time()


def iso_utc(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def tail_text(value: str | None, limit: int = 16_384) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def durable_home() -> Path:
    raw = os.environ.get("DURABLE_CONTINUE_HOME")
    path = (
        Path(raw).expanduser() if raw else Path.home() / ".codex" / "durable-continue"
    )
    return ensure_private_dir(path)


def database_path() -> Path:
    return durable_home() / "state.sqlite3"


def codex_home_from_environment() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return (
        Path(raw).expanduser().absolute()
        if raw
        else (Path.home() / ".codex").absolute()
    )


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
