"""Installed QuakePro command."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import shutil
import sys


def _version() -> str:
    try:
        return version("quakepro")
    except PackageNotFoundError:
        return "0.1.0"


def _executable() -> str:
    configured = os.environ.get("QUAKEPRO_EXECUTABLE")
    if configured:
        candidate = configured
    elif os.sep in sys.argv[0]:
        candidate = sys.argv[0]
    else:
        candidate = shutil.which(sys.argv[0]) or ""
    path = Path(candidate).expanduser().absolute() if candidate else None
    if path is None or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("cannot find stable quakepro executable")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"quakepro {_version()}")
        return 0
    if args[:1] == ["hooks"]:
        from .hook_config import hooks_main

        try:
            return hooks_main(args[1:], _executable)
        except RuntimeError as exc:
            print(f"quakepro hooks: {exc}", file=sys.stderr)
            return 1
    if args[:1] == ["setup"]:
        from .hook_config import setup_main

        try:
            return setup_main(args[1:], _executable)
        except RuntimeError as exc:
            print(f"quakepro setup: {exc}", file=sys.stderr)
            return 1
    if args[:1] == ["doctor"]:
        from .hook_config import doctor_main

        try:
            return doctor_main(args[1:], _executable)
        except RuntimeError as exc:
            print(f"quakepro doctor: {exc}", file=sys.stderr)
            return 1
    if args[:1] == ["_hook"]:
        from .pane_lifecycle import main as hook_main

        try:
            executable = _executable()
        except RuntimeError:
            return 0
        return hook_main(args[1:], executable=executable)
    from .app import main as app_main

    return app_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
