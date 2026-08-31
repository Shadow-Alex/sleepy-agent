from __future__ import annotations

import subprocess
from typing import Any

import pytest

from durable_continue.queue_delivery import (
    VISIBLE_MESSAGE,
    QueueDeliveryError,
    deliver_continue,
    queue_supported,
)

THREAD_ID = "019f0000-0000-7000-8000-000000000004"


def test_exact_queue_command_and_environment() -> None:
    captured: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = list(command)
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            f"Queued message qs_123 for thread {THREAD_ID}.\n",
            "",
        )

    result = deliver_continue(
        codex_bin="/opt/codex",
        codex_home="/tmp/codex-home",
        thread_id=THREAD_ID,
        runner=runner,
    )

    assert captured["command"] == [
        "/opt/codex",
        "queue",
        "--thread",
        THREAD_ID,
        "--message",
        "continue",
    ]
    assert captured["env"]["CODEX_HOME"] == "/tmp/codex-home"
    assert result.queued_submission_id == "qs_123"
    assert VISIBLE_MESSAGE == "continue"


def test_nonzero_queue_result_is_retryable_error() -> None:
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 3, "", "daemon too old")

    with pytest.raises(QueueDeliveryError) as raised:
        deliver_continue(
            codex_bin="/opt/codex",
            codex_home="/tmp/codex-home",
            thread_id=THREAD_ID,
            runner=runner,
        )
    assert raised.value.returncode == 3
    assert "daemon too old" in str(raised.value)


def test_receipt_must_name_exact_thread() -> None:
    other = "019f0000-0000-7000-8000-000000000099"

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, f"Queued message q1 for thread {other}.\n", ""
        )

    with pytest.raises(QueueDeliveryError, match="unexpected thread"):
        deliver_continue(
            codex_bin="/opt/codex",
            codex_home="/tmp/codex-home",
            thread_id=THREAD_ID,
            runner=runner,
        )


def test_queue_capability_probe() -> None:
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, "Usage: codex queue [OPTIONS]", ""
        )

    supported, detail = queue_supported(
        "/opt/codex",
        codex_home="/tmp/codex-home",
        runner=runner,
    )
    assert supported is True
    assert detail == "codex queue --help succeeded"
