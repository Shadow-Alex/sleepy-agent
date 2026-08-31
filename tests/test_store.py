from pathlib import Path

from durable_continue.checker import CheckDecision, CheckObservation
from durable_continue.queue_delivery import QueueDeliveryResult
from durable_continue.store import (
    CANCELLED,
    CHECKING,
    QUEUE_PENDING,
    QUEUED,
    QUEUING,
    WAITING,
    Store,
)

THREAD_ID = "019f0000-0000-7000-8000-000000000001"


def obs(text: str = "RUNNING") -> CheckObservation:
    return CheckObservation(0, text, "", None, False)


def register(store: Store, *, now: float = 1000.0) -> dict:
    return store.register(
        thread_id=THREAD_ID,
        cwd="/tmp",
        check_command="echo RUNNING",
        success_regex="^SUCCESS$",
        failure_regex="^FAILED$",
        interval_seconds=10,
        timeout_seconds=100,
        check_timeout_seconds=5,
        codex_home="/tmp/codex-home",
        codex_bin="/tmp/codex",
        now=now,
    )


def move_to_queue_pending(store: Store, monitor: dict, *, now: float = 1010) -> dict:
    claim = store.claim_due_check(now=now)
    assert claim is not None
    assert store.complete_check(
        monitor["id"],
        claim["claim_token"],
        CheckDecision("success", obs("SUCCESS")),
        now=now,
    )
    current = store.get(monitor["id"])
    assert current is not None
    return current


def test_state_persists_across_store_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first = Store(path)
    monitor = register(first)
    loaded = Store(path).get(monitor["id"])
    assert loaded is not None
    assert loaded["state"] == WAITING
    assert loaded["thread_id"] == THREAD_ID
    assert loaded["codex_home"] == "/tmp/codex-home"


def test_check_claim_and_waiting_transition(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    claim = store.claim_due_check(now=1010)
    assert claim is not None and claim["state"] == CHECKING
    assert store.complete_check(
        monitor["id"], claim["claim_token"], CheckDecision(None, obs()), now=1010
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == WAITING
    assert current["poll_count"] == 1
    assert current["next_check_at"] == 1020


def test_terminal_check_moves_to_queue_pending(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    current = move_to_queue_pending(store, monitor)
    assert current["state"] == QUEUE_PENDING
    assert current["wake_reason"] == "success"


def test_cancel_wins_over_late_checker_result(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    claim = store.claim_due_check(now=1010)
    assert claim is not None
    assert store.cancel(monitor["id"], now=1011)
    changed = store.complete_check(
        monitor["id"],
        claim["claim_token"],
        CheckDecision("success", obs("SUCCESS")),
        now=1012,
    )
    assert changed is False
    assert store.get(monitor["id"])["state"] == CANCELLED


def test_stale_check_claim_recovers(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    assert store.claim_due_check(now=1010, claim_seconds=5) is not None
    assert store.recover_stale(now=1016) == 1
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == WAITING
    assert current["next_check_at"] == 1016


def test_queue_claim_and_receipt(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_queue_pending(store, monitor)
    claim = store.claim_due_queue(now=1010)
    assert claim is not None and claim["state"] == QUEUING
    result = QueueDeliveryResult("qs_123", 0, "queued", "")
    assert store.queue_succeeded(monitor["id"], claim["claim_token"], result, now=1011)
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == QUEUED
    assert current["queued_submission_id"] == "qs_123"


def test_queue_failure_stays_pending_for_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_queue_pending(store, monitor)
    claim = store.claim_due_queue(now=1010)
    assert claim is not None
    assert store.queue_retry(
        monitor["id"], claim["claim_token"], "temporary", retry_seconds=60, now=1011
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == QUEUE_PENDING
    assert current["next_queue_at"] == 1071
    assert current["queue_attempts"] == 1


def test_dead_queue_worker_recovers(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_queue_pending(store, monitor)
    claim = store.claim_due_queue(now=1010, claim_seconds=500)
    assert claim is not None
    assert store.set_queue_worker_pid(monitor["id"], claim["claim_token"], 999_999_999)
    assert store.recover_stale(now=1011) == 1
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == QUEUE_PENDING
    assert current["next_queue_at"] == 1011
