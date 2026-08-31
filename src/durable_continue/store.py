from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .checker import CheckDecision
from .queue_delivery import QueueDeliveryResult
from .util import append_jsonl, database_path, ensure_private_dir, iso_utc, now_ts

WAITING = "WAITING"
CHECKING = "CHECKING"
QUEUE_PENDING = "QUEUE_PENDING"
QUEUING = "QUEUING"
QUEUED = "QUEUED"
CANCELLED = "CANCELLED"
ACTIVE_STATES = (WAITING, CHECKING, QUEUE_PENDING, QUEUING)
ALL_STATES = ACTIVE_STATES + (QUEUED, CANCELLED)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS monitors (
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
    state TEXT NOT NULL CHECK (state IN ({",".join(repr(s) for s in ALL_STATES)})),
    wake_reason TEXT CHECK (wake_reason IS NULL OR wake_reason IN ('success','failure','timeout')),
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
CREATE INDEX IF NOT EXISTS idx_monitors_due_check
    ON monitors(state, next_check_at, deadline_at);
CREATE INDEX IF NOT EXISTS idx_monitors_due_queue
    ON monitors(state, next_queue_at);
PRAGMA user_version = 1;
"""

_EXPECTED_COLUMNS = {
    "id",
    "thread_id",
    "cwd",
    "check_command",
    "success_regex",
    "failure_regex",
    "interval_seconds",
    "check_timeout_seconds",
    "codex_home",
    "codex_bin",
    "created_at",
    "updated_at",
    "deadline_at",
    "next_check_at",
    "state",
    "wake_reason",
    "poll_count",
    "last_check_at",
    "last_returncode",
    "last_stdout_tail",
    "last_stderr_tail",
    "last_error",
    "next_queue_at",
    "queue_attempts",
    "queue_worker_pid",
    "last_queue_returncode",
    "last_queue_stdout_tail",
    "last_queue_stderr_tail",
    "queued_submission_id",
    "queued_at",
    "cancelled_at",
    "claim_token",
    "claim_expires_at",
}


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or database_path()).expanduser().absolute()
        ensure_private_dir(self.path.parent)
        self.logs_path = ensure_private_dir(self.path.parent / "logs")
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def init(self) -> None:
        with self.connection() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
        missing = _EXPECTED_COLUMNS - columns
        if missing:
            names = ", ".join(sorted(missing))
            raise RuntimeError(
                f"unsupported durable-continue database schema at {self.path}; "
                f"missing columns: {names}"
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def register(
        self,
        *,
        thread_id: str,
        cwd: str,
        check_command: str,
        success_regex: str,
        failure_regex: str | None,
        interval_seconds: int,
        timeout_seconds: int,
        check_timeout_seconds: int,
        codex_home: str,
        codex_bin: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = now if now is not None else now_ts()
        monitor_id = f"dc_{int(ts)}_{uuid.uuid4().hex[:8]}"
        deadline = ts + timeout_seconds
        next_check = min(ts + interval_seconds, deadline)
        with self.immediate() as conn:
            conn.execute(
                """
                INSERT INTO monitors (
                    id, thread_id, cwd, check_command, success_regex, failure_regex,
                    interval_seconds, check_timeout_seconds, codex_home, codex_bin,
                    created_at, updated_at, deadline_at, next_check_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    thread_id,
                    cwd,
                    check_command,
                    success_regex,
                    failure_regex,
                    interval_seconds,
                    check_timeout_seconds,
                    codex_home,
                    codex_bin,
                    ts,
                    ts,
                    deadline,
                    next_check,
                    WAITING,
                ),
            )
        record = self.get(monitor_id)
        assert record is not None
        return record

    def get(self, monitor_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
            ).fetchone()
        return _row_to_dict(row)

    def list(self, *, include_terminal: bool = True) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if include_terminal:
                rows = conn.execute(
                    "SELECT * FROM monitors ORDER BY created_at DESC"
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in ACTIVE_STATES)
                rows = conn.execute(
                    f"SELECT * FROM monitors WHERE state IN ({placeholders}) ORDER BY created_at DESC",
                    ACTIVE_STATES,
                ).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, monitor_id: str, *, now: float | None = None) -> bool:
        ts = now if now is not None else now_ts()
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self.immediate() as conn:
            cur = conn.execute(
                f"""
                UPDATE monitors
                SET state = ?, cancelled_at = ?, updated_at = ?, claim_token = NULL,
                    claim_expires_at = NULL, queue_worker_pid = NULL
                WHERE id = ? AND state IN ({placeholders})
                """,
                (CANCELLED, ts, ts, monitor_id, *ACTIVE_STATES),
            )
        return cur.rowcount == 1

    def recover_stale(self, *, now: float | None = None) -> int:
        ts = now if now is not None else now_ts()
        changed = 0
        with self.immediate() as conn:
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, next_check_at = ?, updated_at = ?, claim_token = NULL,
                    claim_expires_at = NULL,
                    last_error = COALESCE(last_error || '; ', '') || 'recovered stale checker claim'
                WHERE state = ? AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                """,
                (WAITING, ts, ts, CHECKING, ts),
            )
            changed += cur.rowcount

            rows = conn.execute(
                "SELECT id, queue_worker_pid, claim_expires_at FROM monitors WHERE state = ?",
                (QUEUING,),
            ).fetchall()
            for row in rows:
                pid = row["queue_worker_pid"]
                expired = (
                    row["claim_expires_at"] is not None
                    and row["claim_expires_at"] <= ts
                )
                dead = pid is not None and not _pid_alive(int(pid))
                if not (expired or dead):
                    continue
                conn.execute(
                    """
                    UPDATE monitors
                    SET state = ?, next_queue_at = ?, updated_at = ?, claim_token = NULL,
                        claim_expires_at = NULL, queue_worker_pid = NULL,
                        last_error = COALESCE(last_error || '; ', '') || 'recovered stale queue worker'
                    WHERE id = ? AND state = ?
                    """,
                    (QUEUE_PENDING, ts, ts, row["id"], QUEUING),
                )
                changed += 1
        return changed

    def claim_due_check(
        self,
        *,
        now: float | None = None,
        claim_seconds: int = 150,
    ) -> dict[str, Any] | None:
        ts = now if now is not None else now_ts()
        token = uuid.uuid4().hex
        with self.immediate() as conn:
            row = conn.execute(
                """
                SELECT * FROM monitors
                WHERE state = ? AND (next_check_at <= ? OR deadline_at <= ?)
                ORDER BY MIN(next_check_at, deadline_at), created_at
                LIMIT 1
                """,
                (WAITING, ts, ts),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, claim_token = ?, claim_expires_at = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (CHECKING, token, ts + claim_seconds, ts, row["id"], WAITING),
            )
            if cur.rowcount != 1:
                return None
        claimed = dict(row)
        claimed.update(
            state=CHECKING, claim_token=token, claim_expires_at=ts + claim_seconds
        )
        return claimed

    def complete_check(
        self,
        monitor_id: str,
        claim_token: str,
        decision: CheckDecision,
        *,
        now: float | None = None,
    ) -> bool:
        ts = now if now is not None else now_ts()
        observation = decision.observation
        with self.immediate() as conn:
            current = conn.execute(
                "SELECT state, interval_seconds, deadline_at FROM monitors WHERE id = ?",
                (monitor_id,),
            ).fetchone()
            if current is None or current["state"] != CHECKING:
                return False
            if decision.wake_reason is not None:
                state = QUEUE_PENDING
                next_check_at = ts
                next_queue_at = ts
            else:
                state = WAITING
                next_check_at = min(
                    ts + int(current["interval_seconds"]), float(current["deadline_at"])
                )
                next_queue_at = None
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, wake_reason = COALESCE(?, wake_reason),
                    poll_count = poll_count + 1, last_check_at = ?,
                    last_returncode = ?, last_stdout_tail = ?, last_stderr_tail = ?,
                    last_error = ?, next_check_at = ?, next_queue_at = ?,
                    updated_at = ?, claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (
                    state,
                    decision.wake_reason,
                    ts,
                    observation.returncode,
                    observation.stdout_tail,
                    observation.stderr_tail,
                    observation.error,
                    next_check_at,
                    next_queue_at,
                    ts,
                    monitor_id,
                    CHECKING,
                    claim_token,
                ),
            )
        changed = cur.rowcount == 1
        if changed:
            append_jsonl(
                self.logs_path / f"{monitor_id}.jsonl",
                {
                    "event": "check",
                    "at": iso_utc(ts),
                    "wake_reason": decision.wake_reason,
                    **asdict(observation),
                },
            )
        return changed

    def claim_due_queue(
        self,
        *,
        now: float | None = None,
        claim_seconds: int = 120,
    ) -> dict[str, Any] | None:
        ts = now if now is not None else now_ts()
        token = uuid.uuid4().hex
        with self.immediate() as conn:
            row = conn.execute(
                """
                SELECT * FROM monitors
                WHERE state = ? AND COALESCE(next_queue_at, 0) <= ?
                ORDER BY COALESCE(next_queue_at, 0), created_at
                LIMIT 1
                """,
                (QUEUE_PENDING, ts),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, claim_token = ?, claim_expires_at = ?,
                    queue_attempts = queue_attempts + 1, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (QUEUING, token, ts + claim_seconds, ts, row["id"], QUEUE_PENDING),
            )
            if cur.rowcount != 1:
                return None
        claimed = dict(row)
        claimed.update(
            state=QUEUING,
            claim_token=token,
            claim_expires_at=ts + claim_seconds,
            queue_attempts=int(claimed.get("queue_attempts") or 0) + 1,
        )
        return claimed

    def set_queue_worker_pid(self, monitor_id: str, claim_token: str, pid: int) -> bool:
        with self.immediate() as conn:
            cur = conn.execute(
                """
                UPDATE monitors SET queue_worker_pid = ?, updated_at = ?
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (pid, now_ts(), monitor_id, QUEUING, claim_token),
            )
        return cur.rowcount == 1

    def queue_succeeded(
        self,
        monitor_id: str,
        claim_token: str,
        result: QueueDeliveryResult,
        *,
        now: float | None = None,
    ) -> bool:
        ts = now if now is not None else now_ts()
        with self.immediate() as conn:
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, queued_submission_id = ?, queued_at = ?, updated_at = ?,
                    last_queue_returncode = ?, last_queue_stdout_tail = ?,
                    last_queue_stderr_tail = ?, last_error = NULL,
                    claim_token = NULL, claim_expires_at = NULL, queue_worker_pid = NULL
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (
                    QUEUED,
                    result.queued_submission_id,
                    ts,
                    ts,
                    result.returncode,
                    result.stdout_tail,
                    result.stderr_tail,
                    monitor_id,
                    QUEUING,
                    claim_token,
                ),
            )
        changed = cur.rowcount == 1
        if changed:
            append_jsonl(
                self.logs_path / f"{monitor_id}.jsonl",
                {
                    "event": "queued",
                    "at": iso_utc(ts),
                    "queued_submission_id": result.queued_submission_id,
                    "returncode": result.returncode,
                    "stdout_tail": result.stdout_tail,
                    "stderr_tail": result.stderr_tail,
                },
            )
        return changed

    def queue_retry(
        self,
        monitor_id: str,
        claim_token: str,
        error: str,
        *,
        returncode: int | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
        retry_seconds: int = 60,
        now: float | None = None,
    ) -> bool:
        ts = now if now is not None else now_ts()
        with self.immediate() as conn:
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, next_queue_at = ?, updated_at = ?, last_error = ?,
                    last_queue_returncode = ?, last_queue_stdout_tail = ?,
                    last_queue_stderr_tail = ?, claim_token = NULL,
                    claim_expires_at = NULL, queue_worker_pid = NULL
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (
                    QUEUE_PENDING,
                    ts + retry_seconds,
                    ts,
                    error,
                    returncode,
                    stdout_tail,
                    stderr_tail,
                    monitor_id,
                    QUEUING,
                    claim_token,
                ),
            )
        changed = cur.rowcount == 1
        if changed:
            append_jsonl(
                self.logs_path / f"{monitor_id}.jsonl",
                {
                    "event": "queue_retry",
                    "at": iso_utc(ts),
                    "error": error,
                    "returncode": returncode,
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                },
            )
        return changed

    def public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        for field in (
            "created_at",
            "updated_at",
            "deadline_at",
            "next_check_at",
            "last_check_at",
            "next_queue_at",
            "queued_at",
            "cancelled_at",
            "claim_expires_at",
        ):
            result[f"{field}_iso"] = iso_utc(result.get(field))
        return result


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
