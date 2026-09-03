import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_shell_scripts_parse() -> None:
    for relative in (
        "scripts/install_local.sh",
        "scripts/uninstall_local.sh",
        "scripts/launch_dispatcher.sh",
    ):
        result = subprocess.run(
            ["/bin/bash", "-n", str(ROOT / relative)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_launchd_template_is_complete() -> None:
    text = (
        ROOT / "launchd/io.github.shadow-alex.durable-continue.plist.in"
    ).read_text()
    for placeholder in (
        "__PYTHON__",
        "__HOME__",
        "__CODEX_HOME__",
        "__STATE_ROOT__",
        "__PATH__",
        "__PYTHONPATH__",
        "__LOG_DIR__",
        "__SOURCE_DIR__",
    ):
        assert placeholder in text


def test_plugin_manifest_and_dispatcher_files_exist() -> None:
    for relative in (
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "mcp/dispatcher.mjs",
        "mcp/native_pipe_client.mjs",
        "skills/sleepy-agent/SKILL.md",
        "assets/codex-continue-delivery.png",
    ):
        assert (ROOT / relative).is_file(), relative


def test_public_names_and_versions_are_consistent() -> None:
    manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "sleepy-agent"
    assert manifest["version"] == "0.4.0"
    assert manifest["mcpServers"] == "./.mcp.json"

    skill = (ROOT / "skills/sleepy-agent/SKILL.md").read_text(encoding="utf-8")
    assert "name: sleepy-agent" in skill
    assert "Desktop-owned dispatcher" in skill
    assert not (ROOT / "skills/durable-continue").exists()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "assets/codex-continue-delivery.png" in readme
    assert "codex queue" in readme


def test_node_sources_parse() -> None:
    for relative in ("mcp/dispatcher.mjs", "mcp/native_pipe_client.mjs"):
        result = subprocess.run(
            ["node", "--check", str(ROOT / relative)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_native_pipe_node_suite() -> None:
    result = subprocess.run(
        [
            "node",
            "--test",
            str(ROOT / "tests" / "node" / "test_native_pipe_client.mjs"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
