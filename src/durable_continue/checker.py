from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .util import tail_text

_REGEX_FLAGS = re.IGNORECASE | re.MULTILINE


@dataclass(frozen=True)
class CheckObservation:
    returncode: int | None
    stdout_tail: str
    stderr_tail: str
    error: str | None
    process_timed_out: bool

    @property
    def combined(self) -> str:
        pieces = [self.stdout_tail, self.stderr_tail]
        if self.error:
            pieces.append(self.error)
        return "\n".join(pieces)


@dataclass(frozen=True)
class CheckDecision:
    wake_reason: str | None
    observation: CheckObservation


def compile_regex(
    pattern: str | None, *, required: bool = False
) -> re.Pattern[str] | None:
    if pattern is None or pattern == "":
        if required:
            raise ValueError("success regex is required")
        return None
    try:
        return re.compile(pattern, _REGEX_FLAGS)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc


def default_shell() -> str:
    if os.path.exists("/bin/zsh"):
        return "/bin/zsh"
    return shutil.which("zsh") or shutil.which("bash") or "/bin/sh"


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_checker(
    command: str,
    *,
    cwd: str,
    timeout_seconds: int,
    output_tail_chars: int = 16_384,
) -> CheckObservation:
    shell = default_shell()
    try:
        proc = subprocess.Popen(
            [shell, "-lc", command],
            cwd=str(Path(cwd).expanduser()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(proc)
            stdout, stderr = proc.communicate()
            return CheckObservation(
                returncode=None,
                stdout_tail=tail_text(
                    stdout or _as_text(exc.stdout), output_tail_chars
                ),
                stderr_tail=tail_text(
                    stderr or _as_text(exc.stderr), output_tail_chars
                ),
                error=f"checker timed out after {timeout_seconds}s",
                process_timed_out=True,
            )
        return CheckObservation(
            returncode=proc.returncode,
            stdout_tail=tail_text(stdout, output_tail_chars),
            stderr_tail=tail_text(stderr, output_tail_chars),
            error=None,
            process_timed_out=False,
        )
    except Exception as exc:  # noqa: BLE001 - observation failures are recorded data
        return CheckObservation(
            returncode=None,
            stdout_tail="",
            stderr_tail="",
            error=f"checker execution error: {type(exc).__name__}: {exc}",
            process_timed_out=False,
        )


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def decide(
    observation: CheckObservation,
    *,
    success_regex: str,
    failure_regex: str | None,
    deadline_due: bool,
) -> CheckDecision:
    failure = compile_regex(failure_regex)
    success = compile_regex(success_regex, required=True)
    combined = observation.combined

    if failure is not None and failure.search(combined):
        return CheckDecision("failure", observation)
    if success is not None and success.search(combined):
        return CheckDecision("success", observation)
    if deadline_due:
        return CheckDecision("timeout", observation)
    return CheckDecision(None, observation)
