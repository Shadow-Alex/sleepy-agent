import sqlite3
from pathlib import Path

from durable_continue import store as store_module
from durable_continue.checker import CheckDecision, CheckObservation
from durable_continue.delivery_evidence import DeliveryResult, RolloutAnchor
from durable_continue.store import (
    CANCELLED,
    CHECKING,
    DELIVERED,
    DELIVERING,
    DELIVERY_PENDING,
    WAITING,
    Store,
)

THREAD_ID = "019f0000-0000-7000-8000-000000000001"

LEGACY_SCHEMA = """
CREATE TABLE monitors (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    check_command TEXT NOT NULL,
    success_regex TEXT NOT NULL,
    failure_regex TEXT,
    interval_seconds INTEGER NOT NULL,
    check_timeout_seconds INTEGER NOT NULL,
    codex_home TEXT NOT NULL,
    codex_bin TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    next_check_at REAL NOT NULL,
    state TEXT NOT NULL,
    wake_reason TEXT,
    poll_count INTEGER NOT NULL DEFAULT 0,
    last_check_at REAL,
    last_returncode INTEGER,
    last_stdout_tail TEXT,
    last_stderr_tail TEXT,
    last_error TEXT,
    next_queue_at REAL,
    queue_attempts INTEGER NOT NULL DEFAULT 0,
    queue_worker_pid INTEGER,
    last_queue_returncode INTEGER,
    last_queue_stdout_tail TEXT,
    last_queue_stderr_tail TEXT,
    client_user_message_id TEXT,
    queued_submission_id TEXT,
    queued_at REAL,
    started_turn_id TEXT,
    started_at REAL,
    delivery_blocked_at REAL,
    delivery_blocked_reason TEXT,
    delivery_backend TEXT,
    delivery_rollout_path TEXT,
    delivery_rollout_offset INTEGER,
    delivery_started_at REAL,
    cancelled_at REAL,
    claim_token TEXT,
    claim_expires_at REAL
);
PRAGMA user_version = 4;
"""

PUBLIC_V2_SCHEMA = """
CREATE TABLE monitors (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    check_command TEXT NOT NULL,
    success_regex TEXT NOT NULL,
    failure_regex TEXT,
    interval_seconds INTEGER NOT NULL,
    check_timeout_seconds INTEGER NOT NULL,
    codex_home TEXT NOT NULL,
    codex_bin TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    next_check_at REAL NOT NULL,
    state TEXT NOT NULL,
    wake_reason TEXT,
    poll_count INTEGER NOT NULL DEFAULT 0,
    last_check_at REAL,
    last_returncode INTEGER,
    last_stdout_tail TEXT,
    last_stderr_tail TEXT,
    last_error TEXT,
    next_queue_at REAL,
    queue_attempts INTEGER NOT NULL DEFAULT 0,
    queue_worker_pid INTEGER,
    last_queue_returncode INTEGER,
    last_queue_stdout_tail TEXT,
    last_queue_stderr_tail TEXT,
    queued_submission_id TEXT,
    queued_at REAL,
    cancelled_at REAL,
    claim_token TEXT,
    claim_expires_at REAL
);
PRAGMA user_version = 1;
"""


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
        now=now,
    )


def move_to_delivery_pending(store: Store, monitor: dict, *, now: float = 1010) -> dict:
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
    assert loaded["client_user_message_id"] == monitor["client_user_message_id"]


