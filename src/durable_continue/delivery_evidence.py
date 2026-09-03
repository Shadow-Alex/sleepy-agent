from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

VISIBLE_MESSAGE = "continue"
_MAX_ROLLOUT_SCAN_BYTES = 64 * 1024 * 1024
_MAX_ROLLOUT_LINE_BYTES = 16 * 1024 * 1024
_CONTEXT_TAIL_BYTES = 8 * 1024 * 1024


class DeliveryEvidenceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        retryable: bool = True,
        category: str = "transient",
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.category = category


@dataclass(frozen=True)
class RolloutAnchor:
    path: Path
    offset: int


@dataclass(frozen=True)
class ObservedContinue:
    turn_id: str
    source: str


@dataclass(frozen=True)
class DeliveryResult:
    client_user_message_id: str
    turn_id: str
    turn_status: str
    rollout_path: str
    rollout_offset: int
    returncode: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""


def capture_rollout_anchor(codex_home: str, thread_id: str) -> RolloutAnchor:
    _validated_thread_id(thread_id)
    home = Path(codex_home).expanduser().absolute()
    active = _matching_rollouts(home / "sessions", thread_id)
    if not active:
        archived = _matching_rollouts(home / "archived_sessions", thread_id)
        if archived:
            raise DeliveryEvidenceError(
                f"target task is archived: {thread_id}",
                stage="capture_rollout",
                retryable=False,
                category="thread_archived",
            )
        raise DeliveryEvidenceError(
            f"could not locate the target task rollout: {thread_id}",
            stage="capture_rollout",
            category="rollout_not_found",
        )
    path = max(active, key=_mtime_ns)
    try:
        offset = path.stat().st_size
    except OSError as exc:
        raise DeliveryEvidenceError(
            f"could not inspect target rollout: {exc}",
            stage="capture_rollout",
            category="rollout_unavailable",
        ) from exc
    return RolloutAnchor(path=path, offset=offset)


def validate_rollout_anchor(
    codex_home: str,
    thread_id: str,
    path: str,
    offset: int,
) -> RolloutAnchor:
    _validated_thread_id(thread_id)
    home = Path(codex_home).expanduser().absolute().resolve()
    candidate = Path(path).expanduser().absolute().resolve()
    if (
        not candidate.is_relative_to(home)
        or thread_id not in candidate.name
        or candidate.suffix != ".jsonl"
        or offset < 0
    ):
        raise DeliveryEvidenceError(
            "persisted rollout anchor is invalid",
            stage="validate_rollout",
            retryable=False,
            category="invalid_rollout_anchor",
        )
    if not candidate.is_file():
        archived = _matching_rollouts(home / "archived_sessions", thread_id)
        if archived:
            raise DeliveryEvidenceError(
                f"target task is archived: {thread_id}",
                stage="validate_rollout",
                retryable=False,
                category="thread_archived",
            )
        raise DeliveryEvidenceError(
            f"persisted rollout disappeared: {candidate}",
            stage="validate_rollout",
            category="rollout_unavailable",
        )
    return RolloutAnchor(path=candidate, offset=offset)


def find_continue_after(anchor: RolloutAnchor) -> ObservedContinue | None:
    try:
        size = anchor.path.stat().st_size
        if size < anchor.offset:
            raise DeliveryEvidenceError(
                "target rollout was truncated after delivery began",
                stage="verify_rollout",
                category="rollout_truncated",
            )
        with anchor.path.open("rb") as handle:
            handle.seek(anchor.offset)
            scanned = 0
            while scanned <= _MAX_ROLLOUT_SCAN_BYTES:
                line = handle.readline(_MAX_ROLLOUT_LINE_BYTES + 1)
                if not line:
                    return None
                scanned += len(line)
                if len(line) > _MAX_ROLLOUT_LINE_BYTES:
                    raise DeliveryEvidenceError(
                        "target rollout contains an oversized record",
                        stage="verify_rollout",
                        category="rollout_unreadable",
                    )
                observed = _observed_continue(_decode_record(line))
                if observed is not None:
                    return observed
    except DeliveryEvidenceError:
        raise
    except OSError as exc:
        raise DeliveryEvidenceError(
            f"could not read target rollout: {exc}",
            stage="verify_rollout",
            category="rollout_unavailable",
        ) from exc
    raise DeliveryEvidenceError(
        "target rollout verification window exceeded its safety bound",
        stage="verify_rollout",
        retryable=False,
        category="rollout_scan_limit",
    )


