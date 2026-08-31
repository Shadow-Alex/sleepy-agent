from pathlib import Path


def test_production_has_no_writer_takeover_or_session_fallback() -> None:
    source_root = Path(__file__).parents[1] / "src" / "durable_continue"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.glob("*.py")
    )
    forbidden = (
        "thread/resume",
        "thread/start",
        "thread/fork",
        "turn/steer",
        "codex exec resume",
    )
    assert not [item for item in forbidden if item in source]
