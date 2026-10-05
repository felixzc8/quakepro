"""Lossless teammate inbox messaging."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, ContextManager, List, Protocol, Tuple

from .claude_model import TEAMS
from .session_model import SessionModel


FileSignature = Tuple[int, int, int, int, bytes]


def _safe_path_component(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and not any(
        separator in value for separator in ("/", "\\")
    )


@dataclass(frozen=True)
class TeammateTarget:
    team: str
    name: str


@dataclass(frozen=True)
class SendResult:
    ok: bool
    recipient: str
    detail: str = ""


class TeammateMessenger(Protocol):
    def target_for(
        self, model: SessionModel, node_id: str
    ) -> TeammateTarget | None: ...

    def send(self, target: TeammateTarget, text: str) -> SendResult: ...


class InboxFileOps(Protocol):
    def locked(self, lock_path: str) -> ContextManager[None]: ...

    def ensure_list(self, path: str) -> None: ...

    def read(self, path: str) -> Tuple[List[object], FileSignature]: ...

    def write_temp(self, path: str, values: List[object]) -> str: ...

    def exchange(self, left: str, right: str) -> None: ...

    def rollback(
        self, temporary: str, path: str, desired: FileSignature
    ) -> Tuple[bool, str]: ...

    def remove(self, path: str) -> None: ...

    def sync_directory(self, path: str) -> None: ...


class PosixInboxFileOps:
    def __init__(self, directory: str = "", descriptor: int | None = None):
        self._directory = directory
        self._descriptor = descriptor

    @contextmanager
    def inbox(self, teams_root: str, team: str):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        os.makedirs(teams_root, mode=0o700, exist_ok=True)
        root = os.open(teams_root, flags)
        descriptors = [root]
        try:
            current = root
            for component in (team, "inboxes"):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                current = os.open(component, flags, dir_fd=current)
                descriptors.append(current)
            directory = os.path.join(teams_root, team, "inboxes")
            yield PosixInboxFileOps(directory, current)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _name(self, path: str) -> str:
        if self._descriptor is None:
            return path
        if os.path.dirname(path) != self._directory:
            raise ValueError("inbox path leaves admitted directory")
        return os.path.basename(path)

    def _open(self, path: str, flags: int, mode: int = 0o600) -> int:
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
        if flags & os.O_CREAT and not flags & os.O_EXCL:
            try:
                descriptor = os.open(
                    self._name(path), flags | os.O_EXCL, mode,
                    dir_fd=self._descriptor,
                )
            except FileExistsError:
                descriptor = os.open(
                    self._name(path), flags & ~os.O_CREAT,
                    dir_fd=self._descriptor,
                )
        else:
            descriptor = os.open(
                self._name(path), flags, mode, dir_fd=self._descriptor,
            )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            os.close(descriptor)
            raise OSError("unsafe inbox file")
        return descriptor

    @contextmanager
    def locked(self, lock_path: str):
        if self._descriptor is None:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        descriptor = self._open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def ensure_list(self, path: str) -> None:
        try:
            descriptor = self._open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
        except FileExistsError:
            return
        try:
            os.write(descriptor, b"[]")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.sync_directory(os.path.dirname(path))

    def read(self, path: str) -> Tuple[List[object], FileSignature]:
        with os.fdopen(self._open(path, os.O_RDONLY), "rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read()
            after = os.fstat(stream.fileno())
        signature = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            hashlib.sha256(raw).digest(),
        )
        before_stat = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if before_stat != signature[:4]:
            raise BlockingIOError("inbox changed while it was being read")
        current = os.stat(
            self._name(path), dir_fd=self._descriptor, follow_symlinks=False
        )
        current_stat = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        if current_stat != signature[:4]:
            raise BlockingIOError("inbox changed while it was being read")
        values = json.loads(raw)
        if not isinstance(values, list):
            raise ValueError("inbox JSON is not a list")
        return values, signature

    def write_temp(self, path: str, values: List[object]) -> str:
        directory = os.path.dirname(path)
        temporary = os.path.join(
            directory,
            f".{os.path.basename(path)}.quakepro-recovery-pending-{uuid.uuid4().hex}.json",
        )
        descriptor = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self.sync_directory(directory)
        return temporary

    def exchange(self, left: str, right: str) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        left_bytes = os.fsencode(self._name(left))
        right_bytes = os.fsencode(self._name(right))
        directory_fd = self._descriptor if self._descriptor is not None else -100
        if (
            self._descriptor is not None and sys.platform == "darwin"
            and hasattr(libc, "renameatx_np")
        ):
            result = libc.renameatx_np(
                directory_fd, left_bytes, directory_fd, right_bytes, 0x00000002
            )
        elif (
            self._descriptor is None and sys.platform == "darwin"
            and hasattr(libc, "renamex_np")
        ):
            result = libc.renamex_np(left_bytes, right_bytes, 0x00000002)
        elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
            result = libc.renameat2(
                directory_fd, left_bytes, directory_fd, right_bytes, 0x00000002
            )
        else:
            raise OSError(errno.ENOTSUP, "atomic inbox exchange is unavailable")
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def rollback(
        self, temporary: str, path: str, desired: FileSignature
    ) -> Tuple[bool, str]:
        try:
            _, current = self.read(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return self._keep_recovery(
                temporary, path, f"rollback conflict: {exc}"
            )
        if current != desired:
            return self._keep_recovery(
                temporary, path, "rollback destination changed"
            )
        try:
            self.exchange(temporary, path)
            self.sync_directory(os.path.dirname(path))
        except OSError as exc:
            return self._keep_recovery(
                temporary, path, f"rollback failed: {exc}"
            )
        try:
            _, rolled_back = self.read(temporary)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return self._keep_recovery(
                temporary, path, f"rollback conflict: {exc}"
            )
        if rolled_back != desired:
            return self._keep_recovery(
                temporary, path, "rollback conflict"
            )
        return True, ""

    def remove(self, path: str) -> None:
        try:
            os.unlink(self._name(path), dir_fd=self._descriptor)
        except FileNotFoundError:
            return
        self.sync_directory(os.path.dirname(path))

    def sync_directory(self, path: str) -> None:
        if self._descriptor is not None:
            if path != self._directory:
                raise ValueError("inbox path leaves admitted directory")
            os.fsync(self._descriptor)
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _keep_recovery(
        self, temporary: str, inbox_path: str, detail: str
    ) -> Tuple[bool, str]:
        recovery = os.path.join(
            os.path.dirname(inbox_path),
            (
                f".{os.path.basename(inbox_path)}.quakepro-recovery-"
                f"{uuid.uuid4().hex}.json"
            ),
        )
        try:
            os.rename(
                self._name(temporary), self._name(recovery),
                src_dir_fd=self._descriptor, dst_dir_fd=self._descriptor,
            )
        except OSError as exc:
            return False, (
                f"{detail}; recovery file: {temporary}; "
                f"recovery rename failed: {exc}"
            )
        try:
            self.sync_directory(os.path.dirname(inbox_path))
        except OSError as exc:
            return False, (
                f"{detail}; recovery file: {recovery}; "
                f"recovery directory sync failed: {exc}"
            )
        return False, f"{detail}; recovery file: {recovery}"


class ClaudeInboxMessenger:
    def __init__(
        self,
        teams_root: str,
        clock: Callable[[], datetime],
        new_id: Callable[[], str],
        files: InboxFileOps,
        wait: Callable[[float], None],
    ) -> None:
        self._teams_root = teams_root
        self._clock = clock
        self._new_id = new_id
        self._files = files
        self._wait = wait

    def target_for(
        self, model: SessionModel, node_id: str
    ) -> TeammateTarget | None:
        if getattr(model, "provider", "") == "overview":
            return None
        node = model.nodes.get(node_id)
        if node is None or node.kind != "teammate" or not node.parent:
            return None
        parent = model.nodes.get(node.parent)
        if parent is None or parent.kind != "team":
            return None
        team = parent.id.split(":", 1)[-1].strip()
        name = (node.label or "").strip()
        if not _safe_path_component(team) or not _safe_path_component(name):
            return None
        return TeammateTarget(team, name)

    def send(self, target: TeammateTarget, text: str) -> SendResult:
        if not text.strip():
            return SendResult(False, target.name, "message is blank")
        if not _safe_path_component(target.team) or not _safe_path_component(
            target.name
        ):
            return SendResult(False, target.name, "invalid teammate target")
        try:
            message = self._message(text)
        except (OSError, ValueError, TypeError) as exc:
            return SendResult(False, target.name, str(exc))
        path = os.path.join(
            self._teams_root,
            target.team,
            "inboxes",
            f"{target.name}.json",
        )
        try:
            admission = (
                self._files.inbox(self._teams_root, target.team)
                if isinstance(self._files, PosixInboxFileOps)
                else nullcontext(self._files)
            )
            with admission as files:
                with files.locked(path + ".lock"):
                    files.ensure_list(path)
                    return self._send_locked(path, target.name, message, files)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return SendResult(False, target.name, str(exc))

    def _message(self, text: str) -> dict:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
        ):
            raise ValueError("clock must return aware UTC datetime")
        timestamp = now.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return {
            "id": f"quakepro-{self._new_id()}",
            "from": "team-lead",
            "text": text,
            "summary": text[:60],
            "timestamp": timestamp.replace("+00:00", "Z"),
            "type": "message",
            "read": False,
        }

    def _send_locked(
        self, path: str, recipient: str, message: dict, files: InboxFileOps
    ) -> SendResult:
        last_error = "inbox kept changing"
        for attempt in range(32):
            try:
                authoritative, expected = files.read(path)
                desired = list(authoritative)
                if not any(
                    isinstance(entry, dict)
                    and entry.get("id") == message["id"]
                    for entry in desired
                ):
                    desired.append(message)
                temporary = files.write_temp(path, desired)
                _, desired_signature = files.read(temporary)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return SendResult(False, recipient, str(exc))

            exchanged = False
            try:
                files.exchange(temporary, path)
                exchanged = True
                files.sync_directory(os.path.dirname(path))
                _, displaced = files.read(temporary)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if exchanged:
                    safe, detail = files.rollback(
                        temporary, path, desired_signature
                    )
                    if not safe:
                        return SendResult(
                            False,
                            recipient,
                            f"{last_error}; {detail}" if detail else last_error,
                        )
                    files.remove(temporary)
                    return SendResult(False, recipient, last_error)
                files.remove(temporary)
                if isinstance(exc, OSError) and exc.errno in {
                    errno.ENOSYS,
                    errno.ENOTSUP,
                    errno.EOPNOTSUPP,
                    errno.EINVAL,
                }:
                    return SendResult(False, recipient, last_error)
            else:
                if displaced == expected:
                    files.remove(temporary)
                    return SendResult(True, recipient)
                safe, detail = files.rollback(
                    temporary, path, desired_signature
                )
                if not safe:
                    return SendResult(False, recipient, detail)
                files.remove(temporary)
                last_error = "inbox kept changing during the atomic exchange"
            if attempt < 31:
                self._wait(0.01 * (attempt + 1))
        return SendResult(False, recipient, last_error)


def default_messenger() -> TeammateMessenger:
    return ClaudeInboxMessenger(
        teams_root=TEAMS,
        clock=lambda: datetime.now(timezone.utc),
        new_id=lambda: uuid.uuid4().hex,
        files=PosixInboxFileOps(),
        wait=time.sleep,
    )