def test_v4_database_migrates_to_v5_without_runtime_compat_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            """
            INSERT INTO monitors (
                id, thread_id, cwd, check_command, success_regex,
                interval_seconds, check_timeout_seconds, codex_home, codex_bin,
                created_at, updated_at, deadline_at, next_check_at, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dc_legacy",
                THREAD_ID,
                "/tmp",
                "echo RUNNING",
                "SUCCESS",
                10,
                5,
                "/tmp/codex-home",
                "/tmp/codex",
                1,
                1,
                101,
                11,
                CHECKING,
            ),
        )

    migrated = Store(path).get("dc_legacy")
    assert migrated is not None
    assert migrated["state"] == WAITING
    assert migrated["client_user_message_id"]
    assert migrated["started_turn_id"] is None
    assert migrated["delivery_blocked_at"] is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
    assert columns == store_module._EXPECTED_COLUMNS
    assert "codex_bin" not in columns
    assert "queued_submission_id" not in columns


def test_public_v2_database_migrates_to_v5_and_preserves_active_monitor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "public-v2.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(PUBLIC_V2_SCHEMA)
        conn.execute(
            """
            INSERT INTO monitors (
                id, thread_id, cwd, check_command, success_regex,
                interval_seconds, check_timeout_seconds, codex_home, codex_bin,
                created_at, updated_at, deadline_at, next_check_at, state,
                queue_attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dc_public_v2",
                THREAD_ID,
                "/tmp",
                "echo RUNNING",
                "SUCCESS",
                10,
                5,
                "/tmp/codex-home",
                "/tmp/codex",
                1,
                1,
                101,
                11,
                WAITING,
                3,
            ),
        )

    migrated = Store(path).get("dc_public_v2")
    assert migrated is not None
    assert migrated["state"] == WAITING
    assert migrated["thread_id"] == THREAD_ID
    assert migrated["check_command"] == "echo RUNNING"
    assert migrated["delivery_attempts"] == 3
    assert migrated["client_user_message_id"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
    assert columns == store_module._EXPECTED_COLUMNS


def test_v4_delivery_records_migrate_without_replaying_ambiguous_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-delivery.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        for monitor_id, receipt in (
            ("dc_retry_safely", None),
            ("dc_ambiguous_receipt", "submission-old"),
        ):
            conn.execute(
                """
                INSERT INTO monitors (
                    id, thread_id, cwd, check_command, success_regex,
                    interval_seconds, check_timeout_seconds, codex_home, codex_bin,
                    created_at, updated_at, deadline_at, next_check_at, state,
                    next_queue_at, queued_submission_id, claim_token, claim_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    THREAD_ID,
                    "/tmp",
                    "echo SUCCESS",
                    "SUCCESS",
                    10,
                    5,
                    "/tmp/codex-home",
                    "/tmp/codex",
                    1,
                    1,
                    101,
                    11,
                    "QUEUING",
                    12,
                    receipt,
                    "old-claim",
                    999999,
                ),
            )

    store = Store(path)
    retry = store.get("dc_retry_safely")
    ambiguous = store.get("dc_ambiguous_receipt")

    assert retry is not None
    assert retry["state"] == DELIVERY_PENDING
    assert retry["claim_token"] is None
    assert retry["next_delivery_at"] > 1000
    assert ambiguous is not None
    assert ambiguous["state"] == CANCELLED
    assert ambiguous["delivery_blocked_reason"] == "legacy_delivery_receipt_removed"


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


def test_terminal_check_moves_to_delivery_pending(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    current = move_to_delivery_pending(store, monitor)
    assert current["state"] == DELIVERY_PENDING
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


def test_delivery_anchor_and_observed_turn_are_distinct(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_delivery_pending(store, monitor)
    claim = store.claim_due_delivery(now=1010)
    assert claim is not None and claim["state"] == DELIVERING
    anchor = RolloutAnchor(Path("/tmp/rollout.jsonl"), 123)
    assert store.record_delivery_anchor(
        monitor["id"], claim["claim_token"], anchor, now=1011
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERING
    assert current["delivery_rollout_path"] == "/tmp/rollout.jsonl"
    assert current["delivery_rollout_offset"] == 123
    assert current["delivery_backend"] == "codex_desktop_native_pipe"
    assert current["started_turn_id"] is None

    result = DeliveryResult(
        client_user_message_id=monitor["client_user_message_id"],
        turn_id="turn_123",
        turn_status="verified",
        rollout_path="/tmp/rollout.jsonl",
        rollout_offset=123,
        returncode=0,
        stdout_tail="observed",
        stderr_tail="",
    )
    assert store.delivery_succeeded(
        monitor["id"], claim["claim_token"], result, now=1012
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERED
    assert current["started_turn_id"] == "turn_123"
    assert current["started_at"] == 1012


def test_delivery_failure_stays_pending_for_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_delivery_pending(store, monitor)
    claim = store.claim_due_delivery(now=1010)
    assert claim is not None
    assert store.delivery_retry(
        monitor["id"], claim["claim_token"], "temporary", retry_seconds=60, now=1011
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERY_PENDING
    assert current["next_delivery_at"] == 1071
    assert current["delivery_attempts"] == 1


def test_stale_delivery_claim_recovers(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_delivery_pending(store, monitor)
    claim = store.claim_due_delivery(now=1010, claim_seconds=5)
    assert claim is not None
    assert store.recover_stale(now=1016) == 1
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERY_PENDING
    assert current["next_delivery_at"] == 1016


def test_unexpired_delivery_claim_is_not_recovered(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_delivery_pending(store, monitor)
    claim = store.claim_due_delivery(now=1010, claim_seconds=30)
    assert claim is not None
    assert store.recover_stale(now=1016) == 0
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERING
    assert store.claim_due_delivery(now=1016) is None


def test_permanent_delivery_failure_becomes_terminal_blocked(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monitor = register(store)
    move_to_delivery_pending(store, monitor)
    claim = store.claim_due_delivery(now=1010)
    assert claim is not None
    assert store.delivery_blocked(
        monitor["id"],
        claim["claim_token"],
        "session is archived",
        reason="thread_archived",
        now=1011,
    )
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == CANCELLED
    assert current["delivery_blocked_at"] == 1011
    assert current["delivery_blocked_reason"] == "thread_archived"
    assert current["next_delivery_at"] is None
    assert store.claim_due_delivery(now=9999) is None
