import subprocess
from pathlib import Path
from zipfile import ZipFile


def test_wheel_exposes_quakepro_command_and_runtime(tmp_path):
    project = Path(__file__).resolve().parents[1]
    built = subprocess.run(
        [
            "uv", "build", "--wheel", "--offline",
            "--out-dir", str(tmp_path), str(project),
        ],
        capture_output=True,
        text=True,
    )

    assert built.returncode == 0, built.stderr
    wheel, = tmp_path.glob("quakepro-*.whl")
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        entry_points = archive.read(
            next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        ).decode()

    assert "quakepro = quakepro.cli:main" in entry_points
    assert "quakepro/cli.py" in names
    assert "quakepro/app.py" in names
