from pathlib import Path

import pytest

from durable_continue.checker import (
    CheckObservation,
    compile_regex,
    decide,
    run_checker,
)


def observation(output: str, returncode: int = 0) -> CheckObservation:
    return CheckObservation(returncode, output, "", None, False)


def test_failure_precedes_success() -> None:
    result = decide(
        observation("SUCCESS\nFAILED"),
        success_regex=r"SUCCESS",
        failure_regex=r"FAILED",
        deadline_due=False,
    )
    assert result.wake_reason == "failure"


def test_nonzero_returncode_alone_does_not_wake() -> None:
    result = decide(
        observation("temporary ssh problem", returncode=255),
        success_regex=r"^SUCCESS$",
        failure_regex=r"^FAILED$",
        deadline_due=False,
    )
    assert result.wake_reason is None


def test_deadline_is_final_fallback() -> None:
    result = decide(
        observation("RUNNING"),
        success_regex=r"^SUCCESS$",
        failure_regex=r"^FAILED$",
        deadline_due=True,
    )
    assert result.wake_reason == "timeout"


def test_success_wins_over_timeout_after_final_check() -> None:
    result = decide(
        observation("SUCCESS"),
        success_regex=r"^SUCCESS$",
        failure_regex=r"^FAILED$",
        deadline_due=True,
    )
    assert result.wake_reason == "success"


def test_invalid_regex_rejected() -> None:
    with pytest.raises(ValueError):
        compile_regex("[")


def test_checker_process_timeout_is_bounded(tmp_path: Path) -> None:
    result = run_checker("sleep 5", cwd=str(tmp_path), timeout_seconds=1)
    assert result.process_timed_out is True
    assert "timed out" in (result.error or "")
