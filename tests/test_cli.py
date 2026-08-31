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
    monkeypatch.setattr(cli, "resolve_codex_bin", lambda _value: "/fake/codex")
    monkeypatch.setattr(
        cli, "queue_supported", lambda *_args, **_kwargs: (True, "supported")
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
    assert record["codex_bin"] == "/fake/codex"
    assert record["codex_home"] == str(tmp_path / "codex-home")


def test_register_rejects_old_codex_before_persisting(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "resolve_codex_bin", lambda _value: "/fake/codex")
    monkeypatch.setattr(
        cli, "queue_supported", lambda *_args, **_kwargs: (False, "missing queue")
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
    assert "missing queue" in capsys.readouterr().err
    assert cli.Store(tmp_path / "state.sqlite3").list() == []
