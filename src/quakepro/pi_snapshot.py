"""Explicit snapshot codec for pi session state."""
from __future__ import annotations

import math
import os
from dataclasses import fields

from .pi_model import (
    PiModel,
    _async_child_identity,
    _pi_header_identity,
    _valid_pi_header,
)
from .provider_reload import (
    reload_acceptance_state,
    seed_reload_acceptance,
    valid_reload_acceptance_state,
)
from .session_cache import (
    ResumePoint,
    Snapshot,
    SourceClaim,
)
from .session_model import Node, Step, Task
from .source_cohort import _regular_file_stamp, _stamp_tuple


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
        raise ValueError("invalid pi nodes")
    node_ids = set(nodes)
    if any(node.id != node_id for node_id, node in nodes.items()):
        raise ValueError("invalid pi node ids")
    if not _string_map(
        task_list,
        lambda tasks: type(tasks) is list and all(_valid_task(task) for task in tasks),
    ):
        raise ValueError("invalid pi tasks")
    visible = [step for node in nodes.values() for step in node.steps]
    if (
        len({id(step) for step in visible}) != len(visible)
        or any(step.child and step.child not in node_ids for step in visible)
        or any(step.dispatch and step.dispatch not in node_ids for step in visible)
    ):
        raise ValueError("invalid pi step graph")


def _step_map(value: object) -> bool:
    return _string_map(value, _valid_step)


_FIELDS = {
    "path", "_header", "_header_identity", "cwd",
    "nodes", "task_list", "_offset", "_carry",
    "_source_signature", "_steps", "_children",
    "_dispatches", "_async_runs", "_async_sessions", "_async_seen",
    "_changed", "_reload_directories", "_reload_denials",
}


def _state(model: PiModel) -> dict:
    reload_directories, reload_denials = reload_acceptance_state(model)
    return {
        "path": model.path,
        "_header": model._header,
        "_header_identity": _pi_header_identity(model._header),
        "cwd": model.cwd,
        "nodes": model.nodes,
        "task_list": model.task_list,
        "_offset": model._offset,
        "_carry": model._carry,
        "_source_signature": model._source_signature,
        "_steps": model._steps,
        "_children": model._children,
        "_dispatches": model._dispatches,
        "_async_runs": model._async_runs,
        "_async_sessions": model._async_sessions,
        "_async_seen": model._async_seen,
        "_changed": model._changed,
        "_reload_directories": reload_directories,
        "_reload_denials": reload_denials,
    }


def _validate_state(state: object) -> dict:
    if type(state) is not dict or set(state) != _FIELDS:
        raise ValueError("invalid pi snapshot fields")
    if (
        type(state["path"]) is not str
        or type(state["_header"]) is not dict
        or not _json_value(state["_header"])
        or not _valid_pi_header(state["_header"])
        or type(state["cwd"]) is not str
        or state["cwd"] != str(state["_header"].get("cwd") or "")
        or state["_header_identity"] != _pi_header_identity(state["_header"])
        or type(state["_offset"]) is not int
        or state["_offset"] < 0
        or type(state["_carry"]) is not bytes
        or state["_carry"] != b""
        or not _int_tuple(state["_source_signature"], 6)
        or type(state["_children"]) is not int
        or state["_children"] < 0
        or type(state["_dispatches"]) is not int
        or state["_dispatches"] < 0
        or not _string_map(state["_async_sessions"], lambda value: type(value) is str)
        or not _string_map(state["_async_seen"], lambda value: (
            type(value) is set and all(type(item) is str for item in value)
        ))
        or type(state["_changed"]) is not bool
        or not _step_map(state["_steps"])
        or not valid_reload_acceptance_state(
            state["_reload_directories"], state["_reload_denials"],
        )
    ):
        raise ValueError("invalid pi snapshot state")
    if state["_changed"]:
        raise ValueError("transient pi change cannot be restored")
    _validate_shared_state(state["nodes"], state["task_list"])
    if not _valid_async_state(state):
        raise ValueError("invalid pi async state")
    allocated = {node_id for node_id in state["nodes"] if node_id.startswith("pi:")}
    expected = {"pi:%d" % index for index in range(1, state["_children"] + 1)}
    if allocated != expected:
        raise ValueError("pi child counter does not match allocated nodes")
    visible = {
        id(step)
        for node in state["nodes"].values()
        for step in node.steps
    }
    if any(
        id(step) not in visible or step.tid != tid
        for tid, step in state["_steps"].items()
    ):
        raise ValueError("pi step index must alias visible steps")
    if any(
        step.tid and state["_steps"].get(step.tid) is not step
        for step in state["nodes"]["main"].steps
    ):
        raise ValueError("visible pi step missing from index")
    return state


