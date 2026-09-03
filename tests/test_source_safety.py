from pathlib import Path


def test_only_desktop_native_pipe_can_write_a_thread() -> None:
    root = Path(__file__).parents[1]
    source_paths = [
        *sorted((root / "src" / "durable_continue").glob("*.py")),
        *sorted((root / "mcp").glob("*.mjs")),
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    forbidden = (
        "app-server",
        "app_server",
        "codex queue",
        "thread/queue/",
        "thread/resume",
        "thread/start",
        "turn/start",
        "turn/steer",
        "codex exec resume",
        "queue_1.sqlite",
        "AXFocusedUIElement",
        "codex://threads/",
    )
    assert not [item for item in forbidden if item in source]
    assert 'candidate?.name === "send_message_to_thread"' in source
    assert 'prompt: "continue"' in source
    assert "CODEX_APP_TOOLS_PIPE_PATH" in source
    assert "find_continue_after" in source


def test_launch_agent_never_spawns_a_delivery_writer() -> None:
    daemon = (
        Path(__file__).parents[1]
        / "src"
        / "durable_continue"
        / "daemon.py"
    ).read_text(encoding="utf-8")
    assert "claim_due_delivery" not in daemon
    assert "Popen" not in daemon
