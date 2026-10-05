import os
import subprocess
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]


def test_launcher_runs_packaged_app_with_src_on_module_path(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy2(ROOT / "quakepro", bundle / "quakepro")
    python = bundle / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_ARGS\"\n"
        "printf '%s' \"$PYTHONPATH\" > \"$CAPTURE_PATH\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    args_path = tmp_path / "args"
    path_path = tmp_path / "path"
    env = {
        "CAPTURE_ARGS": str(args_path),
        "CAPTURE_PATH": str(path_path),
    }

    result = subprocess.run(
        [str(bundle / "quakepro"), "--root", "/work"],
        cwd=bundle,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert args_path.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "quakepro.cli",
        "--root",
        "/work",
    ]
    assert path_path.read_text(encoding="utf-8").split(os.pathsep)[0] == str(
        bundle / "src"
    )


def test_launcher_honors_local_disable_marker(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy2(ROOT / "quakepro", bundle / "quakepro")
    (bundle / "DISABLED").write_text("maintenance\n", encoding="utf-8")

    result = subprocess.run(
        [str(bundle / "quakepro")],
        cwd=bundle,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == "QuakePro is disabled.\n"
