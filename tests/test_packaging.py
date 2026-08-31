import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_shell_scripts_parse() -> None:
    for relative in ("scripts/install_local.sh", "scripts/uninstall_local.sh"):
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


def test_sleepy_agent_release_layout() -> None:
    skill = ROOT / "skills/sleepy-agent/SKILL.md"
    assert skill.is_file()
    assert "name: sleepy-agent" in skill.read_text()
    assert not (ROOT / "skills/durable-continue").exists()

    readme = (ROOT / "README.md").read_text()
    assert "assets/sleepy-agent-demo.png" in readme
    assert "Injected message: continue" in readme
