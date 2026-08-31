from __future__ import annotations

from pathlib import Path

from .queue_delivery import QueueDeliveryError, deliver_continue
from .store import QUEUING, Store


def run_queue_worker(
    monitor_id: str,
    claim_token: str,
    *,
    db_path: Path | None = None,
    retry_seconds: int = 60,
    delivery_timeout_seconds: int = 60,
) -> int:
    store = Store(db_path)
    monitor = store.get(monitor_id)
    if monitor is None:
        return 2
    if monitor.get("state") != QUEUING or monitor.get("claim_token") != claim_token:
        return 0

    try:
        result = deliver_continue(
            codex_bin=str(monitor["codex_bin"]),
            codex_home=str(monitor["codex_home"]),
            thread_id=str(monitor["thread_id"]),
            timeout_seconds=delivery_timeout_seconds,
        )
    except QueueDeliveryError as exc:
        store.queue_retry(
            monitor_id,
            claim_token,
            str(exc),
            returncode=exc.returncode,
            stdout_tail=exc.stdout_tail,
            stderr_tail=exc.stderr_tail,
            retry_seconds=retry_seconds,
        )
        return 75
    except Exception as exc:  # noqa: BLE001 - keep the durable delivery queue alive
        store.queue_retry(
            monitor_id,
            claim_token,
            f"unexpected queue worker error: {type(exc).__name__}: {exc}",
            retry_seconds=retry_seconds,
        )
        return 70

    if not store.queue_succeeded(monitor_id, claim_token, result):
        # Cancellation or stale ownership won the race. Never force the state.
        return 0
    return 0
