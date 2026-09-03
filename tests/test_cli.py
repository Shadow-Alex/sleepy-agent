import json
from pathlib import Path

from durable_continue import cli

THREAD_ID = "019f0000-0000-7000-8000-000000000003"


def test_register_fails_without_thread_id(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    code = cli.main(
        [
            "--db",
            str(tmp_path / "state.sqlite3"),
            "register",
            "--check",
            "echo RUNNING",
            "--success",
            "SUCCESS",
            "--timeout",
            "1h",
        ]
    )
    assert code == 2
    assert "CODEX_THREAD_ID" in capsys.readouterr().err


def test_register_captures_runtime(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli, "dispatcher_supported", lambda: (True, "supported")
    )
    code = cli.main(
        [
            "--db",
            str(tmp_path / "state.sqlite3"),
            "register",
            "--thread-id",
            THREAD_ID,
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--check",
            "echo RUNNING",
            "--success",
            "SUCCESS",
            "--timeout",
            "1h",
            "--interval",
            "10m",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["thread_id"] == THREAD_ID
    assert payload["state"] == "WAITING"
    record = cli.Store(tmp_path / "state.sqlite3").get(payload["id"])
    assert record is not None
    assert "codex_bin" not in record
    assert record["codex_home"] == str(tmp_path / "codex-home")


def test_register_rejects_missing_desktop_dispatcher_before_persisting(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli,
        "dispatcher_supported",
        lambda: (False, "missing Desktop plugin"),
    )
    code = cli.main(
        [
            "--db",
            str(tmp_path / "state.sqlite3"),
            "register",
            "--thread-id",
            THREAD_ID,
            "--check",
            "echo RUNNING",
            "--success",
            "SUCCESS",
            "--timeout",
            "1h",
        ]
    )
    assert code == 2
    assert "missing Desktop plugin" in capsys.readouterr().err
    assert cli.Store(tmp_path / "state.sqlite3").list() == []


def test_delivery_phase_distinguishes_native_dispatch_states() -> None:
    assert (
        cli._delivery_phase(
            {
                "state": "DELIVERING",
                "delivery_started_at": 123.0,
            }
        )
        == "submitting"
    )
    assert (
        cli._delivery_phase(
            {
                "state": "DELIVERY_PENDING",
            }
        )
        == "pending"
    )
    assert (
        cli._delivery_phase(
            {
                "state": "CANCELLED",
                "delivery_blocked_at": 123.0,
                "delivery_blocked_reason": "thread_archived",
            }
        )
        == "blocked"
    )


def test_status_includes_delivery_phase(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli, "dispatcher_supported", lambda: (True, "supported")
    )
    db_path = tmp_path / "state.sqlite3"
    assert (
        cli.main(
            [
                "--db",
                str(db_path),
                "register",
                "--thread-id",
                THREAD_ID,
                "--check",
                "echo RUNNING",
                "--success",
                "SUCCESS",
                "--timeout",
                "1h",
            ]
        )
        == 0
    )
    monitor_id = json.loads(capsys.readouterr().out)["id"]

    assert cli.main(["--db", str(db_path), "status", monitor_id]) == 0
    assert json.loads(capsys.readouterr().out)["delivery_phase"] == "not_started"
