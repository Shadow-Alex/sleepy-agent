from __future__ import annotations

import json
from pathlib import Path

import pytest

from durable_continue.delivery_evidence import (
    VISIBLE_MESSAGE,
    DeliveryEvidenceError,
    RolloutAnchor,
    capture_rollout_anchor,
    context_turn_id,
    find_continue_after,
    validate_rollout_anchor,
)

THREAD_ID = "019f0000-0000-7000-8000-000000000004"
OTHER_THREAD_ID = "019f0000-0000-7000-8000-000000000005"


def user_continue(turn_id: str = "turn_123", text: str = "continue") -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn_id
                },
            },
        }
    )


def turn_record(turn_id: str) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
            },
        }
    )


def delegation_output(
    source_thread_id: str = THREAD_ID, text: str = "continue"
) -> str:
    return (
        "<codex_delegation>\n"
        f"  <source_thread_id>{source_thread_id}</source_thread_id>\n"
        f"  <input>{text}</input>\n"
        "</codex_delegation>"
    )


def delegated_continue_record(
    *,
    source_thread_id: str = THREAD_ID,
    text: str = "continue",
    name: str = "send_message_to_thread",
    namespace: str = "codex_app",
    turn_id: str = "turn_delegated",
) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "name": name,
                "namespace": namespace,
                "output": delegation_output(source_thread_id, text),
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn_id
                },
            },
        }
    )


def make_rollout(codex_home: Path, initial: str = "") -> Path:
    path = (
        codex_home
        / "sessions"
        / "2026"
        / "09"
        / "01"
        / f"rollout-2026-09-01T00-00-00-{THREAD_ID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(initial, encoding="utf-8")
    return path


def test_capture_validate_and_context_turn(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    initial = turn_record("019f0000-0000-7000-8000-000000000099") + "\n"
    path = make_rollout(home, initial)

    anchor = capture_rollout_anchor(str(home), THREAD_ID)

    assert anchor.path == path.absolute()
    assert anchor.offset == path.stat().st_size
    assert validate_rollout_anchor(
        str(home), THREAD_ID, str(anchor.path), anchor.offset
    ) == anchor
    assert context_turn_id(anchor) == "019f0000-0000-7000-8000-000000000099"


def test_archived_target_is_permanent(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    path = home / "archived_sessions" / f"rollout-x-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    with pytest.raises(DeliveryEvidenceError) as raised:
        capture_rollout_anchor(str(home), THREAD_ID)

    assert raised.value.retryable is False
    assert raised.value.category == "thread_archived"


def test_rollout_verification_matches_only_exact_user_continue(
    tmp_path: Path,
) -> None:
    path = tmp_path / f"rollout-{THREAD_ID}.jsonl"
    baseline = turn_record("019f0000-0000-7000-8000-000000000098") + "\n"
    path.write_text(baseline, encoding="utf-8")
    anchor = RolloutAnchor(path, len(baseline.encode()))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "continue",
                    },
                }
            )
            + "\n"
        )
        handle.write(user_continue(text="please continue") + "\n")
        handle.write(user_continue(turn_id="turn_exact", text="continue\n") + "\n")

    observed = find_continue_after(anchor)

    assert observed is not None
    assert observed.turn_id == "turn_exact"
    assert observed.source == "response_item"
    assert VISIBLE_MESSAGE == "continue"


def test_event_record_can_confirm_turn(tmp_path: Path) -> None:
    path = tmp_path / f"rollout-{THREAD_ID}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn_event",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "continue"}],
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    observed = find_continue_after(RolloutAnchor(path, 0))
    assert observed is not None and observed.turn_id == "turn_event"


def test_desktop_delegation_output_can_confirm_ambiguous_send(
    tmp_path: Path,
) -> None:
    path = tmp_path / f"rollout-{THREAD_ID}.jsonl"
    path.write_text(delegated_continue_record() + "\n", encoding="utf-8")

    observed = find_continue_after(RolloutAnchor(path, 0))

    assert observed is not None
    assert observed.turn_id == "turn_delegated"
    assert observed.source == "response_item_delegation"


@pytest.mark.parametrize(
    ("record",),
    [
        (delegated_continue_record(source_thread_id=OTHER_THREAD_ID),),
        (delegated_continue_record(text="continue please"),),
        (delegated_continue_record(name="other_tool"),),
        (delegated_continue_record(namespace="other_namespace"),),
    ],
)
def test_delegation_evidence_rejects_non_exact_tool_output(
    tmp_path: Path, record: str
) -> None:
    path = tmp_path / f"rollout-{THREAD_ID}.jsonl"
    path.write_text(record + "\n", encoding="utf-8")

    assert find_continue_after(RolloutAnchor(path, 0)) is None


def test_anchor_outside_codex_home_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / f"rollout-{THREAD_ID}.jsonl"
    outside.write_text("", encoding="utf-8")
    home = tmp_path / "codex"
    home.mkdir()

    with pytest.raises(DeliveryEvidenceError) as raised:
        validate_rollout_anchor(str(home), THREAD_ID, str(outside), 0)

    assert raised.value.retryable is False
    assert raised.value.category == "invalid_rollout_anchor"
