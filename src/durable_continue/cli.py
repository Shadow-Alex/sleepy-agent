from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from .checker import compile_regex
from .daemon import DurableContinueDaemon
from .delivery_evidence import DeliveryEvidenceError
from .dispatcher_state import (
    claim_dispatch,
    observe_dispatch,
    record_dispatch_failure,
)
from .durations import parse_duration
from .plugin_runtime import DESKTOP_BACKEND, active_dispatchers, dispatcher_supported
from .store import Store
from .util import (
    codex_home_from_environment,
    database_path,
    iso_utc,
    json_print,
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
    register.add_argument("--codex-home", help=argparse.SUPPRESS)

    status = sub.add_parser("status", help="show one monitor")
    status.add_argument("monitor_id")

    list_cmd = sub.add_parser("list", help="list monitors")
    list_cmd.add_argument(
        "--active", action="store_true", help="exclude terminal records"
    )

    cancel = sub.add_parser("cancel", help="cancel future checks and delivery attempts")
    cancel.add_argument("monitor_id")

    doctor = sub.add_parser(
        "doctor", help="inspect local state and Codex Desktop delivery support"
    )
    doctor.add_argument("--codex-home", help=argparse.SUPPRESS)

    daemon = sub.add_parser("daemon", help=argparse.SUPPRESS)
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--tick-seconds", type=int, default=15)

    claim = sub.add_parser("_dispatcher-claim", help=argparse.SUPPRESS)
    _add_dispatch_retry_arguments(claim)
    claim.add_argument("--claim-seconds", type=int, default=180)

    observe = sub.add_parser("_dispatcher-observe", help=argparse.SUPPRESS)
    observe.add_argument("monitor_id")
    observe.add_argument("--claim-token", required=True)
    observe.add_argument("--wait-seconds", type=float, default=0)
    _add_dispatch_retry_arguments(observe)

    fail = sub.add_parser("_dispatcher-fail", help=argparse.SUPPRESS)
    fail.add_argument("monitor_id")
    fail.add_argument("--claim-token", required=True)
    fail.add_argument("--error", required=True)
    fail.add_argument("--reason", default="native_pipe_error")
    fail.add_argument("--permanent", action="store_true")
    _add_dispatch_retry_arguments(fail)

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
        "next_delivery_at": iso_utc(float(record["next_delivery_at"]))
        if record.get("next_delivery_at")
        else None,
        "delivery_phase": _delivery_phase(record),
        "client_user_message_id": record.get("client_user_message_id"),
        "delivery_backend": record.get("delivery_backend"),
        "delivery_started_at": iso_utc(float(record["delivery_started_at"]))
        if record.get("delivery_started_at")
        else None,
        "delivery_rollout_path": record.get("delivery_rollout_path"),
        "started_turn_id": record.get("started_turn_id"),
        "started_at": iso_utc(float(record["started_at"]))
        if record.get("started_at")
        else None,
        "delivery_blocked_at": iso_utc(float(record["delivery_blocked_at"]))
        if record.get("delivery_blocked_at")
        else None,
        "delivery_blocked_reason": record.get("delivery_blocked_reason"),
        "last_error": record.get("last_error"),
    }


def _delivery_phase(record: dict[str, object]) -> str:
    if record.get("started_turn_id"):
        return "started"
    if record.get("delivery_blocked_at"):
        return "blocked"
    if record.get("state") == "CANCELLED":
        return "cancelled"
    if record.get("delivery_started_at"):
        return "submitting"
    if record.get("state") in {"DELIVERY_PENDING", "DELIVERING"}:
        return "pending"
    return "not_started"


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
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not args.check_command.strip():
            print("error: --check must not be empty", file=sys.stderr)
            return 2
        supported, detail = dispatcher_supported()
        if not supported:
            print(
                f"error: Codex Desktop delivery is unavailable: {detail}",
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
        )
        json_print(_public_summary(record))
        return 0

    if args.command == "status":
        record = store.get(args.monitor_id)
        if record is None:
            print(f"error: monitor not found: {args.monitor_id}", file=sys.stderr)
            return 1
        payload = store.public_record(record)
        payload["delivery_phase"] = _delivery_phase(record)
        json_print(payload)
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
        supported, delivery_detail = dispatcher_supported()
        dispatchers = active_dispatchers()
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
            "codex_home": codex_home,
            "delivery_backend": DESKTOP_BACKEND,
            "desktop_delivery_supported": supported,
            "desktop_delivery_detail": delivery_detail,
            "desktop_dispatcher_loaded": bool(dispatchers),
            "desktop_dispatchers": dispatchers,
            "CODEX_THREAD_ID_present": bool(thread_id),
            "CODEX_THREAD_ID_valid": thread_valid,
        }
        json_print(payload)
        return 0 if supported else 1

    if args.command == "daemon":
        daemon = DurableContinueDaemon(
            store=store,
            tick_seconds=args.tick_seconds,
        )
        if args.once:
            json_print(daemon.run_once())
            return 0
        daemon.run_forever()
        return 0

    if args.command == "_dispatcher-claim":
        claim = claim_dispatch(
            store,
            claim_seconds=args.claim_seconds,
            retry_seconds=args.retry_seconds,
            max_delivery_attempts=args.max_delivery_attempts,
            max_retry_seconds=args.max_retry_seconds,
        )
        json_print({"claim": claim.as_dict() if claim is not None else None})
        return 0

    if args.command == "_dispatcher-observe":
        try:
            payload = observe_dispatch(
                store,
                args.monitor_id,
                args.claim_token,
                wait_seconds=args.wait_seconds,
            )
        except DeliveryEvidenceError as exc:
            record_dispatch_failure(
                store,
                args.monitor_id,
                args.claim_token,
                error=f"{exc.stage}: {exc}",
                retryable=exc.retryable,
                reason=exc.category,
                retry_seconds=args.retry_seconds,
                max_delivery_attempts=args.max_delivery_attempts,
                max_retry_seconds=args.max_retry_seconds,
            )
            payload = {
                "observed": False,
                "stale": False,
                "error": f"{exc.stage}: {exc}",
            }
        json_print(payload)
        return 0

    if args.command == "_dispatcher-fail":
        changed = record_dispatch_failure(
            store,
            args.monitor_id,
            args.claim_token,
            error=args.error[:2_000],
            retryable=not args.permanent,
            reason=args.reason,
            retry_seconds=args.retry_seconds,
            max_delivery_attempts=args.max_delivery_attempts,
            max_retry_seconds=args.max_retry_seconds,
        )
        json_print({"recorded": changed})
        return 0 if changed else 1

    return 2


def _add_dispatch_retry_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--retry-seconds", type=int, default=60)
    parser.add_argument("--max-delivery-attempts", type=int, default=12)
    parser.add_argument("--max-retry-seconds", type=int, default=3600)


if __name__ == "__main__":
    raise SystemExit(main())
