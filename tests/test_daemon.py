import time
from pathlib import Path

from durable_continue.daemon import DurableContinueDaemon
from durable_continue.store import DELIVERY_PENDING, WAITING, Store

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
        now=time.time() - 2,
    )


def test_daemon_success_transition_without_model(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = make_monitor(store, "printf 'SUCCESS\\n'")
    result = DurableContinueDaemon(store=store).run_once()
    assert result["checked"] == 1
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERY_PENDING
    assert current["wake_reason"] == "success"


def test_daemon_no_match_returns_to_waiting(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = make_monitor(store, "printf 'RUNNING\\n'")
    DurableContinueDaemon(store=store).run_once()
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == WAITING


def test_nonzero_checker_does_not_wake_before_deadline(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = make_monitor(store, "printf 'network fault\\n'; exit 255")
    DurableContinueDaemon(store=store).run_once()
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == WAITING
    assert current["last_returncode"] == 255


def test_daemon_recovers_expired_delivery_lease_without_dispatching(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = make_monitor(store, "printf 'SUCCESS\\n'")
    daemon = DurableContinueDaemon(store=store)
    daemon.run_once()
    claim = store.claim_due_delivery(now=time.time(), claim_seconds=1)
    assert claim is not None
    with store.immediate() as conn:
        conn.execute(
            "UPDATE monitors SET claim_expires_at = ? WHERE id = ?",
            (time.time() - 1, monitor["id"]),
        )

    result = daemon.run_once(max_checks=0)
    assert result["recovered"] == 1
    assert store.get(monitor["id"])["state"] == DELIVERY_PENDING
