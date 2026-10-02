"""Stable source identity and strict reads for provider adapters."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Callable, Optional

from .provider_schema import finite_json_value


@dataclass(frozen=True)
class FileStamp:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class DirectoryStamp:
    path: str
    entries: tuple[tuple[str, str], ...]


def _stamp(info) -> FileStamp:
    return FileStamp(
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _stamp_tuple(value: FileStamp) -> tuple[int, int, int, int, int, int]:
    return (
        value.device,
        value.inode,
        value.mode,
        value.size,
        value.mtime_ns,
        value.ctime_ns,
    )


def _file_stamp(path: str) -> FileStamp:
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"source is not a regular file: {path}")
    return _stamp(info)


def _regular_file_stamp(path: str) -> FileStamp:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"source is not a regular file: {path}")
    return _stamp(info)


def _open_file_stamp(stream) -> FileStamp:
    return _stamp(os.fstat(stream.fileno()))


def _entry_kind(info) -> str:
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    return "nonregular"


def _directory_stamp(
    path: str,
    relevant: Optional[Callable[[str, str], bool]] = None,
) -> DirectoryStamp:
    try:
        with os.scandir(path) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return DirectoryStamp(os.path.abspath(path), ())
    entries = []
    for child in children:
        kind = _entry_kind(child.stat(follow_symlinks=False))
        if relevant is None or relevant(child.name, kind):
            entries.append((child.name, kind))
    return DirectoryStamp(os.path.abspath(path), tuple(entries))


def _strict_text(path: str) -> str:
    with open(path, "rb") as stream:
        return _strict_decode(stream.read())


def _strict_decode(data: bytes) -> str:
    return data.decode("utf-8")


def _finite_json_value(value: object) -> bool:
    return finite_json_value(value)
