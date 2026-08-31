from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from .checker import compile_regex
from .daemon import DurableContinueDaemon
from .durations import parse_duration
from .queue_delivery import queue_supported, resolve_codex_bin
from .queue_worker import run_queue_worker
from .store import Store
from .util import (
    codex_home_from_environment,
    database_path,
    iso_utc,
    json_print,
    tail_text,
)


def _valid_thread_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(
            "CODEX_THREAD_ID is unavailable; refusing to register an unbound wait"
        )
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"invalid CODEX_THREAD_ID: {value!r}") from exc
    return value


def _absolute_codex_home(value: str | None) -> str:
    path = (
        Path(value).expanduser().absolute() if value else codex_home_from_environment()
    )
    return str(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="durable-continue")
    parser.add_argument("--db", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser(
        "register", help="register a durable wait for the current Codex task"
    )
    register.add_argument("--check", required=True, dest="check_command")
    register.add_argument("--success", required=True, dest="success_regex")
    register.add_argument("--failure", dest="failure_regex")
    register.add_argument("--timeout", required=True)
    register.add_argument("--interval", default="10m")
    register.add_argument("--check-timeout", default="60s", help=argparse.SUPPRESS)
    register.add_argument("--thread-id", help=argparse.SUPPRESS)
    register.add_argument("--codex-bin", help=argparse.SUPPRESS)
    register.add_argument("--codex-home", help=argparse.SUPPRESS)

    status = sub.add_parser("status", help="show one monitor")
    status.add_argument("monitor_id")

    list_cmd = sub.add_parser("list", help="list monitors")
    list_cmd.add_argument(
        "--active", action="store_true", help="exclude queued and cancelled records"
    )

    cancel = sub.add_parser("cancel", help="cancel future checks and queue attempts")
    cancel.add_argument("monitor_id")

    doctor = sub.add_parser(
        "doctor", help="inspect local state and Codex queue support"
    )
    doctor.add_argument("--codex-bin", help=argparse.SUPPRESS)
    doctor.add_argument("--codex-home", help=argparse.SUPPRESS)

    daemon = sub.add_parser("daemon", help=argparse.SUPPRESS)
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--tick-seconds", type=int, default=15)
    daemon.add_argument("--queue-retry-seconds", type=int, default=60)
    daemon.add_argument("--delivery-timeout", type=int, default=60)

    worker = sub.add_parser("_queue-worker", help=argparse.SUPPRESS)
    worker.add_argument("monitor_id")
    worker.add_argument("--claim-token", required=True)
    worker.add_argument("--retry-seconds", type=int, default=60)
    worker.add_argument("--delivery-timeout", type=int, default=60)
    worker.add_argument("--db", type=Path, default=None)

    return parser


def _public_summary(record: dict[str, object]) -> dict[str, object]:
    return {
        "id": record["id"],
        "state": record["state"],
        "thread_id": record["thread_id"],
        "wake_reason": record.get("wake_reason"),
        "poll_count": record.get("poll_count"),
        "next_check_at": iso_utc(float(record["next_check_at"]))
        if record.get("next_check_at")
        else None,
        "deadline_at": iso_utc(float(record["deadline_at"])),
        "next_queue_at": iso_utc(float(record["next_queue_at"]))
        if record.get("next_queue_at")
        else None,
        "queued_submission_id": record.get("queued_submission_id"),
        "queued_at": iso_utc(float(record["queued_at"]))
        if record.get("queued_at")
        else None,
        "last_error": record.get("last_error"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = args.db if getattr(args, "db", None) is not None else database_path()
    try:
        store = Store(db)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: could not open durable state: {exc}", file=sys.stderr)
        return 1

    if args.command == "register":
        try:
            thread_id = _valid_thread_id(
                args.thread_id or os.environ.get("CODEX_THREAD_ID", "")
            )
            timeout_seconds = parse_duration(args.timeout)
            interval_seconds = parse_duration(args.interval)
            check_timeout_seconds = parse_duration(args.check_timeout)
            compile_regex(args.success_regex, required=True)
            compile_regex(args.failure_regex)
            codex_home = _absolute_codex_home(args.codex_home)
            codex_bin = resolve_codex_bin(args.codex_bin)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not args.check_command.strip():
            print("error: --check must not be empty", file=sys.stderr)
            return 2
        supported, detail = queue_supported(codex_bin, codex_home=codex_home)
        if not supported:
            print(
                f"error: {codex_bin} does not provide a usable `codex queue` command: {detail}",
                file=sys.stderr,
            )
            return 2
        record = store.register(
            thread_id=thread_id,
            cwd=os.getcwd(),
            check_command=args.check_command,
            success_regex=args.success_regex,
            failure_regex=args.failure_regex,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            check_timeout_seconds=check_timeout_seconds,
            codex_home=codex_home,
            codex_bin=codex_bin,
        )
        json_print(_public_summary(record))
        return 0

    if args.command == "status":
        record = store.get(args.monitor_id)
        if record is None:
            print(f"error: monitor not found: {args.monitor_id}", file=sys.stderr)
            return 1
        json_print(store.public_record(record))
        return 0

    if args.command == "list":
        json_print(
            [
                _public_summary(item)
                for item in store.list(include_terminal=not args.active)
            ]
        )
        return 0

    if args.command == "cancel":
        changed = store.cancel(args.monitor_id)
        record = store.get(args.monitor_id)
        if record is None:
            print(f"error: monitor not found: {args.monitor_id}", file=sys.stderr)
            return 1
        json_print({"cancelled": changed, **_public_summary(record)})
        return 0 if changed else 1

    if args.command == "doctor":
        codex_home = _absolute_codex_home(args.codex_home)
        try:
            codex_bin = resolve_codex_bin(args.codex_bin)
            supported, queue_detail = queue_supported(codex_bin, codex_home=codex_home)
            version = _codex_version(codex_bin, codex_home)
        except ValueError as exc:
            codex_bin = None
            supported = False
            queue_detail = str(exc)
            version = None
        thread_id = os.environ.get("CODEX_THREAD_ID")
        thread_valid = False
        if thread_id:
            try:
                _valid_thread_id(thread_id)
                thread_valid = True
            except ValueError:
                pass
        payload = {
            "database": str(store.path),
            "database_exists": store.path.exists(),
            "codex_bin": codex_bin,
            "codex_home": codex_home,
            "codex_version": version,
            "codex_queue_supported": supported,
            "codex_queue_detail": queue_detail,
            "CODEX_THREAD_ID_present": bool(thread_id),
            "CODEX_THREAD_ID_valid": thread_valid,
        }
        json_print(payload)
        return 0 if supported else 1

    if args.command == "daemon":
        daemon = DurableContinueDaemon(
            store=store,
            tick_seconds=args.tick_seconds,
            queue_retry_seconds=args.queue_retry_seconds,
            delivery_timeout_seconds=args.delivery_timeout,
        )
        if args.once:
            json_print(daemon.run_once())
            return 0
        daemon.run_forever()
        return 0

    if args.command == "_queue-worker":
        return run_queue_worker(
            args.monitor_id,
            args.claim_token,
            db_path=args.db,
            retry_seconds=args.retry_seconds,
            delivery_timeout_seconds=args.delivery_timeout,
        )

    return 2


def _codex_version(codex_bin: str, codex_home: str) -> str | None:
    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    try:
        proc = subprocess.run(
            [codex_bin, "--version"],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return tail_text(proc.stdout.strip(), 1_000)


if __name__ == "__main__":
    raise SystemExit(main())
