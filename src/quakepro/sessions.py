"""Session discovery and model selection for Claude Code and Codex."""
from __future__ import annotations

import glob
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

from .provider_admission import AdmittedRecord, read_admission, read_admission_result
from .session_model import BODY_CLIP, SessionModel
from .session_identity import (
    claude_project_dir as _claude_project_dir,
    claude_projects as _claude_projects,
    codex_sessions as _codex_sessions,
    detect_provider,
    is_codex_child as _is_codex_child,
)
from .session_cache import open_cached_model, restore_cached_model
from .source_cohort import FileStamp, _strict_decode


PROVIDERS = ("auto", "claude", "codex", "pi")
_CLAUDE_METADATA_RECORDS = 64
_CLAUDE_METADATA_LINE_BYTES = 256 * 1024
_CLAUDE_METADATA_TOTAL_BYTES = 512 * 1024
_DISCOVERY_PROVIDERS = ("claude", "codex", "pi")


@dataclass(frozen=True)
class DiscoveryFailure:
    error_type: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.error_type, str) or not self.error_type:
            raise TypeError("discovery error type must be a nonempty string")
        if len(self.error_type) > 128:
            raise TypeError("discovery error type exceeds 128 characters")
        if not isinstance(self.message, str):
            raise TypeError("discovery error message must be a string")
        if len(self.message) > BODY_CLIP:
            raise TypeError("discovery error message exceeds BODY_CLIP")


@dataclass(frozen=True)
class ProviderDiscovery:
    provider: Literal["claude", "codex", "pi"]
    path: str | None
    error: DiscoveryFailure | None

    def __post_init__(self) -> None:
        if self.provider not in _DISCOVERY_PROVIDERS:
            raise TypeError("unknown discovery provider")
        if self.path is not None and (
            not isinstance(self.path, str)
            or not self.path
            or not os.path.isabs(self.path)
        ):
            raise TypeError("discovery path must be absolute or None")
        if self.error is not None and not isinstance(self.error, DiscoveryFailure):
            raise TypeError("discovery error must be DiscoveryFailure or None")
        if self.error is not None and self.path is not None:
            raise TypeError("failed provider discovery cannot include a path")


@dataclass(frozen=True)
class RootDiscovery:
    root: str
    providers: tuple[ProviderDiscovery, ProviderDiscovery, ProviderDiscovery]

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or not self.root or not os.path.isabs(self.root):
            raise TypeError("discovery root must be an absolute path")
        if not isinstance(self.providers, tuple) or len(self.providers) != 3:
            raise TypeError("root discovery needs exactly three provider results")
        if any(not isinstance(result, ProviderDiscovery) for result in self.providers):
            raise TypeError("root discovery providers must be ProviderDiscovery values")
        if tuple(result.provider for result in self.providers) != _DISCOVERY_PROVIDERS:
            raise TypeError("root discovery providers must be claude, codex, pi")


class _EmptyClaudePath(str):
    """An auto-discovered transcript Claude has created but not written yet."""


@dataclass(frozen=True)
class _CachedCandidate:
    generation: tuple[int, int, int]
    stamp: FileStamp
    accepted: bool
    value: object


@dataclass(frozen=True)
class _CachedDirectory:
    stamp: tuple[int, int, int, int, int]
    files: tuple[str, ...]
    directories: tuple[str, ...]


