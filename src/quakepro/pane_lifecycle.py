"""Provider-neutral hook adapter for QuakePro pane lifecycle."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import glob
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

from .lifecycle import SAFE_ID, append_fact, fact_from_payload
from .provider_admission import read_admission
from .session_identity import hook_transcript_matches
from .session_model import LifecycleFact


MAX_BUDGET = 7.5
try:
    _requested_budget = float(os.environ.get("QUAKEPRO_HOOK_BUDGET", MAX_BUDGET))
except ValueError:
    _requested_budget = MAX_BUDGET
DEADLINE = time.monotonic() + max(0.1, min(_requested_budget, MAX_BUDGET))


class DeadlineExpired(TimeoutError):
    pass


def _time_left(reserve: float = 0.0) -> float:
    return DEADLINE - time.monotonic() - reserve


def _timeout(cap: float, reserve: float = 0.0) -> float:
    available = _time_left(reserve)
    if available <= 0:
        raise DeadlineExpired
    return min(cap, available)


def _sleep(delay: float) -> None:
    available = _time_left()
    if available <= 0:
        raise DeadlineExpired
    time.sleep(min(delay, available))


def _transcript(provider: str, payload: dict, session_id: str) -> str:
    supplied = payload.get("transcript_path")
    if not isinstance(supplied, str) or not supplied:
        return ""
    path = os.path.abspath(os.path.expanduser(supplied))
    for attempt in range(5):
        if provider == "claude" and os.path.isfile(path):
            return path
        if provider == "codex":
            if hook_transcript_matches(path, session_id):
                return path
        if provider == "pi":
            admitted = read_admission(path, "pi")
            if admitted is not None:
                return path if admitted.record.get("id") == session_id else ""
        if attempt != 4:
            _sleep(0.05)
    return ""


def _process_environment(pid: int, pane_variable: str = "TMUX_PANE",
                         socket_variable: str = "TMUX") -> tuple[str, str]:
    try:
        output = subprocess.check_output(
            ["ps", "eww", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_timeout(1.0),
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    pane_match = re.search(rf"(?:^|\s){pane_variable}=([^\s]+)", output)
    socket_pattern = (
        rf"(?:^|\s){socket_variable}=(.*?),\d+,\d+(?=\s|$)"
        if socket_variable == "TMUX" else
        rf"(?:^|\s){socket_variable}=([^\s]+)"
    )
    socket_match = re.search(socket_pattern, output)
    pane = pane_match.group(1) if pane_match else ""
    socket = socket_match.group(1) if socket_match else ""
    return pane, socket


def _herdr_process_environment(pid: int) -> tuple[str, str]:
    return _process_environment(pid, "HERDR_PANE_ID", "HERDR_SOCKET_PATH")


def _pane_host() -> tuple[str, list[int], bool]:
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,command="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_timeout(1.0),
        )
    except (OSError, subprocess.SubprocessError):
        return "", [], True
    processes: dict[int, tuple[int, str]] = {}
    for line in output.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) < 3:
            continue
        try:
            processes[int(fields[0])] = (int(fields[1]), fields[2])
        except ValueError:
            continue
    pid = os.getpid()
    ancestry = []
    for _ in range(24):
        process = processes.get(pid)
        if process is None:
            return "", ancestry, True
        parent, command = process
        ancestry.append(pid)
        words = command.split()
        executable = os.path.basename(words[0]).lstrip("-") if words else ""
        if executable == "tmux" or executable.startswith("tmux:"):
            return "tmux", ancestry, False
        if executable == "herdr" and len(words) > 1 and words[1] == "server":
            return "herdr", ancestry, False
        if parent <= 1:
            return "", ancestry, False
        if parent == pid:
            return "", ancestry, True
        pid = parent
    return "", ancestry, True


def _originating_tmux() -> tuple[str, str, bool]:
    host, ancestry, indeterminate = _pane_host()
    if host != "tmux":
        return "", "", indeterminate or bool(host)
    value = os.environ.get("TMUX", "")
    socket = value.split(",", 1)[0] if value else ""
    return _originating_address(
        ancestry,
        _process_environment,
        os.environ.get("TMUX_PANE", ""),
        socket,
    )


def _originating_address(ancestry, reader, pane: str,
                         socket: str) -> tuple[str, str, bool]:
    ancestry_pane = ""
    ancestry_socket = ""
    for pid in ancestry[1:]:
        if _time_left() <= 0:
            return ancestry_pane, ancestry_socket, True
        found_pane, found_socket = reader(pid)
        if found_pane and found_socket:
            return found_pane, found_socket, False
        ancestry_pane = ancestry_pane or found_pane
        ancestry_socket = ancestry_socket or found_socket
    if pane and socket:
        return pane, socket, False
    pane = pane or ancestry_pane
    socket = socket or ancestry_socket
    return pane, socket, bool(pane or socket)


def _originating_herdr(ancestry: list[int]) -> tuple[str, str, bool]:
    return _originating_address(
        ancestry,
        _herdr_process_environment,
        os.environ.get("HERDR_PANE_ID", ""),
        os.environ.get("HERDR_SOCKET_PATH", ""),
    )


def _state_root() -> Path:
    configured = os.environ.get("QUAKEPRO_STATE_DIR")
    root = Path(configured) if configured else (
        Path(os.environ.get("TMPDIR") or "/tmp") / f"quakepro-{os.getuid()}"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise OSError("unsafe state directory")
    if stat.S_IMODE(root_stat.st_mode) != 0o700:
        root.chmod(0o700)
    return root


@contextmanager
def _session_lock(provider: str, session_id: str):
    path = _state_root() / f"{provider}-{session_id}.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
            raise OSError("unsafe lock file")
        if stat.S_IMODE(lock_stat.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _sleep(0.02)
        yield fd
    finally:
        os.close(fd)


def _tmux_command(binary: str, socket: str) -> list[str]:
    command = [binary]
    if socket:
        command.extend(["-S", socket])
    return command


def _tmux_output(command: list[str], *args: str, timeout_cap: float = 1.0,
                 reserve: float = 0.0) -> str | None:
    try:
        return subprocess.check_output(
            command + list(args),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_timeout(timeout_cap, reserve),
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _tmux_run(command: list[str], *args: str, timeout_cap: float = 0.5,
              reserve: float = 0.0) -> bool:
    try:
        return subprocess.run(
            command + list(args),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_timeout(timeout_cap, reserve),
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _pane_title(provider: str, session_id: str) -> str:
    return f"quakepro:{provider}:{session_id}"


def _herdr_state_path(provider: str, session_id: str) -> Path:
    return _state_root() / f"{provider}-{session_id}.herdr.json"


def _read_herdr_state(provider: str, session_id: str) -> dict | None:
    path = _herdr_state_path(provider, session_id)
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.getuid():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    pane = document.get("pane")
    socket = document.get("socket")
    if not isinstance(pane, str) or not pane or not isinstance(socket, str) or not socket:
        return None
    return document


def _write_herdr_state(provider: str, session_id: str, pane: str, socket: str) -> None:
    path = _herdr_state_path(provider, session_id)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        value = json.dumps({"pane": pane, "socket": socket}).encode()
        os.write(fd, value)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _pane_exists(command: list[str], title: str) -> bool | None:
    titles = _tmux_output(
        command,
        "list-panes",
        "-a",
        "-F",
        "#{pane_title}",
        timeout_cap=1.0,
        reserve=3.0,
    )
    if titles is None:
        return None
    return title in titles.splitlines()


def _herdr_output(binary: str, socket: str, *args: str, timeout_cap: float = 1.0,
                  reserve: float = 0.0) -> dict | None:
    environment = os.environ.copy()
    environment["HERDR_SOCKET_PATH"] = socket
    try:
        output = subprocess.check_output(
            [binary, *args],
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
            timeout=_timeout(timeout_cap, reserve),
        )
        document = json.loads(output)
        return document if isinstance(document, dict) else None
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def _herdr_run(binary: str, socket: str, *args: str, timeout_cap: float = 0.75,
               reserve: float = 0.0) -> bool:
    environment = os.environ.copy()
    environment["HERDR_SOCKET_PATH"] = socket
    try:
        return subprocess.run(
            [binary, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=_timeout(timeout_cap, reserve),
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _open_herdr(provider: str, session_id: str, pane: str, socket: str,
                title: str, launch: str, binary: str) -> None:
    current = _herdr_output(
        binary, socket, "pane", "current", "--pane", pane, reserve=4.0
    )
    current_pane = current.get("result", {}).get("pane", {}) if current else {}
    exact_pane = current_pane.get("pane_id") if isinstance(current_pane, dict) else ""
    if not isinstance(exact_pane, str) or not exact_pane:
        return
    with _session_lock(provider, session_id):
        saved = _read_herdr_state(provider, session_id)
        if saved is not None:
            if saved["socket"] != socket:
                return
            existing = _herdr_output(
                binary, socket, "pane", "get", saved["pane"], reserve=3.0
            )
            existing_pane = existing.get("result", {}).get("pane", {}) if existing else {}
            if isinstance(existing_pane, dict) and existing_pane.get("pane_id"):
                return
            _herdr_state_path(provider, session_id).unlink(missing_ok=True)
        created = _herdr_output(
            binary,
            socket,
            "pane",
            "split",
            exact_pane,
            "--direction",
            "right",
            "--ratio",
            "0.4",
            "--no-focus",
            reserve=2.0,
        )
        created_pane = created.get("result", {}).get("pane", {}) if created else {}
        created_id = created_pane.get("pane_id") if isinstance(created_pane, dict) else ""
        if not isinstance(created_id, str) or not created_id:
            return
        if not _herdr_run(
            binary, socket, "pane", "rename", created_id, title, reserve=1.2
        ):
            _herdr_run(
                binary, socket, "pane", "close", created_id, timeout_cap=0.5
            )
            return
        if not _herdr_run(
            binary, socket, "pane", "run", created_id, launch, reserve=0.6
        ):
            _herdr_run(
                binary, socket, "pane", "close", created_id, timeout_cap=0.5
            )
            return
        try:
            _write_herdr_state(provider, session_id, created_id, socket)
        except OSError:
            _herdr_run(
                binary, socket, "pane", "close", created_id, timeout_cap=0.5
            )


def _close_herdr(provider: str, session_id: str) -> bool:
    path = _herdr_state_path(provider, session_id)
    if not path.exists():
        return False
    with _session_lock(provider, session_id):
        saved = _read_herdr_state(provider, session_id)
        if saved is None:
            return True
        binary = shutil.which("herdr")
        if not binary:
            return True
        if _herdr_run(binary, saved["socket"], "pane", "close", saved["pane"]):
            path.unlink(missing_ok=True)
    return True


def _open_tmux(provider: str, session_id: str, pane: str, socket: str,
               title: str, launch: str, binary: str) -> None:
    command = _tmux_command(binary, socket)
    identified_launch = (
        f"{shlex.quote(binary)} select-pane -t \"$TMUX_PANE\" "
        f"-T {shlex.quote(title)} >/dev/null 2>&1; {launch}"
    )
    with _session_lock(provider, session_id):
        existing = _pane_exists(command, title)
        if existing is None or existing:
            return
        active = _tmux_output(
            command,
            "display-message",
            "-p",
            "-t",
            pane,
            "#{pane_id}",
            timeout_cap=0.75,
            reserve=2.25,
        )
        if not active:
            return
        created = _tmux_output(
            command,
            "split-window",
            "-d",
            "-h",
            "-l",
            "40%",
            "-t",
            pane,
            "-P",
            "-F",
            "#{pane_id}",
            identified_launch,
            timeout_cap=1.5,
            reserve=1.25,
        )
        if not created:
            return
        titled = _tmux_run(
            command,
            "select-pane",
            "-t",
            created,
            "-T",
            title,
            timeout_cap=0.4,
            reserve=0.8,
        )
        _tmux_run(
            command,
            "select-pane",
            "-t",
            active,
            timeout_cap=0.3,
            reserve=0.45,
        )
        if not titled:
            _tmux_run(command, "kill-pane", "-t", created, timeout_cap=0.35)
            return
        _tmux_run(command, "set", "-t", pane, "mouse", "on", timeout_cap=0.35)


def _socket_candidates() -> list[str]:
    sockets = []
    value = os.environ.get("TMUX", "")
    if value:
        sockets.append(value.split(",", 1)[0])
    _, ancestor, _ = _originating_tmux()
    if ancestor:
        sockets.append(ancestor)
    uid = os.getuid()
    temporary = os.environ.get("TMPDIR") or "/tmp"
    sockets.extend(glob.glob(os.path.join(temporary, f"tmux-{uid}", "*")))
    sockets.extend(glob.glob(os.path.join("/private/tmp", f"tmux-{uid}", "*")))
    return list(dict.fromkeys(sockets))


def _close_tmux(provider: str, session_id: str) -> None:
    binary = shutil.which("tmux")
    if not binary:
        return
    title = _pane_title(provider, session_id)
    for socket in _socket_candidates():
        try:
            socket_stat = os.stat(socket)
        except OSError:
            continue
        if not stat.S_ISSOCK(socket_stat.st_mode):
            continue
        command = _tmux_command(binary, socket)
        panes = _tmux_output(
            command,
            "list-panes",
            "-a",
            "-F",
            "#{pane_id} #{pane_title}",
            timeout_cap=0.5,
        )
        if panes is None:
            continue
        for line in panes.splitlines():
            pane_id, separator, pane_title = line.partition(" ")
            if separator and pane_title == title:
                _tmux_run(command, "kill-pane", "-t", pane_id, timeout_cap=0.4)


def _launch(
    directory: Path,
    provider: str,
    transcript: str,
    executable: str | None = None,
) -> str:
    if executable:
        args = [executable]
    else:
        source = str(directory / "src")
        inherited = os.environ.get("PYTHONPATH")
        module_path = source if not inherited else source + os.pathsep + inherited
        args = [
            "env",
            f"PYTHONPATH={module_path}",
            str(directory / ".venv" / "bin" / "python"),
            "-m",
            "quakepro.app",
        ]
    if provider in {"codex", "pi"}:
        args.extend(["--provider", provider])
    args.extend(["--file", transcript])
    return "exec " + shlex.join(args)


def handle(
    provider: str,
    payload: dict,
    directory: Path,
    executable: str | None = None,
) -> None:
    if provider == "pi":
        session_id = payload.get("session_id")
        event = payload.get("hook_event_name")
        fact = (
            LifecycleFact(provider, session_id, event)
            if isinstance(session_id, str)
            and SAFE_ID.fullmatch(session_id)
            and event in {"SessionStart", "SessionEnd"}
            else None
        )
    else:
        fact = fact_from_payload(provider, payload)
    if fact is None:
        return
    if provider != "pi":
        append_fact(fact)
    if fact.event == "SessionEnd":
        if not _close_herdr(provider, fact.session_id):
            _close_tmux(provider, fact.session_id)
        return
    open_event = "SessionStart" if provider == "pi" else "SubagentStart"
    if fact.event != open_event:
        return
    transcript = _transcript(provider, payload, fact.session_id)
    if not transcript:
        return
    launch = _launch(directory, provider, transcript, executable)
    host, ancestry, _ = _pane_host()
    if host == "tmux":
        pane, socket, locator_indeterminate = _originating_tmux()
        tmux = shutil.which("tmux")
        if not locator_indeterminate and pane and socket and tmux:
            _open_tmux(
                provider,
                fact.session_id,
                pane,
                socket,
                _pane_title(provider, fact.session_id),
                launch,
                tmux,
            )
    elif host == "herdr":
        pane, socket, locator_indeterminate = _originating_herdr(ancestry)
        herdr = shutil.which("herdr")
        if not locator_indeterminate and pane and socket and herdr:
            _open_herdr(
                provider,
                fact.session_id,
                pane,
                socket,
                _pane_title(provider, fact.session_id),
                launch,
                herdr,
            )


def main(argv: list[str] | None = None, executable: str | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"claude", "codex", "pi"}:
        return 0
    provider = args[0]
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        handle(provider, payload, Path(__file__).resolve().parents[2], executable)
        if payload.get("hook_event_name") in {"Stop", "SubagentStop"}:
            print("{}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
