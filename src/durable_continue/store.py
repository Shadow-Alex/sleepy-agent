from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .checker import CheckDecision
from .delivery_evidence import DeliveryResult, RolloutAnchor
from .util import append_jsonl, database_path, ensure_private_dir, iso_utc, now_ts

WAITING = "WAITING"
CHECKING = "CHECKING"
DELIVERY_PENDING = "DELIVERY_PENDING"
DELIVERING = "DELIVERING"
DELIVERED = "DELIVERED"
CANCELLED = "CANCELLED"
ACTIVE_STATES = (WAITING, CHECKING, DELIVERY_PENDING, DELIVERING)
ALL_STATES = ACTIVE_STATES + (DELIVERED, CANCELLED)
SCHEMA_VERSION = 5

_MONITOR_COLUMNS = (
    "id",
    "thread_id",
    "cwd",
    "check_command",
    "success_regex",
    "failure_regex",
    "interval_seconds",
    "check_timeout_seconds",
    "codex_home",
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
    "next_delivery_at",
    "delivery_attempts",
    "last_delivery_returncode",
    "last_delivery_stdout_tail",
    "last_delivery_stderr_tail",
    "client_user_message_id",
    "started_turn_id",
    "started_at",
    "delivery_blocked_at",
    "delivery_blocked_reason",
    "delivery_backend",
    "delivery_rollout_path",
    "delivery_rollout_offset",
    "delivery_started_at",
    "cancelled_at",
    "claim_token",
    "claim_expires_at",
)
_EXPECTED_COLUMNS = set(_MONITOR_COLUMNS)
_LEGACY_REQUIRED_COLUMNS = {
    "id",
    "thread_id",
    "cwd",
    "check_command",
    "success_regex",
    "interval_seconds",
    "check_timeout_seconds",
    "codex_home",
    "created_at",
    "updated_at",
    "deadline_at",
    "next_check_at",
    "state",
}