class DiscoveryCache:
    """Process-local candidate prefix and directory cache for one overview."""

    def __init__(self) -> None:
        self._candidates: dict[tuple[str, str, str], _CachedCandidate] = {}
        self._directories: dict[str, _CachedDirectory] = {}
        self._codex_paths: dict[str, set[str]] = {}

    @staticmethod
    def _file_stamp(path: str) -> tuple[str, FileStamp] | None:
        try:
            canonical = os.path.realpath(os.path.abspath(os.fspath(path)))
            info = os.stat(path)
        except (OSError, TypeError, ValueError):
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return canonical, FileStamp(
            info.st_dev,
            info.st_ino,
            stat.S_IMODE(info.st_mode),
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @staticmethod
    def _generation(stamp: FileStamp) -> tuple[int, int, int]:
        return stamp.device, stamp.inode, stamp.mode

    def admission(self, path: str, provider: str) -> AdmittedRecord | None:
        observed = self._file_stamp(path)
        if observed is None:
            return None
        canonical, stamp = observed
        key = (provider, canonical, "")
        cached = self._candidates.get(key)
        generation = self._generation(stamp)
        if cached is not None and cached.generation == generation and (
            cached.accepted or cached.stamp == stamp
        ):
            if not cached.accepted:
                return None
            return replace(cached.value, stamp=stamp, path=canonical)

        result = read_admission_result(path, provider)
        if result.stamp is None or result.failure in {"unavailable", "unstable"}:
            return result.admitted
        accepted = result.admitted is not None
        self._candidates[key] = _CachedCandidate(
            self._generation(result.stamp),
            result.stamp,
            accepted,
            result.admitted,
        )
        return result.admitted

    def claude_candidate(self, path: str, target: str | None) -> bool:
        observed = self._file_stamp(path)
        if observed is None:
            return False
        canonical, stamp = observed
        target_key = target or ""
        key = ("claude", canonical, target_key)
        cached = self._candidates.get(key)
        generation = self._generation(stamp)
        if cached is not None and cached.generation == generation and (
            cached.accepted or cached.stamp == stamp
        ):
            return cached.accepted
        try:
            records = _read_bounded_claude_records(path)
        except OSError:
            return False
        after = self._file_stamp(path)
        if after is None or after != observed:
            return False
        accepted = _claude_records_match(records, target)
        self._candidates[key] = _CachedCandidate(
            generation, stamp, accepted, None,
        )
        return accepted

    @staticmethod
    def _directory_stamp(path: str) -> tuple[int, int, int, int, int] | None:
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None
        return (
            info.st_dev,
            info.st_ino,
            stat.S_IMODE(info.st_mode),
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _listing(self, directory: str) -> _CachedDirectory | None:
        path = _absolute(directory)
        stamp = self._directory_stamp(path)
        if stamp is None:
            return None
        cached = self._directories.get(path)
        if cached is not None and cached.stamp == stamp:
            return cached
        files = []
        directories = []
        with os.scandir(path) as stream:
            entries = list(stream)
        for entry in entries:
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_directory:
                directories.append(os.path.abspath(entry.path))
                continue
            if not entry.name.endswith(".jsonl"):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    files.append(os.path.abspath(entry.path))
            except OSError:
                continue
        value = _CachedDirectory(
            stamp, tuple(sorted(files)), tuple(sorted(directories)),
        )
        self._directories[path] = value
        return value

    def jsonl_files(self, directory: str, recursive: bool = False) -> list[str]:
        found = []

        def visit(current: str, top: bool = False) -> None:
            try:
                listing = self._listing(current)
            except FileNotFoundError:
                if top:
                    return
                raise
            if listing is None:
                return
            found.extend(listing.files)
            if recursive:
                for child in listing.directories:
                    visit(child)

        visit(directory, top=True)
        return found

    def codex_files(self, directory: str) -> list[str]:
        root = _absolute(directory)
        known = self._codex_paths.get(root)
        if known is None:
            known = set(self.jsonl_files(root, recursive=True))
            self._codex_paths[root] = known
        else:
            for current in _current_date_dirs(root):
                known.update(self.jsonl_files(current))
        return sorted(known)


def _provider_id(value) -> bool:
    return isinstance(value, str) and bool(value) and "\0" not in value


def _is_claude_record(record: dict) -> bool:
    record_type = record.get("type")
    if not isinstance(record_type, str):
        return False
    if record_type in {"assistant", "user", "progress"}:
        return "message" in record or "data" in record
    return (record_type in {"queue-operation", "file-history-snapshot"}
            and _provider_id(record.get("sessionId")))


def _candidate_cwd(value) -> str | None:
    if not isinstance(value, str) or "\0" in value or not os.path.isabs(value):
        return None
    try:
        return os.path.realpath(value)
    except (OSError, ValueError):
        return None


def _valid_codex_identity(meta: dict) -> bool:
    if not _provider_id(meta.get("id")):
        return False
    for field in ("session_id", "parent_thread_id", "forked_from_id"):
        value = meta.get(field)
        if value is not None and not _provider_id(value):
            return False
    thread_source = meta.get("thread_source")
    if thread_source is not None and not isinstance(thread_source, str):
        return False
    return True


def _read_bounded_claude_records(path: str) -> list[dict] | None:
    total = 0
    records = []
    with open(path, "rb") as stream:
        for _ in range(_CLAUDE_METADATA_RECORDS):
            remaining = _CLAUDE_METADATA_TOTAL_BYTES - total
            if remaining <= 0:
                return None
            read_size = min(_CLAUDE_METADATA_LINE_BYTES, remaining)
            raw = stream.readline(read_size)
            if not raw:
                break
            total += len(raw)
            if not raw.endswith(b"\n") and len(raw) == read_size:
                return None
            try:
                record = json.loads(_strict_decode(raw))
            except UnicodeDecodeError:
                return None
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _bounded_claude_records(path: str) -> list[dict] | None:
    try:
        return _read_bounded_claude_records(path)
    except OSError:
        return None


def _claude_records_match(records: list[dict] | None, target: str | None) -> bool:
    if records is None:
        return False
    root_matches = target is None
    saw_provider = False
    for record in records:
        saw_provider |= _is_claude_record(record)
        if target is not None and "cwd" in record:
            if _candidate_cwd(record.get("cwd")) != target:
                return False
            root_matches = True
    return root_matches and saw_provider


def _bounded_claude_candidate(path: str, target: str | None) -> bool:
    return _claude_records_match(_bounded_claude_records(path), target)


def _latest_claude(project_filter: str | None,
                   allow_empty: bool = False) -> tuple[str, float] | None:
    candidate_root = os.path.expanduser(project_filter) if project_filter else ""
    exact_root = os.path.realpath(candidate_root) if os.path.isdir(candidate_root) else None
    if exact_root:
        files = glob.glob(os.path.join(_claude_project_dir(exact_root), "*.jsonl"))
    else:
        files = glob.glob(os.path.join(_claude_projects(), "*", "*.jsonl"))
    if project_filter and not exact_root:
        files = [path for path in files if project_filter in path]
    candidates = []
    for path in files:
        try:
            candidates.append((path, os.path.getmtime(path), os.path.getsize(path)))
        except OSError:
            continue
    candidates.sort(key=lambda item: (item[1], item[0]), reverse=True)
    for path, mtime, size in candidates:
        if size == 0:
            if allow_empty and not exact_root:
                return _EmptyClaudePath(path), mtime
            continue
        if not _bounded_claude_candidate(path, exact_root):
            continue
        return path, mtime
    return None


def _latest_codex(project_filter: str | None) -> tuple[str, float] | None:
    entries: dict[str, tuple[str, dict[str, Any], float]] = {}
    paths = _jsonl_files(_codex_sessions(), recursive=True)
    for path in paths:
        admission = read_admission(path, "codex")
        if admission is None:
            continue
        meta = admission.record["payload"]
        thread_id = str(meta["id"])
        mtime = admission.stamp.mtime_ns / 1_000_000_000
        entries[thread_id] = (path, meta, mtime)

    roots = {
        thread_id: entry for thread_id, entry in entries.items()
        if not _is_codex_child(entry[1]) and str(entry[1].get("session_id") or thread_id) == thread_id
    }
    ranked = []
    for root_id, (root_path, root_meta, root_mtime) in roots.items():
        if project_filter and project_filter not in str(root_meta.get("cwd") or ""):
            continue
        newest = root_mtime
        for _, meta, mtime in entries.values():
            session_id = str(meta.get("session_id") or meta.get("id") or "")
            if session_id == root_id:
                newest = max(newest, mtime)
        ranked.append((root_path, newest))
    return max(ranked, key=lambda item: item[1]) if ranked else None


def _latest_pi(project_filter: str | None) -> tuple[str, float] | None:
    from .pi_model import pi_session_files

    ranked = []
    for path in pi_session_files():
        admission = read_admission(path, "pi")
        if admission is None:
            continue
        header = admission.record
        if project_filter and project_filter not in str(header.get("cwd") or ""):
            continue
        ranked.append((path, admission.stamp.mtime_ns / 1_000_000_000))
    return max(ranked, key=lambda item: (item[1], item[0])) if ranked else None


def latest_session(project_filter: str | None = None, provider: str = "auto") -> str | None:
    """Return the newest matching root session for one or all providers."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    candidates = []
    if provider in ("auto", "claude"):
        candidate = _latest_claude(project_filter, allow_empty=True)
        if candidate:
            candidates.append(candidate)
    if provider in ("auto", "codex"):
        candidate = _latest_codex(project_filter)
        if candidate:
            candidates.append(candidate)
    if provider in ("auto", "pi"):
        candidate = _latest_pi(project_filter)
        if candidate:
            candidates.append(candidate)
    if candidates:
        return max(candidates, key=lambda item: (item[1], item[0]))[0]
    return None


def _session_cache_root(path: str, provider: str, project_root: str | None) -> str:
    del provider
    candidate = project_root if project_root else path
    return os.path.realpath(os.path.abspath(os.path.expanduser(candidate)))


def _model_components(path: str, provider: str):
    if provider == "codex":
        from .codex_model import CodexModel
        from .codex_snapshot import CodexSnapshotCodec

        return CodexSnapshotCodec(), lambda: CodexModel(path)
    if provider == "pi":
        from .pi_model import PiModel
        from .pi_snapshot import PiSnapshotCodec

        return PiSnapshotCodec(), lambda: PiModel(path)
    from .claude_model import ClaudeModel
    from .claude_snapshot import ClaudeSnapshotCodec

    return ClaudeSnapshotCodec(), lambda: ClaudeModel(path)


def open_model(
    path: str, provider: str = "auto", project_root: str | None = None
) -> SessionModel:
    """Construct one cached transcript model."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    cache_root = _session_cache_root(path, provider, project_root)
    candidates = _DISCOVERY_PROVIDERS if provider == "auto" else (provider,)
    components = {}
    for candidate in candidates:
        codec, build = _model_components(path, candidate)
        components[candidate] = codec, build
        restored = restore_cached_model(path, cache_root, codec)
        if restored is not None:
            return restored
    allow_empty_claude = False
    if isinstance(path, _EmptyClaudePath):
        try:
            allow_empty_claude = os.path.getsize(path) == 0
        except OSError as exc:
            raise ValueError(f"cannot read session transcript: {path}") from exc
    if allow_empty_claude and provider in ("auto", "claude"):
        codec, build = components.get("claude") or _model_components(path, "claude")
        return open_cached_model(
            path,
            cache_root,
            codec,
            build,
        )
    detected = detect_provider(path)
    if provider != "auto" and provider != detected:
        raise ValueError(f"{path} is a {detected} session, not {provider}")
    codec, build = components.get(detected) or _model_components(path, detected)
    return open_cached_model(
        path,
        cache_root,
        codec,
        build,
    )


def _absolute(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _pi_agent_dir() -> str:
    override = os.environ.get("PI_CODING_AGENT_DIR")
    return _absolute(override) if override else _absolute("~/.pi/agent")


def _pi_session_slug(root: str) -> str:
    path = _absolute(root)
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", path)) + "--"


def _pi_session_dir(root: str) -> str:
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return _absolute(override)
    return os.path.join(_pi_agent_dir(), "sessions", _pi_session_slug(root))


def discovery_observation_paths(root: str) -> frozenset[str]:
    """Provider source roots whose changes can alter discovery for `root`."""
    if not isinstance(root, str):
        raise TypeError("discovery root must be a string")
    normalized = _absolute(root)
    return frozenset({
        _absolute(_claude_project_dir(normalized)),
        _absolute(_codex_sessions()),
        _pi_session_dir(normalized),
    })


def _jsonl_files(directory: str, recursive: bool = False) -> list[str]:
    """List candidates while allowing directory read failures to escape."""
    found = []

    def visit(current: str, top: bool = False) -> None:
        try:
            stream = os.scandir(current)
        except FileNotFoundError:
            if top:
                return
            raise
        with stream:
            entries = list(stream)
        for entry in entries:
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if recursive and is_directory:
                visit(entry.path)
                continue
            if is_directory or not entry.name.endswith(".jsonl"):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    found.append(os.path.abspath(entry.path))
            except OSError:
                continue

    visit(_absolute(directory), top=True)
    return found


def _ranked_candidates(paths: list[str], require_content: bool = False) -> list[str]:
    ranked = []
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if require_content and not stat.st_size:
            continue
        ranked.append((stat.st_mtime, path))
    return [path for _, path in sorted(ranked, reverse=True)]


def _newest_claude_in(
    root: str, cache: DiscoveryCache | None = None,
) -> str | None:
    spelled_root = _absolute(root)
    target = os.path.realpath(spelled_root)
    directory = _claude_project_dir(spelled_root)
    paths = (
        cache.jsonl_files(directory)
        if cache is not None
        else _jsonl_files(directory)
    )
    for path in _ranked_candidates(paths, require_content=True):
        accepted = (
            cache.claude_candidate(path, target)
            if cache is not None
            else _bounded_claude_candidate(path, target)
        )
        if accepted:
            return path
    return None


def _newest_codex_in(
    root: str, cache: DiscoveryCache | None = None,
) -> str | None:
    target = os.path.realpath(_absolute(root))
    directory = _codex_sessions()
    paths = (
        cache.codex_files(directory)
        if cache is not None
        else _jsonl_files(directory, recursive=True)
    )
    ranked = []
    for path in paths:
        admission = (
            cache.admission(path, "codex")
            if cache is not None
            else read_admission(path, "codex")
        )
        if admission is None:
            continue
        meta = admission.record["payload"]
        if not _valid_codex_identity(meta):
            continue
        if _is_codex_child(meta):
            continue
        thread_id = meta["id"]
        if (meta.get("session_id") or thread_id) != thread_id:
            continue
        cwd = meta.get("cwd")
        if _candidate_cwd(cwd) != target:
            continue
        ranked.append((admission.stamp.mtime_ns, path))
    return max(ranked)[1] if ranked else None


def _pi_header(path: str) -> dict:
    admission = read_admission(path, "pi")
    return admission.record if admission is not None else {}


def _newest_pi_in(
    root: str, cache: DiscoveryCache | None = None,
) -> str | None:
    spelled_root = _absolute(root)
    target = os.path.realpath(spelled_root)
    directory = _pi_session_dir(spelled_root)
    paths = (
        cache.jsonl_files(directory)
        if cache is not None
        else _jsonl_files(directory)
    )
    ranked = []
    for path in paths:
        admission = (
            cache.admission(path, "pi")
            if cache is not None
            else read_admission(path, "pi")
        )
        header = admission.record if admission is not None else {}
        cwd = header.get("cwd") if header else None
        if _candidate_cwd(cwd) != target:
            continue
        ranked.append((admission.stamp.mtime_ns, path))
    return max(ranked)[1] if ranked else None


def _discovery_failure(exc: OSError) -> DiscoveryFailure:
    return DiscoveryFailure(type(exc).__name__[:128], str(exc)[:BODY_CLIP])


def _current_date_dirs(root: str) -> tuple[str, ...]:
    dates = {datetime.now(), datetime.now(timezone.utc)}
    return tuple(sorted({
        os.path.join(root, f"{now.year:04d}", f"{now.month:02d}", f"{now.day:02d}")
        for now in dates
    }))


def sessions_for_root(
    root: str, cache: DiscoveryCache | None = None,
) -> RootDiscovery:
    """Newest root session from each provider, with scoped enumeration errors."""
    if cache is not None and not isinstance(cache, DiscoveryCache):
        raise TypeError("session discovery cache must be DiscoveryCache")
    normalized = _absolute(root)
    results = []
    for provider, discover in zip(
        _DISCOVERY_PROVIDERS,
        (_newest_claude_in, _newest_codex_in, _newest_pi_in),
    ):
        try:
            path = discover(normalized, cache)
        except OSError as exc:
            results.append(ProviderDiscovery(provider, None, _discovery_failure(exc)))
        else:
            results.append(ProviderDiscovery(provider, path, None))
    return RootDiscovery(normalized, tuple(results))
