"""Validated, disposable startup snapshots for provider models."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol

from . import provider_reload
from .session_model import (
    LifecycleFact,
    LifecycleSessionModel,
    Node,
    SessionModel,
    Step,
    Task,
)


CACHE_VERSION = 3
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_FORMAT = "quakepro-session-cache"
_ENTRY_FIELDS = {"provider", "requested", "codec_schema", "sources", "payload"}
_FILE_SOURCE_FIELDS = {"kind", "device", "inode", "mode", "offset"}
_DIRECTORY_SOURCE_FIELDS = {"kind", "recursive"}


@dataclass(frozen=True)
class ResumePoint:
    offset: int


@dataclass(frozen=True)
class SourceClaim:
    path: str
    kind: Literal["file", "directory"]
    recursive: bool = False
    resume: ResumePoint | None = None
    device: int | None = None
    inode: int | None = None
    mode: int | None = None


@dataclass(frozen=True)
class Snapshot:
    schema_version: int
    sources: tuple[SourceClaim, ...]
    payload: object


class SnapshotCodec(Protocol):
    provider: str
    schema_version: int

    def capture(self, model: SessionModel) -> Snapshot: ...

    def restore(
        self,
        requested_path: str,
        snapshot: Snapshot,
    ) -> SessionModel: ...


def _cache_dir() -> Path:
    configured = os.environ.get("QUAKEPRO_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / "quakepro"


def _real_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _json_value(value: object) -> bool:
    if type(value) is float:
        return math.isfinite(value)
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is list:
        return all(_json_value(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _json_value(item) for key, item in value.items()
        )
    return False


def _string_map(values: object, valid_value=None) -> bool:
    if type(values) is not dict:
        return False
    return all(
        type(key) is str and (valid_value is None or valid_value(value))
        for key, value in values.items()
    )


def _valid_step(value: object) -> bool:
    return type(value) is Step and (
        type(value.kind) is str
        and type(value.title) is str
        and type(value.body) is str
        and type(value.ts) is float
        and math.isfinite(value.ts)
        and type(value.status) is str
        and type(value.output) is str
        and type(value.tid) is str
        and type(value.child) is str
        and type(value.bg_path) is str
        and type(value.dispatch) is str
        and type(value.goal) is str
    )


def _valid_node(value: object) -> bool:
    return type(value) is Node and (
        type(value.id) is str
        and type(value.label) is str
        and type(value.detail) is str
        and (value.parent is None or type(value.parent) is str)
        and type(value.status) is str
        and type(value.steps) is list
        and all(_valid_step(step) for step in value.steps)
        and type(value.children) is list
        and all(type(child) is str for child in value.children)
        and type(value.tokens) is int
        and type(value.last_ts) is float
        and math.isfinite(value.last_ts)
        and type(value.model) is str
        and type(value.kind) is str
        and type(value.result) is str
        and type(value.skills) is list
        and all(type(skill) is str for skill in value.skills)
        and type(value.phase) is str
        and type(value.workflow) is list
        and all(type(item) is str for item in value.workflow)
        and type(value.finish) is str
        and type(value.active_burst) is int
    )


def _valid_task(value: object) -> bool:
    return type(value) is Task and all(
        type(getattr(value, field.name)) is str for field in fields(Task)
    )


def _validate_shared_state(nodes: object, task_list: object) -> None:
    if not _string_map(nodes, _valid_node) or "main" not in nodes:
        raise ValueError("invalid cached nodes")
    node_ids = set(nodes)
    if nodes["main"].parent is not None:
        raise ValueError("invalid cached main parent")
    for node_id, node in nodes.items():
        if (
            node.id != node_id
            or len(node.children) != len(set(node.children))
            or node_id in node.children
            or (node_id != "main" and node.parent not in node_ids)
            or any(child not in node_ids for child in node.children)
        ):
            raise ValueError("invalid cached node links")
        if node_id != "main" and node_id not in nodes[node.parent].children:
            raise ValueError("invalid cached parent link")
        for child in node.children:
            if nodes[child].parent != node_id:
                raise ValueError("invalid cached child link")
        for step in node.steps:
            if step.child and (step.child not in node_ids or step.child == node_id):
                raise ValueError("invalid cached step child")
            if step.dispatch and step.dispatch in nodes:
                dispatch = nodes[step.dispatch]
                if dispatch.kind != "dispatch" or dispatch.parent != node_id:
                    raise ValueError("invalid cached step dispatch")
                if step.child and (
                    step.child == step.dispatch
                    or nodes[step.child].parent != step.dispatch
                    or step.child not in dispatch.children
                ):
                    raise ValueError("invalid cached dispatch child")
    visited = set()
    visiting = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("cached node cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for child in nodes[node_id].children:
            visit(child)
        visiting.remove(node_id)
        visited.add(node_id)

    visit("main")
    if visited != node_ids:
        raise ValueError("invalid cached node links")
    if not _string_map(
        task_list,
        lambda tasks: type(tasks) is list and all(_valid_task(task) for task in tasks),
    ):
        raise ValueError("invalid cached tasks")


class _GraphEncoder:
    _TAGS = {
        Node: "session_model.Node",
        Step: "session_model.Step",
        Task: "session_model.Task",
    }

    def __init__(self) -> None:
        self._seen: dict[int, int] = {}
        self.objects: list[dict] = []

    def encode(self, value: object):
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("non-finite cached float")
            return value
        if value is None or type(value) in (bool, int, str):
            return value
        if type(value) is bytes:
            return {"$bytes": base64.b64encode(value).decode("ascii")}
        if type(value) is tuple:
            return {"$tuple": [self.encode(item) for item in value]}
        if type(value) is set:
            return {"$set": [self.encode(item) for item in sorted(value, key=repr)]}
        if type(value) is list:
            return {"$list": [self.encode(item) for item in value]}
        if type(value) is dict:
            return {"$dict": [
                [self.encode(key), self.encode(item)] for key, item in value.items()
            ]}
        value_type = type(value)
        if value_type in self._TAGS:
            marker = id(value)
            if marker in self._seen:
                return {"$ref": self._seen[marker]}
            index = len(self.objects)
            self._seen[marker] = index
            record = {"type": self._TAGS[value_type], "fields": {}}
            self.objects.append(record)
            record["fields"] = {
                field.name: self.encode(getattr(value, field.name))
                for field in fields(value_type)
            }
            return {"$ref": index}
        raise TypeError(f"unsupported cached value: {value_type.__name__}")


class _GraphDecoder:
    _TYPES = {
        "session_model.Node": Node,
        "session_model.Step": Step,
        "session_model.Task": Task,
    }

    def __init__(self, records: object) -> None:
        if type(records) is not list:
            raise ValueError("invalid object table")
        self._records = records
        self._objects = []
        for record in records:
            if (
                type(record) is not dict
                or set(record) != {"type", "fields"}
                or record.get("type") not in self._TYPES
                or type(record.get("fields")) is not dict
            ):
                raise ValueError("invalid cached object type")
            value_type = self._TYPES[record["type"]]
            if set(record["fields"]) != {field.name for field in fields(value_type)}:
                raise ValueError("invalid cached object fields")
            self._objects.append(value_type.__new__(value_type))

    def fill(self) -> None:
        for target, record in zip(self._objects, self._records):
            for name, value in record["fields"].items():
                setattr(target, name, self.decode(value))
        if any(
            not (
                _valid_node(value)
                or _valid_step(value)
                or _valid_task(value)
            )
            for value in self._objects
        ):
            raise ValueError("invalid cached object value")

    def decode(self, value: object):
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("non-finite cached float")
            return value
        if value is None or type(value) in (bool, int, str):
            return value
        if type(value) is not dict or len(value) != 1:
            raise ValueError("invalid cached graph value")
        if "$bytes" in value:
            encoded = value["$bytes"]
            if type(encoded) is not str:
                raise ValueError("invalid cached bytes")
            return base64.b64decode(encoded, validate=True)
        if "$tuple" in value:
            values = value["$tuple"]
            if type(values) is not list:
                raise ValueError("invalid cached tuple")
            return tuple(self.decode(item) for item in values)
        if "$set" in value:
            values = value["$set"]
            if type(values) is not list:
                raise ValueError("invalid cached set")
            decoded = [self.decode(item) for item in values]
            try:
                if len(set(decoded)) != len(decoded):
                    raise ValueError("duplicate cached set item")
                return set(decoded)
            except TypeError as exc:
                raise ValueError("invalid cached set item") from exc
        if "$list" in value:
            values = value["$list"]
            if type(values) is not list:
                raise ValueError("invalid cached list")
            return [self.decode(item) for item in values]
        if "$dict" in value:
            pairs = value["$dict"]
            if type(pairs) is not list:
                raise ValueError("invalid cached dictionary")
            decoded = {}
            for pair in pairs:
                if type(pair) is not list or len(pair) != 2:
                    raise ValueError("invalid cached dictionary item")
                key = self.decode(pair[0])
                try:
                    if key in decoded:
                        raise ValueError("duplicate cached dictionary key")
                    decoded[key] = self.decode(pair[1])
                except TypeError as exc:
                    raise ValueError("invalid cached dictionary key") from exc
            return decoded
        if "$ref" in value:
            index = value["$ref"]
            if type(index) is not int or not 0 <= index < len(self._objects):
                raise ValueError("invalid cached object reference")
            return self._objects[index]
        raise ValueError("invalid cached graph marker")


def _encode_payload(value: object) -> dict:
    encoder = _GraphEncoder()
    state = encoder.encode(value)
    return {"state": state, "objects": encoder.objects}


def _decode_payload(value: object) -> object:
    if type(value) is not dict or set(value) != {"state", "objects"}:
        raise ValueError("invalid cached payload")
    decoder = _GraphDecoder(value["objects"])
    decoder.fill()
    state = decoder.decode(value["state"])
    nodes = [item for item in decoder._objects if type(item) is Node]
    if nodes:
        by_id = {node.id: node for node in nodes}
        if len(by_id) != len(nodes):
            raise ValueError("duplicate cached node id")
        if type(state) is dict and "nodes" in state:
            mapped = state["nodes"]
            task_list = state.get("task_list")
            _validate_shared_state(mapped, task_list)
            if {id(node) for node in mapped.values()} != {id(node) for node in nodes}:
                raise ValueError("cached node table does not match state")
        else:
            _validate_shared_state(by_id, {})
    return state


def _validate_claims(claims: object) -> tuple[SourceClaim, ...]:
    if type(claims) is not tuple:
        raise ValueError("snapshot sources must be a tuple")
    seen = set()
    validated = []
    for claim in claims:
        if type(claim) is not SourceClaim:
            raise ValueError("invalid snapshot source claim")
        canonical = _real_path(claim.path) if type(claim.path) is str else ""
        if (
            type(claim.path) is not str
            or not claim.path
            or "\0" in claim.path
            or not os.path.isabs(claim.path)
            or canonical in seen
            or type(claim.recursive) is not bool
        ):
            raise ValueError("invalid snapshot source path")
        if claim.kind == "file":
            if claim.recursive:
                raise ValueError("file source cannot be recursive")
            if (
                type(claim.resume) is not ResumePoint
                or type(claim.resume.offset) is not int
                or claim.resume.offset < 0
                or type(claim.device) is not int
                or claim.device < 0
                or type(claim.inode) is not int
                or claim.inode < 0
                or type(claim.mode) is not int
                or claim.mode < 0
            ):
                raise ValueError("invalid checkpoint file cursor")
        elif claim.kind == "directory":
            if (
                claim.resume is not None
                or claim.device is not None
                or claim.inode is not None
                or claim.mode is not None
            ):
                raise ValueError("directory source cannot carry cursor identity")
        else:
            raise ValueError("invalid snapshot source kind")
        seen.add(canonical)
        validated.append(SourceClaim(
            canonical,
            claim.kind,
            claim.recursive,
            claim.resume,
            claim.device,
            claim.inode,
            claim.mode,
        ))
    return tuple(validated)


def _capture_manifest(claims: tuple[SourceClaim, ...]) -> dict:
    files = {}
    directories = {}
    for claim in claims:
        if claim.kind == "file":
            files[claim.path] = {
                "kind": "file",
                "device": claim.device,
                "inode": claim.inode,
                "mode": claim.mode,
                "offset": claim.resume.offset,
            }
        else:
            directories[claim.path] = {
                "kind": "directory",
                "recursive": claim.recursive,
            }
    return {"files": files, "directories": directories}


def _fence_manifest(manifest: Mapping[str, object]) -> None:
    files = manifest["files"]
    for path, record in files.items():
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))
            != (record["device"], record["inode"], record["mode"])
            or info.st_size < record["offset"]
        ):
            raise OSError("checkpoint source changed during capture")


def _snapshot_from_entry(entry: dict) -> Snapshot:
    sources = entry.get("sources")
    if type(sources) is not dict or set(sources) != {"files", "directories"}:
        raise ValueError("invalid cached source manifest")
    files = sources.get("files")
    directories = sources.get("directories")
    if type(files) is not dict or type(directories) is not dict:
        raise ValueError("invalid cached source manifest")
    claims = []
    seen = set()
    for path, record in files.items():
        if (
            type(path) is not str
            or path != _real_path(path)
            or type(record) is not dict
            or set(record) != _FILE_SOURCE_FIELDS
            or path in seen
        ):
            raise ValueError("invalid cached file source")
        if (
            record.get("kind") != "file"
            or any(
                type(record.get(name)) is not int or record[name] < 0
                for name in ("device", "inode", "mode", "offset")
            )
        ):
            raise ValueError("invalid cached file cursor")
        claims.append(SourceClaim(
            path,
            "file",
            resume=ResumePoint(record["offset"]),
            device=record["device"],
            inode=record["inode"],
            mode=record["mode"],
        ))
        seen.add(path)
    for path, record in directories.items():
        if (
            type(path) is not str
            or path != _real_path(path)
            or type(record) is not dict
            or set(record) != _DIRECTORY_SOURCE_FIELDS
            or path in seen
            or type(record.get("recursive")) is not bool
        ):
            raise ValueError("invalid cached directory source")
        if record.get("kind") != "directory":
            raise ValueError("invalid cached directory source")
        claims.append(SourceClaim(path, "directory", record["recursive"]))
        seen.add(path)
    claims_tuple = _validate_claims(tuple(claims))
    payload = _decode_payload(entry.get("payload"))
    return Snapshot(entry["codec_schema"], claims_tuple, payload)


def _valid_entry_shell(entry_id: object, entry: object) -> bool:
    try:
        if (
            type(entry_id) is not str
            or type(entry) is not dict
            or set(entry) != _ENTRY_FIELDS
        ):
            return False
        provider = entry.get("provider")
        requested = entry.get("requested")
        schema = entry.get("codec_schema")
        if (
            type(provider) is not str
            or not provider
            or "\0" in provider
            or type(requested) is not str
            or not requested
            or "\0" in requested
            or requested != _real_path(requested)
            or type(schema) is not int
            or schema < 1
        ):
            return False
        expected_id = hashlib.sha256(
            (provider + "\0" + requested).encode("utf-8")
        ).hexdigest()
        if entry_id != expected_id:
            return False
        _snapshot_from_entry(entry)
    except (OSError, TypeError, ValueError, AttributeError, RecursionError):
        return False
    return True


class _RootCache:
    def __init__(self, root: str, requested: str, codec: SnapshotCodec) -> None:
        self.root = _real_path(root)
        self.requested = _real_path(requested)
        self.codec = codec
        provider = codec.provider
        schema = codec.schema_version
        if (
            type(provider) is not str
            or not provider
            or "\0" in provider
            or type(schema) is not int
            or schema < 1
        ):
            raise ValueError("invalid snapshot codec identity")
        root_id = hashlib.sha256(self.root.encode("utf-8")).hexdigest()
        self.path = _cache_dir() / "session-state-v3" / f"{root_id}.json"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.entry_id = hashlib.sha256(
            (provider + "\0" + self.requested).encode("utf-8")
        ).hexdigest()

    def _empty_document(self) -> dict:
        return {
            "format": _FORMAT,
            "version": CACHE_VERSION,
            "root": self.root,
            "entries": {},
        }

    def _read(self) -> dict | None:
        try:
            cache_info = self.path.parent.parent.lstat()
            if (
                not stat.S_ISDIR(cache_info.st_mode)
                or stat.S_IMODE(cache_info.st_mode) != 0o700
                or (
                    hasattr(os, "getuid")
                    and cache_info.st_uid != os.getuid()
                )
            ):
                return None
            parent_info = self.path.parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_IMODE(parent_info.st_mode) != 0o700
                or (
                    hasattr(os, "getuid")
                    and parent_info.st_uid != os.getuid()
                )
            ):
                return None
            info = self.path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_CACHE_BYTES
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            ):
                return None
            with self.path.open("r", encoding="utf-8") as stream:
                document = json.load(
                    stream, parse_constant=_reject_json_constant,
                )
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None
        if (
            type(document) is not dict
            or set(document) != {"format", "version", "root", "entries"}
            or document.get("format") != _FORMAT
            or document.get("version") != CACHE_VERSION
            or document.get("root") != self.root
            or type(document.get("entries")) is not dict
        ):
            return None
        return document

    @contextmanager
    def _locked(self):
        cache_directory = self.path.parent.parent
        cache_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_info = cache_directory.lstat()
        if (
            not stat.S_ISDIR(cache_info.st_mode)
            or (
                hasattr(os, "getuid")
                and cache_info.st_uid != os.getuid()
            )
        ):
            raise OSError("invalid cache directory owner or kind")
        os.chmod(cache_directory, 0o700)
        self.path.parent.mkdir(exist_ok=True, mode=0o700)
        parent_info = self.path.parent.lstat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or (
                hasattr(os, "getuid")
                and parent_info.st_uid != os.getuid()
            )
        ):
            raise OSError("invalid cache directory owner or kind")
        os.chmod(self.path.parent, 0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            lock_info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_nlink != 1
                or (
                    hasattr(os, "getuid")
                    and lock_info.st_uid != os.getuid()
                )
            ):
                raise OSError("invalid cache lock owner or kind")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self) -> SessionModel | None:
        document = self._read()
        if document is None:
            return None
        entry = document["entries"].get(self.entry_id)
        if entry is None:
            return None
        try:
            if (
                type(entry) is not dict
                or set(entry) != _ENTRY_FIELDS
                or entry.get("provider") != self.codec.provider
                or entry.get("requested") != self.requested
                or entry.get("codec_schema") != self.codec.schema_version
            ):
                raise ValueError("cached entry does not match codec")
            snapshot = _snapshot_from_entry(entry)
            model = self.codec.restore(self.requested, snapshot)
            if (
                getattr(model, "provider") != self.codec.provider
                or type(getattr(model, "nodes")) is not dict
                or type(getattr(model, "task_list")) is not dict
            ):
                raise ValueError("codec restored invalid model")
            return model
        except (OSError, TypeError, ValueError, AttributeError, RecursionError):
            self._discard(entry)
            return None

    def _retained_entries(self, document: dict) -> dict:
        return {
            entry_id: entry
            for entry_id, entry in document["entries"].items()
            if _valid_entry_shell(entry_id, entry)
        }

    def _discard(self, rejected: object) -> None:
        try:
            with self._locked():
                document = self._read()
                if document is None:
                    return
                if document["entries"].get(self.entry_id) != rejected:
                    return
                entries = self._retained_entries(document)
                entries.pop(self.entry_id, None)
                document["entries"] = entries
                self._write_document(document)
        except (OSError, TypeError, ValueError, RecursionError):
            return

    def save(self, model: SessionModel) -> bool:
        try:
            snapshot = self.codec.capture(model)
            if (
                type(snapshot) is not Snapshot
                or snapshot.schema_version != self.codec.schema_version
            ):
                raise ValueError("codec captured wrong snapshot schema")
            claims = _validate_claims(snapshot.sources)
            manifest = _capture_manifest(claims)
            if self.requested not in manifest["files"]:
                raise ValueError("requested source changed during snapshot capture")
            _fence_manifest(manifest)
            entry = {
                "provider": self.codec.provider,
                "requested": self.requested,
                "codec_schema": self.codec.schema_version,
                "sources": manifest,
                "payload": _encode_payload(snapshot.payload),
            }
            with self._locked():
                document = self._read() or self._empty_document()
                entries = self._retained_entries(document)
                entries = {
                    entry_id: retained
                    for entry_id, retained in entries.items()
                    if (
                        retained["requested"] != self.requested
                        or retained["provider"] == self.codec.provider
                    )
                }
                entries[self.entry_id] = entry
                document["entries"] = entries
                _fence_manifest(manifest)
                return self._write_document(document)
        except (
            OSError,
            TypeError,
            ValueError,
            AttributeError,
            UnicodeError,
            RecursionError,
        ):
            return False

    def _write_document(self, document: dict) -> bool:
        temporary = ""
        try:
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > _MAX_CACHE_BYTES:
                return False
            descriptor, temporary = tempfile.mkstemp(
                prefix=self.path.name + ".",
                suffix=".tmp",
                dir=self.path.parent,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = ""
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        except (OSError, TypeError, ValueError, RecursionError):
            return False
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


class CachedModel:
    def __init__(self, model: SessionModel, cache: _RootCache, restored: bool) -> None:
        self.raw_model = model
        self.cache_restored = restored
        self._cache = cache
        self._save_pending = not restored
        self._save_attempted = False
        self._opened_source_revision = _source_revision(model)

    def __getattr__(self, name):
        return getattr(self.raw_model, name)

    @property
    def provider(self) -> str:
        return self.raw_model.provider

    @property
    def path(self) -> str:
        return self.raw_model.path

    @property
    def nodes(self) -> dict[str, Node]:
        return self.raw_model.nodes

    @property
    def task_list(self) -> dict[str, list[Task]]:
        return self.raw_model.task_list

    def poll(self) -> bool:
        changed = bool(self.raw_model.poll())
        if (
            not self._save_attempted
            and not self._save_pending
            and _source_revision(self.raw_model) != self._opened_source_revision
        ):
            self._save_pending = True
        if not changed and self._save_pending:
            self._save_pending = False
            self._save_attempted = True
            self._cache.save(self.raw_model)
        return changed

    @property
    def source_revision(self) -> int:
        return _source_revision(self.raw_model)

    @property
    def visible_revision(self) -> int:
        return _visible_revision(self.raw_model)

    def observation_paths(self) -> set[str]:
        return self.raw_model.observation_paths()

    def accepts_observation(self, paths: frozenset[str]) -> bool:
        return self.raw_model.accepts_observation(paths)

    def lifecycle_identity(self) -> tuple[str, str] | None:
        identity = self.raw_model.lifecycle_identity()
        if identity is not None and (
            not isinstance(self.raw_model, LifecycleSessionModel)
            or not callable(getattr(self.raw_model, "apply_lifecycle", None))
        ):
            raise TypeError("cached lifecycle identity requires raw LifecycleSessionModel")
        return identity

    def apply_lifecycle(
        self,
        fact: LifecycleFact,
        target: str,
        status: str,
        preserve_error: bool = False,
    ) -> bool:
        changed = bool(self.raw_model.apply_lifecycle(
            fact,
            target,
            status,
            preserve_error=preserve_error,
        ))
        return changed


def open_cached_model(
    path: str,
    root: str,
    codec: SnapshotCodec,
    build: Callable[[], SessionModel],
) -> SessionModel:
    cache = _RootCache(root, path, codec)
    restored = cache.load()
    return CachedModel(restored or build(), cache, restored is not None)


def restore_cached_model(
    path: str,
    root: str,
    codec: SnapshotCodec,
) -> SessionModel | None:
    """Restore one valid cache entry without constructing a cold model."""
    cache = _RootCache(root, path, codec)
    restored = cache.load()
    return CachedModel(restored, cache, True) if restored is not None else None


def _source_revision(model: SessionModel) -> int:
    getter = getattr(provider_reload, "source_revision", None)
    if callable(getter):
        return getter(model)
    return provider_reload.reload_revision(model)


def _visible_revision(model: SessionModel) -> int:
    getter = getattr(provider_reload, "visible_revision", None)
    if callable(getter):
        return getter(model)
    return provider_reload.reload_revision(model)