def _valid_async_state(state: dict) -> bool:
    nodes = state["nodes"]
    steps = state["_steps"]
    runs = state["_async_runs"]
    if not _string_map(runs):
        return False
    for run_id, run in runs.items():
        if (
            type(run) is not dict
            or set(run) != {
                "dir", "tid", "seen_status", "terminal", "children", "child_status_dirs",
            }
            or type(run["dir"]) is not str
            or run["dir"] != _real_path(run["dir"])
            or PiModel._async_dir(run_id, run["dir"]) != run["dir"]
            or type(run["tid"]) is not str
            or run["tid"] not in steps
            or type(run["seen_status"]) is not bool
            or type(run["terminal"]) is not bool
            or type(run["children"]) is not list
            or any(type(node_id) is not str or node_id not in nodes for node_id in run["children"])
            or type(run["child_status_dirs"]) is not list
            or any(
                type(path) is not str
                or os.path.dirname(path) != os.path.dirname(run["dir"])
                or PiModel._async_dir(os.path.basename(path), path) != path
                for path in run["child_status_dirs"]
            )
        ):
            return False
    sessions = state["_async_sessions"]
    seen = state["_async_seen"]
    if set(seen) != set(sessions):
        return False
    return all(
        path == _real_path(path)
        and path.endswith(".jsonl")
        and _async_child_identity(state["path"], path) is not None
        and node_id in nodes
        and nodes[node_id].kind == "agent"
        for path, node_id in sessions.items()
    )


def _int_tuple(value: object, size: int) -> bool:
    return type(value) is tuple and len(value) == size and all(
        type(item) is int for item in value
    )


def _claims(state: dict) -> tuple[SourceClaim, ...]:
    path = _real_path(state["path"])
    signature = state["_source_signature"]
    return (SourceClaim(
        path,
        "file",
        resume=ResumePoint(state["_offset"]),
        device=signature[0],
        inode=signature[1],
        mode=signature[2],
    ),)


def _stat_signature(path: str) -> tuple[int, ...]:
    return _stamp_tuple(_regular_file_stamp(path))


def _coherent_signature(
    earlier: tuple[int, ...], later: tuple[int, ...], resumable: bool,
) -> bool:
    if later[3] == earlier[3]:
        return later == earlier
    return later[3] > earlier[3] and resumable and later[:3] == earlier[:3]


class PiSnapshotCodec:
    provider = "pi"
    schema_version = 7

    def capture(self, model) -> Snapshot:
        if type(model) is not PiModel or model.provider != self.provider:
            raise ValueError("wrong provider for pi snapshot")
        state = _validate_state(_state(model))
        if not _coherent_signature(
            state["_source_signature"],
            _stat_signature(_real_path(state["path"])),
            True,
        ):
            raise ValueError("pi parsed source changed before snapshot capture")
        return Snapshot(self.schema_version, _claims(state), state)

    def restore(self, requested_path, snapshot):
        if snapshot.schema_version != self.schema_version:
            raise ValueError("wrong pi snapshot schema")
        state = _validate_state(snapshot.payload)
        if _real_path(state["path"]) != _real_path(requested_path):
            raise ValueError("pi snapshot requested path mismatch")
        expected = {claim.path: claim for claim in _claims(state)}
        actual = {claim.path: claim for claim in snapshot.sources}
        if actual != expected or len(actual) != len(snapshot.sources):
            raise ValueError("pi source claims do not match state")

        model = PiModel.__new__(PiModel)
        model.path = state["path"]
        model._header = state["_header"]
        model._header_digest = b""
        model.cwd = state["cwd"]
        model.nodes = state["nodes"]
        model.task_list = state["task_list"]
        model._offset = state["_offset"]
        model._carry = state["_carry"]
        model._source_signature = state["_source_signature"]
        model._steps = state["_steps"]
        model._children = state["_children"]
        model._dispatches = state["_dispatches"]
        model._async_runs = state["_async_runs"]
        model._async_sessions = state["_async_sessions"]
        model._async_seen = state["_async_seen"]
        model._changed = state["_changed"]
        model._replaying = False
        _seed_checkpoint_cursors(model, snapshot, state)
        return model


def _seed_checkpoint_cursors(model, snapshot: Snapshot, state: dict) -> None:
    claim = next(item for item in snapshot.sources if item.kind == "file")
    seed_reload_acceptance(model, ((
        claim.path,
        state["_source_signature"],
        claim.resume.offset,
        b"",
    ),), state["_reload_directories"], state["_reload_denials"])
