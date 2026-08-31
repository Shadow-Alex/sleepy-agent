import time
from pathlib import Path

from durable_continue.daemon import DurableContinueDaemon
from durable_continue.store import QUEUE_PENDING, WAITING, Store

THREAD_ID = "019f0000-0000-7000-8000-000000000002"


def make_monitor(store: Store, command: str, *, timeout: int = 100) -> dict:
    return store.register(
        thread_id=THREAD_ID,
        cwd="/tmp",
        check_command=command,
        success_regex="^SUCCESS$",
        failure_regex="^FAILED$",
        interval_seconds=1,
        timeout_seconds=timeout,
        check_timeout_seconds=5,
        codex_home="/tmp/codex-home",
        codex_bin="/tmp/codex",
        now=time.time() - 2,
    )


def test_daemon_success_transition_without_model(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = make_monitor(store, "printf 'SUCCESS\\n'")
    result = DurableContinueDaemon(store=store).run_once(max_queue_workers=0)
    assert result["checked"] == 1
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == QUEUE_PENDING
    assert current["wake_reason"] == "success"


def test_daemon_no_match_returns_to_waiting(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = make_monitor(store, "printf 'RUNNING\\n'")
    DurableContinueDaemon(store=store).run_once(max_queue_workers=0)
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == WAITING


def test_nonzero_checker_does_not_wake_before_deadline(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = make_monitor(store, "printf 'network fault\\n'; exit 255")
    DurableContinueDaemon(store=store).run_once(max_queue_workers=0)
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == WAITING
    assert current["last_returncode"] == 255