def _create_table_sql(table: str) -> str:
    if table not in {"monitors", "monitors_v5"}:
        raise ValueError(f"unsupported table name: {table}")
    states = ",".join(repr(state) for state in ALL_STATES)
    return f"""
CREATE TABLE {table} (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    check_command TEXT NOT NULL,
    success_regex TEXT NOT NULL,
    failure_regex TEXT,
    interval_seconds INTEGER NOT NULL,
    check_timeout_seconds INTEGER NOT NULL,
    codex_home TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    next_check_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({states})),
    wake_reason TEXT CHECK (wake_reason IS NULL OR wake_reason IN ('success','failure','timeout')),
    poll_count INTEGER NOT NULL DEFAULT 0,
    last_check_at REAL,
    last_returncode INTEGER,
    last_stdout_tail TEXT,
    last_stderr_tail TEXT,
    last_error TEXT,
    next_delivery_at REAL,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_delivery_returncode INTEGER,
    last_delivery_stdout_tail TEXT,
    last_delivery_stderr_tail TEXT,
    client_user_message_id TEXT NOT NULL,
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
)
"""


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_monitors_due_check
        ON monitors(state, next_check_at, deadline_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_monitors_due_delivery
        ON monitors(state, next_delivery_at)
        """
    )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _legacy_value(
    row: dict[str, Any], name: str, default: Any = None
) -> Any:
    return row[name] if name in row else default


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
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'monitors'"
            ).fetchone()
            if exists is None:
                conn.execute(_create_table_sql("monitors"))
                _create_indexes(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(monitors)")
                }
                if columns == _EXPECTED_COLUMNS:
                    _create_indexes(conn)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                else:
                    missing_core = _LEGACY_REQUIRED_COLUMNS - columns
                    if missing_core:
                        names = ", ".join(sorted(missing_core))
                        raise RuntimeError(
                            f"unsupported durable-continue database schema at {self.path}; "
                            f"missing core columns: {names}"
                        )
                    self._migrate_legacy_schema(conn)

            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(monitors)")
            }
            if columns != _EXPECTED_COLUMNS:
                missing = ", ".join(sorted(_EXPECTED_COLUMNS - columns))
                extra = ", ".join(sorted(columns - _EXPECTED_COLUMNS))
                raise RuntimeError(
                    f"unsupported durable-continue database schema at {self.path}; "
                    f"missing columns: {missing or 'none'}; extra columns: {extra or 'none'}"
                )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        now = now_ts()
        conn.execute("BEGIN IMMEDIATE")
        try:
            legacy_rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM monitors ORDER BY created_at")
            ]
            temporary = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'monitors_v5'"
            ).fetchone()
            if temporary is not None:
                raise RuntimeError(
                    "cannot migrate durable-continue database while monitors_v5 exists"
                )
            conn.execute(_create_table_sql("monitors_v5"))
            placeholders = ",".join("?" for _ in _MONITOR_COLUMNS)
            columns = ",".join(_MONITOR_COLUMNS)
            for legacy in legacy_rows:
                migrated = self._migrated_record(legacy, now)
                conn.execute(
                    f"INSERT INTO monitors_v5 ({columns}) VALUES ({placeholders})",
                    tuple(migrated[name] for name in _MONITOR_COLUMNS),
                )
            conn.execute("DROP TABLE monitors")
            conn.execute("ALTER TABLE monitors_v5 RENAME TO monitors")
            _create_indexes(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def _migrated_record(
        self, legacy: dict[str, Any], now: float
    ) -> dict[str, Any]:
        legacy_state = str(_legacy_value(legacy, "state", WAITING))
        started_turn_id = _legacy_value(legacy, "started_turn_id")
        old_receipt = _legacy_value(legacy, "queued_submission_id")
        blocked_reason = _legacy_value(legacy, "delivery_blocked_reason")
        blocked_at = _legacy_value(legacy, "delivery_blocked_at")
        cancelled_at = _legacy_value(legacy, "cancelled_at")

        if legacy_state == CANCELLED:
            state = CANCELLED
        elif started_turn_id:
            state = DELIVERED
        elif old_receipt or legacy_state == "QUEUED":
            state = CANCELLED
            blocked_reason = blocked_reason or "legacy_delivery_receipt_removed"
            blocked_at = blocked_at or now
            cancelled_at = cancelled_at or now
        elif legacy_state in {"QUEUE_PENDING", "QUEUING", DELIVERY_PENDING, DELIVERING}:
            state = DELIVERY_PENDING
        elif legacy_state in {WAITING, CHECKING}:
            state = WAITING
        elif legacy_state == DELIVERED:
            state = DELIVERED
        else:
            state = CANCELLED
            blocked_reason = blocked_reason or "unsupported_legacy_state"
            blocked_at = blocked_at or now
            cancelled_at = cancelled_at or now

        next_check_at = float(_legacy_value(legacy, "next_check_at", now))
        if legacy_state == CHECKING:
            next_check_at = now
        next_delivery_at = None
        if state == DELIVERY_PENDING:
            if legacy_state in {"QUEUING", DELIVERING}:
                candidate = now
            else:
                candidate = _legacy_value(
                    legacy,
                    "next_delivery_at",
                    _legacy_value(legacy, "next_queue_at", now),
                )
            next_delivery_at = now if candidate is None else float(candidate)

        monitor_id = str(legacy["id"])
        return {
            "id": monitor_id,
            "thread_id": str(legacy["thread_id"]),
            "cwd": str(legacy["cwd"]),
            "check_command": str(legacy["check_command"]),
            "success_regex": str(legacy["success_regex"]),
            "failure_regex": _legacy_value(legacy, "failure_regex"),
            "interval_seconds": int(legacy["interval_seconds"]),
            "check_timeout_seconds": int(legacy["check_timeout_seconds"]),
            "codex_home": str(legacy["codex_home"]),
            "created_at": float(legacy["created_at"]),
            "updated_at": now,
            "deadline_at": float(legacy["deadline_at"]),
            "next_check_at": next_check_at,
            "state": state,
            "wake_reason": _legacy_value(legacy, "wake_reason"),
            "poll_count": int(_legacy_value(legacy, "poll_count", 0) or 0),
            "last_check_at": _legacy_value(legacy, "last_check_at"),
            "last_returncode": _legacy_value(legacy, "last_returncode"),
            "last_stdout_tail": _legacy_value(legacy, "last_stdout_tail"),
            "last_stderr_tail": _legacy_value(legacy, "last_stderr_tail"),
            "last_error": _legacy_value(legacy, "last_error"),
            "next_delivery_at": next_delivery_at,
            "delivery_attempts": int(
                _legacy_value(
                    legacy,
                    "delivery_attempts",
                    _legacy_value(legacy, "queue_attempts", 0),
                )
                or 0
            ),
            "last_delivery_returncode": _legacy_value(
                legacy,
                "last_delivery_returncode",
                _legacy_value(legacy, "last_queue_returncode"),
            ),
            "last_delivery_stdout_tail": _legacy_value(
                legacy,
                "last_delivery_stdout_tail",
                _legacy_value(legacy, "last_queue_stdout_tail"),
            ),
            "last_delivery_stderr_tail": _legacy_value(
                legacy,
                "last_delivery_stderr_tail",
                _legacy_value(legacy, "last_queue_stderr_tail"),
            ),
            "client_user_message_id": _legacy_value(
                legacy, "client_user_message_id"
            )
            or _client_message_id(monitor_id),
            "started_turn_id": started_turn_id,
            "started_at": (
                _legacy_value(legacy, "started_at")
                or _legacy_value(legacy, "queued_at")
            )
            if state == DELIVERED
            else None,
            "delivery_blocked_at": blocked_at,
            "delivery_blocked_reason": blocked_reason,
            "delivery_backend": _legacy_value(legacy, "delivery_backend"),
            "delivery_rollout_path": _legacy_value(
                legacy, "delivery_rollout_path"
            ),
            "delivery_rollout_offset": _legacy_value(
                legacy, "delivery_rollout_offset"
            ),
            "delivery_started_at": _legacy_value(legacy, "delivery_started_at"),
            "cancelled_at": cancelled_at,
            "claim_token": None,
            "claim_expires_at": None,
        }

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
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = now if now is not None else now_ts()
        monitor_id = f"dc_{int(ts)}_{uuid.uuid4().hex[:8]}"
        client_user_message_id = _client_message_id(monitor_id)
        deadline = ts + timeout_seconds
        next_check = min(ts + interval_seconds, deadline)
        with self.immediate() as conn:
            conn.execute(
                """
                INSERT INTO monitors (
                    id, thread_id, cwd, check_command, success_regex, failure_regex,
                    interval_seconds, check_timeout_seconds, codex_home,
                    client_user_message_id, created_at, updated_at, deadline_at,
                    next_check_at, state
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
                    client_user_message_id,
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
                    claim_expires_at = NULL, next_delivery_at = NULL
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
                WHERE state = ?
                  AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                """,
                (WAITING, ts, ts, CHECKING, ts),
            )
            changed += cur.rowcount
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, next_delivery_at = ?, updated_at = ?,
                    claim_token = NULL, claim_expires_at = NULL,
                    last_error = COALESCE(last_error || '; ', '') || 'recovered stale delivery claim'
                WHERE state = ?
                  AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                """,
                (DELIVERY_PENDING, ts, ts, DELIVERING, ts),
            )
            changed += cur.rowcount
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
                state = DELIVERY_PENDING
                next_check_at = ts
                next_delivery_at = ts
            else:
                state = WAITING
                next_check_at = min(
                    ts + int(current["interval_seconds"]), float(current["deadline_at"])
                )
                next_delivery_at = None
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, wake_reason = COALESCE(?, wake_reason),
                    poll_count = poll_count + 1, last_check_at = ?,
                    last_returncode = ?, last_stdout_tail = ?, last_stderr_tail = ?,
                    last_error = ?, next_check_at = ?, next_delivery_at = ?,
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
                    next_delivery_at,
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

    def claim_due_delivery(
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
                WHERE state = ? AND COALESCE(next_delivery_at, 0) <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM monitors AS active_delivery
                    WHERE active_delivery.state = ?
                  )
                ORDER BY COALESCE(next_delivery_at, 0), created_at
                LIMIT 1
                """,
                (DELIVERY_PENDING, ts, DELIVERING),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, claim_token = ?, claim_expires_at = ?,
                    delivery_attempts = delivery_attempts + 1, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    DELIVERING,
                    token,
                    ts + claim_seconds,
                    ts,
                    row["id"],
                    DELIVERY_PENDING,
                ),
            )
            if cur.rowcount != 1:
                return None
        claimed = dict(row)
        claimed.update(
            state=DELIVERING,
            claim_token=token,
            claim_expires_at=ts + claim_seconds,
            delivery_attempts=int(claimed.get("delivery_attempts") or 0) + 1,
        )
        return claimed

    def delivery_succeeded(
        self,
        monitor_id: str,
        claim_token: str,
        result: DeliveryResult,
        *,
        now: float | None = None,
    ) -> bool:
        ts = now if now is not None else now_ts()
        with self.immediate() as conn:
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, client_user_message_id = ?,
                    started_turn_id = ?, started_at = ?, updated_at = ?,
                    last_delivery_returncode = ?, last_delivery_stdout_tail = ?,
                    last_delivery_stderr_tail = ?, last_error = NULL,
                    delivery_backend = 'codex_desktop_native_pipe',
                    delivery_rollout_path = ?, delivery_rollout_offset = ?,
                    next_delivery_at = NULL,
                    claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (
                    DELIVERED,
                    result.client_user_message_id,
                    result.turn_id,
                    ts,
                    ts,
                    result.returncode,
                    result.stdout_tail,
                    result.stderr_tail,
                    result.rollout_path,
                    result.rollout_offset,
                    monitor_id,
                    DELIVERING,
                    claim_token,
                ),
            )
        changed = cur.rowcount == 1
        if changed:
            append_jsonl(
                self.logs_path / f"{monitor_id}.jsonl",
                {
                    "event": "desktop_turn_started",
                    "at": iso_utc(ts),
                    "client_user_message_id": result.client_user_message_id,
                    "turn_id": result.turn_id,
                    "turn_status": result.turn_status,
                    "rollout_path": result.rollout_path,
                    "rollout_offset": result.rollout_offset,
                    "returncode": result.returncode,
                    "stdout_tail": result.stdout_tail,
                    "stderr_tail": result.stderr_tail,
                },
            )
        return changed

    def record_delivery_anchor(
        self,
        monitor_id: str,
        claim_token: str,
        anchor: RolloutAnchor,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        ts = now if now is not None else now_ts()
        with self.immediate() as conn:
            cur = conn.execute(
                """
                UPDATE monitors
                SET delivery_backend = 'codex_desktop_native_pipe',
                    delivery_rollout_path = COALESCE(delivery_rollout_path, ?),
                    delivery_rollout_offset = COALESCE(delivery_rollout_offset, ?),
                    delivery_started_at = COALESCE(delivery_started_at, ?),
                    updated_at = ?, last_error = NULL
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (
                    str(anchor.path),
                    anchor.offset,
                    ts,
                    ts,
                    monitor_id,
                    DELIVERING,
                    claim_token,
                ),
            )
            row = (
                conn.execute(
                    "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
                ).fetchone()
                if cur.rowcount == 1
                else None
            )
        if row is not None:
            append_jsonl(
                self.logs_path / f"{monitor_id}.jsonl",
                {
                    "event": "desktop_delivery_anchored",
                    "at": iso_utc(ts),
                    "rollout_path": row["delivery_rollout_path"],
                    "rollout_offset": row["delivery_rollout_offset"],
                },
            )
        return _row_to_dict(row)

    def delivery_retry(
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
                SET state = ?, next_delivery_at = ?, updated_at = ?, last_error = ?,
                    last_delivery_returncode = ?, last_delivery_stdout_tail = ?,
                    last_delivery_stderr_tail = ?, claim_token = NULL,
                    claim_expires_at = NULL
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (
                    DELIVERY_PENDING,
                    ts + retry_seconds,
                    ts,
                    error,
                    returncode,
                    stdout_tail,
                    stderr_tail,
                    monitor_id,
                    DELIVERING,
                    claim_token,
                ),
            )
        changed = cur.rowcount == 1
        if changed:
            append_jsonl(
                self.logs_path / f"{monitor_id}.jsonl",
                {
                    "event": "delivery_retry",
                    "at": iso_utc(ts),
                    "error": error,
                    "returncode": returncode,
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                },
            )
        return changed

    def delivery_blocked(
        self,
        monitor_id: str,
        claim_token: str,
        error: str,
        *,
        reason: str,
        returncode: int | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
        now: float | None = None,
    ) -> bool:
        ts = now if now is not None else now_ts()
        with self.immediate() as conn:
            cur = conn.execute(
                """
                UPDATE monitors
                SET state = ?, delivery_blocked_at = ?,
                    delivery_blocked_reason = ?, next_delivery_at = NULL,
                    updated_at = ?, last_error = ?, last_delivery_returncode = ?,
                    last_delivery_stdout_tail = ?, last_delivery_stderr_tail = ?,
                    cancelled_at = ?, claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND state = ? AND claim_token = ?
                """,
                (
                    CANCELLED,
                    ts,
                    reason,
                    ts,
                    error,
                    returncode,
                    stdout_tail,
                    stderr_tail,
                    ts,
                    monitor_id,
                    DELIVERING,
                    claim_token,
                ),
            )
        changed = cur.rowcount == 1
        if changed:
            append_jsonl(
                self.logs_path / f"{monitor_id}.jsonl",
                {
                    "event": "delivery_blocked",
                    "at": iso_utc(ts),
                    "reason": reason,
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
            "next_delivery_at",
            "started_at",
            "delivery_blocked_at",
            "delivery_started_at",
            "cancelled_at",
            "claim_expires_at",
        ):
            result[f"{field}_iso"] = iso_utc(result.get(field))
        return result


def _client_message_id(monitor_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"durable-continue:{monitor_id}"))
