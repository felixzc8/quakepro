"""Explicit snapshot codec for Codex rollout state."""
from __future__ import annotations

import math
import os
from dataclasses import fields

from .codex_model import CodexModel
from .provider_reload import (
    reload_acceptance_state,
    seed_reload_acceptance,
    valid_reload_acceptance_state,
)
from .session_identity import _valid_codex_meta
from .session_cache import (
    ResumePoint,
    Snapshot,
    SourceClaim,
)
from .session_model import Node, Step, Task
from .source_cohort import DirectoryStamp, _regular_file_stamp, _stamp_tuple


def _real_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


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
    return type(values) is dict and all(
        type(key) is str and (valid_value is None or valid_value(value))
        for key, value in values.items()
    )


def _int_tuple(value: object, size: int) -> bool:
    return type(value) is tuple and len(value) == size and all(
        type(item) is int for item in value
    )


def _valid_step(value: object) -> bool:
    return type(value) is Step and (
        all(
            type(getattr(value, field.name)) is (float if field.name == "ts" else str)
            for field in fields(Step)
        )
        and math.isfinite(value.ts)
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
        and all(type(item) is str for item in value.skills)
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
        raise ValueError("invalid Codex nodes")
    if any(node.id != node_id for node_id, node in nodes.items()):
        raise ValueError("invalid Codex node ids")
    if not _string_map(
        task_list,
        lambda tasks: type(tasks) is list and all(_valid_task(task) for task in tasks),
    ):
        raise ValueError("invalid Codex tasks")
    visible = [step for node in nodes.values() for step in node.steps]
    if len({id(step) for step in visible}) != len(visible):
        raise ValueError("duplicate visible Codex step")


def _step_map(value: object) -> bool:
    return _string_map(value, _valid_step)


def _step_list(value: object) -> bool:
    return type(value) is list and all(_valid_step(step) for step in value)


_FIELDS = {
    "_requested_path", "_sessions", "_pending_candidates", "_metadata", "path",
    "_root_meta", "_group_id", "_root_thread", "_root_resolved", "nodes",
    "task_list", "_states", "_thread_nodes", "_steps", "_pending_outputs",
    "_spawn_steps", "_spawn_threads", "_dispatch_spawns", "_known_paths",
    "_irrelevant_paths", "_excluded_threads", "_directory_stamps", "_initial_changed",
    "_source_signatures", "_reload_directories", "_reload_denials",
}
_FILE_FIELDS = {
    "path", "meta", "thread_id", "node_id", "ready", "offset", "carry",
    "source_signature", "saw_trigger", "pending_started",
    "pending_context", "turn_id", "last_text", "last_text_phase",
    "last_text_origin", "open_dispatch", "open_spawns", "dispatch_count",
}


def _lower_file_state(value) -> dict:
    return {
        "path": value.path,
        "meta": value.meta,
        "thread_id": value.thread_id,
        "node_id": value.node_id,
        "ready": value.ready,
        "offset": value.offset,
        "carry": value.carry,
        "source_signature": value.source_signature,
        "saw_trigger": value.saw_trigger,
        "pending_started": value.pending_started,
        "pending_context": value.pending_context,
        "turn_id": value.turn_id,
        "last_text": value.last_text,
        "last_text_phase": value.last_text_phase,
        "last_text_origin": value.last_text_origin,
        "open_dispatch": value.open_dispatch,
        "open_spawns": value.open_spawns,
        "dispatch_count": value.dispatch_count,
    }


def _validate_step_aliases(state: dict) -> None:
    visible = {
        id(step): step
        for node in state["nodes"].values()
        for step in node.steps
    }
    owners = {
        id(step): node_id
        for node_id, node in state["nodes"].items()
        for step in node.steps
    }
    if any(
        id(step) not in visible or step.tid != call_id
        for call_id, step in state["_steps"].items()
    ):
        raise ValueError("Codex step index must alias visible steps")
    if any(
        step.tid and state["_steps"].get(step.tid) is not step
        for step in visible.values()
    ):
        raise ValueError("visible Codex step missing from index")
    if any(
        id(step) not in visible or step.kind != "spawn"
        for step in state["_spawn_steps"].values()
    ):
        raise ValueError("Codex spawn index must alias visible steps")
    if any(
        step.kind == "spawn"
        and step.tid
        and state["_spawn_steps"].get(step.tid) is not step
        for step in visible.values()
    ):
        raise ValueError("visible Codex spawn missing from spawn index")
    for key, step in state["_spawn_steps"].items():
        if key == step.tid:
            if state["_steps"].get(key) is not step:
                raise ValueError("Codex spawn and step indexes disagree")
            continue
        if state["_spawn_threads"].get(step.tid) != key:
            raise ValueError("invalid Codex spawn thread alias")
    for event_id, thread_id in state["_spawn_threads"].items():
        step = state["_spawn_steps"].get(event_id)
        if (
            step is None
            or state["_spawn_steps"].get(thread_id) is not step
            or state["_thread_nodes"].get(thread_id) != step.child
        ):
            raise ValueError("Codex spawn thread index mismatch")
    for dispatch_id, steps in state["_dispatch_spawns"].items():
        expected = ""
        first = steps[0] if steps else None
        owner = owners.get(id(first)) if first is not None else None
        if first is not None and owner is not None:
            position = next(
                index
                for index, item in enumerate(state["nodes"][owner].steps, 1)
                if item is first
            )
            expected = f"dispatch:{owner}:{first.tid or position}"
        if (
            not steps
            or len({id(step) for step in steps}) != len(steps)
            or any(
                id(step) not in visible or step.dispatch != dispatch_id
                for step in steps
            )
            or dispatch_id != expected
            or (dispatch_id not in state["nodes"] and len(steps) != 1)
        ):
            raise ValueError("Codex dispatch index must alias visible steps")
    if any(
        step.dispatch
        and all(
            indexed is not step
            for indexed in state["_dispatch_spawns"].get(step.dispatch, [])
        )
        for step in visible.values()
        if step.kind == "spawn"
    ):
        raise ValueError("visible Codex spawn missing from dispatch index")
    for file_state in state["_states"].values():
        dispatch_id = file_state["open_dispatch"]
        open_steps = file_state["open_spawns"]
        if (
            len({id(step) for step in open_steps}) != len(open_steps)
            or any(
                id(step) not in visible or step.dispatch != dispatch_id
                for step in open_steps
            )
        ):
            raise ValueError("Codex open dispatch must alias visible steps")
        if dispatch_id:
            indexed = state["_dispatch_spawns"].get(dispatch_id, [])
            if any(all(step is not item for item in indexed) for step in open_steps):
                raise ValueError("Codex open dispatch index mismatch")
    for step in visible.values():
        indexed = state["_dispatch_spawns"].get(step.dispatch, [])
        if (
            step.dispatch
            and step.dispatch not in state["nodes"]
            and not (
                step.kind == "spawn"
                and len(indexed) == 1
                and indexed[0] is step
            )
        ):
            raise ValueError("Codex unresolved dispatch is not a singleton")


def _state(model: CodexModel) -> dict:
    reload_directories, reload_denials = reload_acceptance_state(model)
    return {
        "_requested_path": model._requested_path,
        "_sessions": model._sessions,
        "_pending_candidates": model._pending_candidates,
        "_metadata": model._metadata,
        "path": model.path,
        "_root_meta": model._root_meta,
        "_group_id": model._group_id,
        "_root_thread": model._root_thread,
        "_root_resolved": model._root_resolved,
        "nodes": model.nodes,
        "task_list": model.task_list,
        "_states": {
            path: _lower_file_state(value) for path, value in model._states.items()
        },
        "_thread_nodes": model._thread_nodes,
        "_steps": model._steps,
        "_pending_outputs": model._pending_outputs,
        "_spawn_steps": model._spawn_steps,
        "_spawn_threads": model._spawn_threads,
        "_dispatch_spawns": model._dispatch_spawns,
        "_known_paths": model._known_paths,
        "_irrelevant_paths": model._irrelevant_paths,
        "_excluded_threads": model._excluded_threads,
        "_directory_stamps": {
            path: stamp.entries for path, stamp in model._directory_stamps.items()
        },
        "_initial_changed": model._initial_changed,
        "_source_signatures": {},
        "_reload_directories": reload_directories,
        "_reload_denials": reload_denials,
    }


def _valid_file_state(value: object, path: str, node_ids: set[str]) -> bool:
    if type(value) is not dict or set(value) != _FILE_FIELDS:
        return False
    return (
        type(value["path"]) is str
        and value["path"] == path
        and type(value["meta"]) is dict
        and _json_value(value["meta"])
        and _valid_codex_meta(value["meta"])
        and type(value["thread_id"]) is str
        and type(value["node_id"]) is str
        and value["node_id"] in node_ids
        and type(value["ready"]) is bool
        and type(value["offset"]) is int
        and value["offset"] >= 0
        and type(value["carry"]) is bytes
        and value["carry"] == b""
        and _int_tuple(value["source_signature"], 6)
        and type(value["saw_trigger"]) is bool
        and (
            value["pending_started"] is None
            or (
                type(value["pending_started"]) is tuple
                and len(value["pending_started"]) == 2
                and type(value["pending_started"][0]) is dict
                and _json_value(value["pending_started"][0])
                and type(value["pending_started"][1]) is float
                and math.isfinite(value["pending_started"][1])
            )
        )
        and (
            value["pending_context"] is None
            or (
                type(value["pending_context"]) is dict
                and _json_value(value["pending_context"])
            )
        )
        and type(value["turn_id"]) is str
        and type(value["last_text"]) is str
        and type(value["last_text_phase"]) is str
        and type(value["last_text_origin"]) is str
        and type(value["open_dispatch"]) is str
        and _step_list(value["open_spawns"])
        and type(value["dispatch_count"]) is int
        and value["dispatch_count"] >= 0
    )


def _validate_state(state: object) -> dict:
    if type(state) is not dict or set(state) != _FIELDS:
        raise ValueError("invalid Codex snapshot fields")
    for name in ("_requested_path", "_sessions", "path", "_group_id", "_root_thread"):
        if type(state[name]) is not str:
            raise ValueError("invalid Codex string state")
    if type(state["_root_resolved"]) is not bool or type(state["_initial_changed"]) is not bool:
        raise ValueError("invalid Codex boolean state")
    if state["_initial_changed"]:
        raise ValueError("transient Codex change cannot be restored")
    if not valid_reload_acceptance_state(
        state["_reload_directories"], state["_reload_denials"],
    ):
        raise ValueError("invalid Codex reload acceptance")
    _validate_shared_state(state["nodes"], state["task_list"])
    node_ids = set(state["nodes"])
    if not _string_map(
        state["_pending_candidates"], lambda value: _int_tuple(value, 6)
    ):
        raise ValueError("invalid Codex candidate cache")
    if state["_pending_candidates"]:
        raise ValueError("pending Codex candidate cannot be restored")
    if not _string_map(
        state["_metadata"],
        lambda value: _json_value(value) and _valid_codex_meta(value),
    ) or not _json_value(state["_root_meta"]) or not _valid_codex_meta(
        state["_root_meta"]
    ):
        raise ValueError("invalid Codex metadata")
    if (
        state["_group_id"]
        != (state["_root_meta"].get("session_id") or state["_root_meta"]["id"])
        or state["_root_thread"] != state["_root_meta"]["id"]
    ):
        raise ValueError("invalid Codex root identity")
    if not _string_map(
        state["_states"],
        lambda value: type(value) is dict,
    ) or any(
        not _valid_file_state(value, path, node_ids)
        for path, value in state["_states"].items()
    ):
        raise ValueError("invalid Codex file states")
    if type(state["_known_paths"]) is not set or set(state["_states"]) != state["_known_paths"]:
        raise ValueError("invalid Codex known paths")
    if not _string_map(
        state["_thread_nodes"],
        lambda node_id: type(node_id) is str and node_id in node_ids,
    ):
        raise ValueError("invalid Codex thread index")
    if any(
        node_id != ("main" if thread_id == state["_root_thread"] else "cx:" + thread_id)
        for thread_id, node_id in state["_thread_nodes"].items()
    ) or state["_thread_nodes"].get(state["_root_thread"]) != "main":
        raise ValueError("invalid Codex thread node mapping")
    if any(
        state["_thread_nodes"].get(value["thread_id"]) != value["node_id"]
        for value in state["_states"].values()
    ):
        raise ValueError("invalid Codex file thread mapping")
    for name in ("_steps", "_spawn_steps"):
        if not _step_map(state[name]):
            raise ValueError("invalid Codex step index")
    if not _string_map(state["_pending_outputs"], lambda value: (
        type(value) is tuple
        and len(value) == 2
        and all(type(item) is str for item in value)
    )):
        raise ValueError("invalid Codex pending output")
    if not _string_map(state["_spawn_threads"], lambda value: type(value) is str):
        raise ValueError("invalid Codex spawn threads")
    if not _string_map(state["_dispatch_spawns"], _step_list):
        raise ValueError("invalid Codex dispatch index")
    for name in ("_known_paths", "_excluded_threads"):
        if type(state[name]) is not set or any(type(item) is not str for item in state[name]):
            raise ValueError("invalid Codex string set")
    if not _string_map(
        state["_irrelevant_paths"], lambda value: _int_tuple(value, 6)
    ):
        raise ValueError("invalid Codex irrelevant source cache")
    if not _string_map(state["_directory_stamps"], lambda value: (
        type(value) is tuple
        and all(
            type(entry) is tuple
            and len(entry) == 2
            and all(type(item) is str for item in entry)
            for entry in value
        )
    )):
        raise ValueError("invalid Codex directory stamps")
    if not _string_map(
        state["_source_signatures"], lambda value: _int_tuple(value, 6),
    ):
        raise ValueError("invalid Codex source signatures")
    _validate_step_aliases(state)
    return state


def _claims(state: dict) -> tuple[SourceClaim, ...]:
    signatures = {
        _real_path(path): signature
        for path, signature in state["_source_signatures"].items()
    }
    offsets = {
        _real_path(value["path"]): value["offset"]
        for value in state["_states"].values()
    }
    directories = {
        _real_path(path)
        for path, _entries in (state["_reload_directories"] or ())
    }
    return tuple(
        [
            SourceClaim(
                path,
                "file",
                resume=ResumePoint(offsets.get(path, signature[3])),
                device=signature[0],
                inode=signature[1],
                mode=signature[2],
            )
            for path, signature in sorted(signatures.items())
        ]
        + [SourceClaim(path, "directory", recursive=True) for path in sorted(directories)]
    )


def _stat_signature(path: str) -> tuple[int, ...]:
    return _stamp_tuple(_regular_file_stamp(path))


def _capture_source_signatures(
    claims: tuple[SourceClaim, ...],
) -> dict[str, tuple[int, ...]]:
    return {
        claim.path: _stat_signature(claim.path)
        for claim in claims
        if claim.kind == "file"
    }


def _validate_parsed_source_signatures(state: dict) -> None:
    signatures = {
        _real_path(path): signature
        for path, signature in state["_source_signatures"].items()
    }
    for value in state["_states"].values():
        path = _real_path(value["path"])
        captured = signatures.get(path)
        parsed = value["source_signature"]
        if captured is None or not _coherent_signature(parsed, captured, True):
            raise ValueError("Codex parsed source changed before snapshot capture")


def _coherent_signature(
    earlier: tuple[int, ...], later: tuple[int, ...], resumable: bool,
) -> bool:
    if later[3] == earlier[3]:
        return later == earlier
    return later[3] > earlier[3] and resumable and later[:3] == earlier[:3]


def _rebuild_file_state(value: dict):
    from .codex_model import _FileState

    return _FileState(
        path=value["path"],
        meta=value["meta"],
        thread_id=value["thread_id"],
        node_id=value["node_id"],
        ready=value["ready"],
        offset=value["offset"],
        carry=value["carry"],
        source_signature=value["source_signature"],
        saw_trigger=value["saw_trigger"],
        pending_started=value["pending_started"],
        pending_context=value["pending_context"],
        turn_id=value["turn_id"],
        last_text=value["last_text"],
        last_text_phase=value["last_text_phase"],
        last_text_origin=value["last_text_origin"],
        open_dispatch=value["open_dispatch"],
        open_spawns=value["open_spawns"],
        dispatch_count=value["dispatch_count"],
    )


class CodexSnapshotCodec:
    provider = "codex"
    schema_version = 6

    def capture(self, model) -> Snapshot:
        if type(model) is not CodexModel or model.provider != self.provider:
            raise ValueError("wrong provider for Codex snapshot")
        state = _state(model)
        source_claims = tuple(
            SourceClaim(_real_path(path), "file")
            for path in sorted(state["_known_paths"])
        )
        state["_source_signatures"] = _capture_source_signatures(source_claims)
        state = _validate_state(state)
        _validate_parsed_source_signatures(state)
        return Snapshot(self.schema_version, _claims(state), state)

    def restore(self, requested_path, snapshot):
        if snapshot.schema_version != self.schema_version:
            raise ValueError("wrong Codex snapshot schema")
        state = _validate_state(snapshot.payload)
        if _real_path(state["_requested_path"]) != _real_path(requested_path):
            raise ValueError("Codex snapshot requested path mismatch")
        expected = {claim.path: claim for claim in _claims(state)}
        actual = {claim.path: claim for claim in snapshot.sources}
        if actual != expected or len(actual) != len(snapshot.sources):
            raise ValueError("Codex source claims do not match state")
        _validate_parsed_source_signatures(state)

        model = CodexModel.__new__(CodexModel)
        model._requested_path = state["_requested_path"]
        model._sessions = state["_sessions"]
        model._pending_candidates = state["_pending_candidates"]
        model._metadata = state["_metadata"]
        model.path = state["path"]
        model._root_meta = state["_root_meta"]
        model._group_id = state["_group_id"]
        model._root_thread = state["_root_thread"]
        model._root_resolved = state["_root_resolved"]
        model.nodes = state["nodes"]
        model.task_list = state["task_list"]
        model._states = {
            path: _rebuild_file_state(value)
            for path, value in state["_states"].items()
        }
        model._thread_nodes = state["_thread_nodes"]
        model._steps = state["_steps"]
        model._pending_outputs = state["_pending_outputs"]
        model._spawn_steps = state["_spawn_steps"]
        model._spawn_threads = state["_spawn_threads"]
        model._dispatch_spawns = state["_dispatch_spawns"]
        model._known_paths = state["_known_paths"]
        model._irrelevant_paths = state["_irrelevant_paths"]
        model._excluded_threads = state["_excluded_threads"]
        model._directory_stamps = {
            path: DirectoryStamp(os.path.abspath(path), entries)
            for path, entries in state["_directory_stamps"].items()
        }
        model._initial_changed = state["_initial_changed"]
        model._replaying = False
        _seed_checkpoint_cursors(model, snapshot, state)
        return model


def _seed_checkpoint_cursors(model, snapshot: Snapshot, state: dict) -> None:
    signatures = {
        _real_path(path): signature
        for path, signature in state["_source_signatures"].items()
    }
    seed_reload_acceptance(model, (
        (
            claim.path,
            signatures[claim.path],
            claim.resume.offset,
            b"",
        )
        for claim in snapshot.sources
        if claim.kind == "file"
    ), state["_reload_directories"], state["_reload_denials"])