def context_turn_id(anchor: RolloutAnchor) -> str:
    try:
        with anchor.path.open("rb") as handle:
            start = max(0, anchor.offset - _CONTEXT_TAIL_BYTES)
            handle.seek(start)
            data = handle.read(anchor.offset - start)
    except OSError as exc:
        raise DeliveryEvidenceError(
            f"could not read delivery context from rollout: {exc}",
            stage="capture_context",
            category="rollout_unavailable",
        ) from exc
    if start:
        first_newline = data.find(b"\n")
        data = data[first_newline + 1 :] if first_newline >= 0 else b""
    latest: str | None = None
    for line in data.splitlines():
        record = _decode_record(line)
        turn_id = _record_turn_id(record)
        if turn_id:
            latest = turn_id
    if latest is None:
        raise DeliveryEvidenceError(
            "target rollout contains no usable turn context",
            stage="capture_context",
            category="turn_context_unavailable",
        )
    return latest


def delivery_result(
    client_user_message_id: str,
    anchor: RolloutAnchor,
    observed: ObservedContinue,
    status: str,
) -> DeliveryResult:
    return DeliveryResult(
        client_user_message_id=client_user_message_id,
        turn_id=observed.turn_id,
        turn_status=status,
        rollout_path=str(anchor.path),
        rollout_offset=anchor.offset,
        stdout_tail=f"verified exact continue via {observed.source}",
    )


def _matching_rollouts(root: Path, thread_id: str) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        return [
            path.absolute()
            for path in root.rglob(f"rollout-*{thread_id}*.jsonl")
            if path.is_file() and thread_id in path.name
        ]
    except OSError as exc:
        raise DeliveryEvidenceError(
            f"could not search Codex rollouts: {exc}",
            stage="capture_rollout",
            category="rollout_unavailable",
        ) from exc


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _decode_record(line: bytes) -> object:
    try:
        return json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _observed_continue(record: object) -> ObservedContinue | None:
    if not isinstance(record, dict):
        return None
    if record.get("type") == "response_item":
        payload = record.get("payload")
        if (
            isinstance(payload, dict)
            and payload.get("type") == "message"
            and payload.get("role") == "user"
            and _exact_continue_content(payload.get("content"))
        ):
            turn_id = _response_turn_id(payload)
            if turn_id:
                return ObservedContinue(turn_id, "response_item")

    if record.get("type") == "event_msg":
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "item_completed":
            return None
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "").replace("_", "").casefold()
        if item_type != "usermessage" or not _exact_continue_content(
            item.get("content")
        ):
            return None
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return ObservedContinue(turn_id, "event_msg")
    return None


def _record_turn_id(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    if record.get("type") == "response_item":
        payload = record.get("payload")
        if isinstance(payload, dict):
            turn_id = _response_turn_id(payload)
            if turn_id:
                return turn_id
    payload = record.get("payload")
    if isinstance(payload, dict):
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


def _response_turn_id(payload: dict[str, object]) -> str | None:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict):
        return None
    turn_id = metadata.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _exact_continue_content(content: object) -> bool:
    if not isinstance(content, list) or len(content) != 1:
        return False
    item = content[0]
    if not isinstance(item, dict) or item.get("type") not in {"input_text", "text"}:
        return False
    text = item.get("text")
    if not isinstance(text, str) or not text.startswith(VISIBLE_MESSAGE):
        return False
    trailing = text[len(VISIBLE_MESSAGE) :]
    return all(char in "\r\n" for char in trailing)


def _validated_thread_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise DeliveryEvidenceError(
            f"invalid target thread id: {value!r}",
            stage="validate_target",
            retryable=False,
            category="invalid_thread_id",
        ) from exc
    if str(parsed) != value.casefold():
        raise DeliveryEvidenceError(
            f"non-canonical target thread id: {value!r}",
            stage="validate_target",
            retryable=False,
            category="invalid_thread_id",
        )
    return value
