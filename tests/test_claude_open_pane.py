import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "open-pane.sh"


def test_claude_shell_adapter_dispatches_provider(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy2(SCRIPT, bundle / SCRIPT.name)
    package = bundle / "src" / "quakepro"
    package.mkdir(parents=True)
    (package / "pane_lifecycle.py").write_text("", encoding="utf-8")
    python = bundle / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    argv_log = tmp_path / "argv.json"
    python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$ARGV_LOG\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)

    result = subprocess.run(
        [str(bundle / SCRIPT.name)],
        input="{}",
        text=True,
        capture_output=True,
        env={**os.environ, "ARGV_LOG": str(argv_log)},
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "quakepro.pane_lifecycle",
        "claude",
    ]


def test_shell_adapter_exits_silently_without_runtime(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy2(SCRIPT, bundle / SCRIPT.name)

    result = subprocess.run(
        [str(bundle / SCRIPT.name)],
        input="not json",
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
