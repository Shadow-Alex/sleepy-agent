from pathlib import Path

from durable_continue.checker import CheckDecision, CheckObservation
from durable_continue.queue_worker import run_queue_worker
from durable_continue.store import QUEUE_PENDING, QUEUED, Store

THREAD_ID = "019f0000-0000-7000-8000-000000000005"


def pending_monitor(store: Store, codex_bin: str) -> tuple[dict, dict]:
    monitor = store.register(
        thread_id=THREAD_ID,
        cwd="/tmp",
        check_command="echo SUCCESS",
        success_regex="^SUCCESS$",
        failure_regex=None,
        interval_seconds=1,
        timeout_seconds=10,
        check_timeout_seconds=2,
        codex_home="/tmp/codex-home",
        codex_bin=codex_bin,
        now=1000,
    )
    check = store.claim_due_check(now=1001)
    assert check is not None
    observation = CheckObservation(0, "SUCCESS", "", None, False)
    assert store.complete_check(
        monitor["id"],
        check["claim_token"],
        CheckDecision("success", observation),
        now=1001,
    )
    queue = store.claim_due_queue(now=1001)
    assert queue is not None
    return monitor, queue


def executable(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-codex"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_worker_records_queue_receipt(tmp_path: Path) -> None:
    fake = executable(
        tmp_path,
        f"printf 'Queued message q_worker for thread {THREAD_ID}.\\n'\n",
    )
    store = Store(tmp_path / "state.sqlite3")
    monitor, queue = pending_monitor(store, fake)
    assert (
        run_queue_worker(
            monitor["id"],
            queue["claim_token"],
            db_path=store.path,
            delivery_timeout_seconds=5,
        )
        == 0
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == QUEUED
    assert current["queued_submission_id"] == "q_worker"


def test_worker_failure_remains_pending(tmp_path: Path) -> None:
    fake = executable(tmp_path, "printf 'temporary failure\\n' >&2\nexit 9\n")
    store = Store(tmp_path / "state.sqlite3")
    monitor, queue = pending_monitor(store, fake)
    assert (
        run_queue_worker(
            monitor["id"],
            queue["claim_token"],
            db_path=store.path,
            retry_seconds=7,
            delivery_timeout_seconds=5,
        )
        == 75
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == QUEUE_PENDING
    assert current["last_queue_returncode"] == 9
    assert "temporary failure" in current["last_error"]
