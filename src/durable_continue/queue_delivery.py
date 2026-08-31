from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .util import tail_text

VISIBLE_MESSAGE = "continue"
_SUCCESS_RE = re.compile(
    r"Queued message (?P<submission>\S+) for thread (?P<thread>[0-9A-Fa-f-]+)\."
)


class QueueDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout_tail = stdout_tail
        self.stderr_tail = stderr_tail


@dataclass(frozen=True)
class QueueDeliveryResult:
    queued_submission_id: str
    returncode: int
    stdout_tail: str
    stderr_tail: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def resolve_codex_bin(explicit: str | None = None) -> str:
    candidate = (
        explicit
        or os.environ.get("DURABLE_CONTINUE_CODEX_BIN")
        or shutil.which("codex")
    )
    if not candidate:
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled.is_file():
            candidate = str(bundled)
    if not candidate:
        raise ValueError("could not find a codex executable")
    path = Path(candidate).expanduser().absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"codex executable is unavailable or not executable: {path}")
    return str(path)


def queue_supported(
    codex_bin: str,
    *,
    codex_home: str,
    timeout_seconds: int = 10,
    runner: Runner = subprocess.run,
) -> tuple[bool, str]:
    env = _delivery_environment(codex_home)
    try:
        proc = runner(
            [codex_bin, "queue", "--help"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    supported = proc.returncode == 0 and (
        "Queue a message for an existing session" in output
        or "Usage: codex queue" in output
    )
    detail = (
        "codex queue --help succeeded"
        if supported
        else tail_text(output.strip(), 2_000)
    )
    return supported, detail


def deliver_continue(
    *,
    codex_bin: str,
    codex_home: str,
    thread_id: str,
    timeout_seconds: int = 60,
    runner: Runner = subprocess.run,
) -> QueueDeliveryResult:
    command: Sequence[str] = (
        codex_bin,
        "queue",
        "--thread",
        thread_id,
        "--message",
        VISIBLE_MESSAGE,
    )
    try:
        proc = runner(
            command,
            env=_delivery_environment(codex_home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_tail = tail_text(_as_text(exc.stdout))
        stderr_tail = tail_text(_as_text(exc.stderr))
        raise QueueDeliveryError(
            f"codex queue timed out after {timeout_seconds}s",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        ) from exc
    except OSError as exc:
        raise QueueDeliveryError(f"could not execute codex queue: {exc}") from exc

    stdout_tail = tail_text(proc.stdout)
    stderr_tail = tail_text(proc.stderr)
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    match = _SUCCESS_RE.search(combined)
    if proc.returncode != 0:
        detail = tail_text(combined.strip(), 2_000)
        raise QueueDeliveryError(
            f"codex queue exited {proc.returncode}: {detail}",
            returncode=proc.returncode,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    if match is None:
        raise QueueDeliveryError(
            "codex queue returned success without a queued-message receipt",
            returncode=proc.returncode,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    if match.group("thread").lower() != thread_id.lower():
        raise QueueDeliveryError(
            f"codex queue receipt named unexpected thread {match.group('thread')!r}",
            returncode=proc.returncode,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    return QueueDeliveryResult(
        queued_submission_id=match.group("submission"),
        returncode=proc.returncode,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )


def _delivery_environment(codex_home: str) -> Mapping[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    return env


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
