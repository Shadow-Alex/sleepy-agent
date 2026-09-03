import json
from pathlib import Path

from durable_continue.checker import CheckDecision, CheckObservation
from durable_continue.dispatcher_state import (
    claim_dispatch,
    observe_dispatch,
    record_dispatch_failure,
    retry_delay,
)
from durable_continue.store import CANCELLED, DELIVERED, DELIVERING, Store

THREAD_ID = "019f0000-0000-7000-8000-000000000005"
CONTEXT_TURN_ID = "019f0000-0000-7000-8000-000000000105"


def rollout(codex_home: Path) -> Path:
    path = (
        codex_home
        / "sessions"
        / "2026"
        / "09"
        / "01"
        / f"rollout-2026-09-01T00-00-00-{THREAD_ID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": CONTEXT_TURN_ID},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def pending_monitor(store: Store, codex_home: Path, *, now: float = 1000) -> dict:
    monitor = store.register(
        thread_id=THREAD_ID,
        cwd="/tmp",
        check_command="echo SUCCESS",
        success_regex="^SUCCESS$",
        failure_regex=None,
        interval_seconds=1,
        timeout_seconds=10,
        check_timeout_seconds=2,
        codex_home=str(codex_home),
        now=now,
    )
    check = store.claim_due_check(now=now + 1)
    assert check is not None
    observation = CheckObservation(0, "SUCCESS", "", None, False)
    assert store.complete_check(
        monitor["id"],
        check["claim_token"],
        CheckDecision("success", observation),
        now=now + 1,
    )
    return monitor


def append_continue(path: Path, turn_id: str = "turn_delivered") -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn_id
                        },
                    },
                }
            )
            + "\n"
        )


def append_delegated_continue(
    path: Path, turn_id: str = "turn_delegated"
) -> None:
    output = (
        "<codex_delegation>\n"
        f"  <source_thread_id>{THREAD_ID}</source_thread_id>\n"
        "  <input>continue</input>\n"
        "</codex_delegation>"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "name": "send_message_to_thread",
                        "namespace": "codex_app",
                        "output": output,
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn_id
                        },
                    },
                }
            )
            + "\n"
        )


def test_claim_anchors_before_native_send_and_observe_completes(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    codex_home = tmp_path / "codex"
    path = rollout(codex_home)
    monitor = pending_monitor(store, codex_home)

    claim = claim_dispatch(store)

    assert claim is not None
    assert claim.monitor_id == monitor["id"]
    assert claim.thread_id == THREAD_ID
    assert claim.context_turn_id == CONTEXT_TURN_ID
    assert claim.prompt == "continue"
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERING
    assert current["delivery_backend"] == "codex_desktop_native_pipe"
    assert current["delivery_rollout_path"] == str(path.absolute())

    append_continue(path)
    observed = observe_dispatch(store, claim.monitor_id, claim.claim_token)

    assert observed == {
        "observed": True,
        "stale": False,
        "turn_id": "turn_delivered",
    }
    final = store.get(monitor["id"])
    assert final is not None
    assert final["state"] == DELIVERED
    assert final["started_turn_id"] == "turn_delivered"


def test_retry_adopts_message_after_persisted_anchor_without_resend(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    codex_home = tmp_path / "codex"
    path = rollout(codex_home)
    monitor = pending_monitor(store, codex_home)
    first = claim_dispatch(store)
    assert first is not None
    assert record_dispatch_failure(
        store,
        first.monitor_id,
        first.claim_token,
        error="native response lost",
        retryable=True,
        reason="native_pipe_error",
        retry_seconds=60,
        max_delivery_attempts=12,
        max_retry_seconds=3600,
    )
    append_continue(path, "turn_recovered")
    with store.immediate() as conn:
        conn.execute(
            "UPDATE monitors SET next_delivery_at = 0 WHERE id = ?", (monitor["id"],)
        )

    assert claim_dispatch(store) is None
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERED
    assert current["started_turn_id"] == "turn_recovered"


def test_retry_adopts_desktop_delegation_after_ambiguous_native_error(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    codex_home = tmp_path / "codex"
    path = rollout(codex_home)
    monitor = pending_monitor(store, codex_home)
    first = claim_dispatch(store)
    assert first is not None
    assert record_dispatch_failure(
        store,
        first.monitor_id,
        first.claim_token,
        error="Codex app tool request failed",
        retryable=True,
        reason="native_pipe_error",
        retry_seconds=60,
        max_delivery_attempts=12,
        max_retry_seconds=3600,
    )
    append_delegated_continue(path)
    with store.immediate() as conn:
        conn.execute(
            "UPDATE monitors SET next_delivery_at = 0 WHERE id = ?", (monitor["id"],)
        )

    assert claim_dispatch(store) is None
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == DELIVERED
    assert current["started_turn_id"] == "turn_delegated"
    assert current["last_delivery_stdout_tail"] == (
        "verified exact continue via response_item_delegation"
    )


def test_only_one_desktop_dispatch_claim_exists_globally(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    first_home = tmp_path / "codex-a"
    second_home = tmp_path / "codex-b"
    rollout(first_home)
    rollout(second_home)
    pending_monitor(store, first_home, now=1000)
    pending_monitor(store, second_home, now=2000)

    first = claim_dispatch(store)
    assert first is not None
    assert claim_dispatch(store) is None
    active = [row for row in store.list() if row["state"] == DELIVERING]
    assert len(active) == 1


def test_archived_rollout_opens_permanent_circuit_breaker(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    codex_home = tmp_path / "codex"
    archived = codex_home / "archived_sessions" / f"rollout-x-{THREAD_ID}.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_text("", encoding="utf-8")
    monitor = pending_monitor(store, codex_home)

    assert claim_dispatch(store) is None
    current = store.get(monitor["id"])
    assert current is not None
    assert current["state"] == CANCELLED
    assert current["delivery_blocked_reason"] == "thread_archived"


def test_retry_budget_exhaustion_is_terminal(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    codex_home = tmp_path / "codex"
    rollout(codex_home)
    pending_monitor(store, codex_home)
    claim = claim_dispatch(store)
    assert claim is not None

    assert record_dispatch_failure(
        store,
        claim.monitor_id,
        claim.claim_token,
        error="temporary outage",
        retryable=True,
        reason="native_pipe_error",
        retry_seconds=60,
        max_delivery_attempts=1,
        max_retry_seconds=3600,
    )
    current = store.get(claim.monitor_id)
    assert current is not None
    assert current["state"] == CANCELLED
    assert current["delivery_blocked_reason"] == "retry_exhausted"


def test_retry_delay_is_exponential_and_bounded() -> None:
    assert retry_delay(60, 1, max_retry_seconds=3600) == 60
    assert retry_delay(60, 2, max_retry_seconds=3600) == 120
    assert retry_delay(60, 20, max_retry_seconds=3600) == 3600
