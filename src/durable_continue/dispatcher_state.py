from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from .delivery_evidence import (
    VISIBLE_MESSAGE,
    DeliveryEvidenceError,
    RolloutAnchor,
    capture_rollout_anchor,
    context_turn_id,
    delivery_result,
    find_continue_after,
    validate_rollout_anchor,
)
from .store import DELIVERING, Store


@dataclass(frozen=True)
class DispatchClaim:
    monitor_id: str
    claim_token: str
    thread_id: str
    context_turn_id: str
    call_id: str
    prompt: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def claim_dispatch(
    store: Store,
    *,
    claim_seconds: int = 180,
    retry_seconds: int = 60,
    max_delivery_attempts: int = 12,
    max_retry_seconds: int = 3600,
) -> DispatchClaim | None:
    store.recover_stale()
    monitor = store.claim_due_delivery(claim_seconds=claim_seconds)
    if monitor is None:
        return None
    monitor_id = str(monitor["id"])
    claim_token = str(monitor["claim_token"])
    try:
        anchor, anchored = _prepare_anchor(store, monitor, claim_token)
        observed = find_continue_after(anchor)
        if observed is not None:
            store.delivery_succeeded(
                monitor_id,
                claim_token,
                delivery_result(
                    str(anchored["client_user_message_id"]),
                    anchor,
                    observed,
                    "recovered_before_native_send",
                ),
            )
            return None
        source_turn_id = context_turn_id(anchor)
    except DeliveryEvidenceError as exc:
        record_dispatch_failure(
            store,
            monitor_id,
            claim_token,
            error=f"{exc.stage}: {exc}",
            retryable=exc.retryable,
            reason=exc.category,
            retry_seconds=retry_seconds,
            max_delivery_attempts=max_delivery_attempts,
            max_retry_seconds=max_retry_seconds,
        )
        return None

    client_id = str(anchored["client_user_message_id"])
    return DispatchClaim(
        monitor_id=monitor_id,
        claim_token=claim_token,
        thread_id=str(anchored["thread_id"]),
        context_turn_id=source_turn_id,
        call_id=f"durable-continue-{client_id}",
        prompt=VISIBLE_MESSAGE,
    )


def observe_dispatch(
    store: Store,
    monitor_id: str,
    claim_token: str,
    *,
    wait_seconds: float = 0,
    poll_seconds: float = 0.25,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        monitor = store.get(monitor_id)
        if (
            monitor is None
            or monitor.get("state") != DELIVERING
            or monitor.get("claim_token") != claim_token
        ):
            return {"observed": False, "stale": True}
        anchor = _anchor_from_monitor(monitor)
        observed = find_continue_after(anchor)
        if observed is not None:
            result = delivery_result(
                str(monitor["client_user_message_id"]),
                anchor,
                observed,
                "verified_by_desktop_rollout",
            )
            changed = store.delivery_succeeded(
                monitor_id, claim_token, result
            )
            return {
                "observed": changed,
                "stale": not changed,
                "turn_id": observed.turn_id if changed else None,
            }
        if time.monotonic() >= deadline:
            return {"observed": False, "stale": False}
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def record_dispatch_failure(
    store: Store,
    monitor_id: str,
    claim_token: str,
    *,
    error: str,
    retryable: bool,
    reason: str,
    retry_seconds: int,
    max_delivery_attempts: int,
    max_retry_seconds: int,
) -> bool:
    monitor = store.get(monitor_id)
    if (
        monitor is None
        or monitor.get("state") != DELIVERING
        or monitor.get("claim_token") != claim_token
    ):
        return False
    attempt = int(monitor.get("delivery_attempts") or 1)
    exhausted = attempt >= max(1, max_delivery_attempts)
    if not retryable or exhausted:
        return store.delivery_blocked(
            monitor_id,
            claim_token,
            error,
            reason=reason if not retryable else "retry_exhausted",
        )
    return store.delivery_retry(
        monitor_id,
        claim_token,
        error,
        retry_seconds=retry_delay(
            retry_seconds,
            attempt,
            max_retry_seconds=max_retry_seconds,
        ),
    )


def retry_delay(
    base_retry_seconds: int,
    attempt: int,
    *,
    max_retry_seconds: int,
) -> int:
    base = max(1, base_retry_seconds)
    cap = max(base, max_retry_seconds)
    exponent = min(max(0, attempt - 1), 16)
    return min(cap, base * (2**exponent))


def _prepare_anchor(
    store: Store,
    monitor: dict[str, Any],
    claim_token: str,
) -> tuple[RolloutAnchor, dict[str, Any]]:
    path = monitor.get("delivery_rollout_path")
    offset = monitor.get("delivery_rollout_offset")
    if isinstance(path, str) and isinstance(offset, int):
        return (
            validate_rollout_anchor(
                str(monitor["codex_home"]),
                str(monitor["thread_id"]),
                path,
                offset,
            ),
            monitor,
        )
    captured = capture_rollout_anchor(
        str(monitor["codex_home"]), str(monitor["thread_id"])
    )
    anchored = store.record_delivery_anchor(
        str(monitor["id"]), claim_token, captured
    )
    if anchored is None:
        raise DeliveryEvidenceError(
            "delivery claim was cancelled before evidence anchoring",
            stage="persist_anchor",
            category="stale_claim",
        )
    return _anchor_from_monitor(anchored), anchored


def _anchor_from_monitor(monitor: dict[str, Any]) -> RolloutAnchor:
    path = monitor.get("delivery_rollout_path")
    offset = monitor.get("delivery_rollout_offset")
    if not isinstance(path, str) or not isinstance(offset, int):
        raise DeliveryEvidenceError(
            "monitor has no persisted rollout anchor",
            stage="validate_rollout",
            retryable=False,
            category="invalid_rollout_anchor",
        )
    return validate_rollout_anchor(
        str(monitor["codex_home"]),
        str(monitor["thread_id"]),
        path,
        offset,
    )
