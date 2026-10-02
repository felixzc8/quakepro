"""Explicit snapshot codec for Claude transcript state."""
from __future__ import annotations

import math
import os
from dataclasses import fields

from .claude_model import TEAMS, ClaudeModel, _validate_claude_metadata
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
        raise ValueError("invalid Claude nodes")
    node_ids = set(nodes)
    if any(node.id != node_id for node_id, node in nodes.items()):
        raise ValueError("invalid Claude node ids")
    if not _string_map(
        task_list,
        lambda tasks: type(tasks) is list and all(_valid_task(task) for task in tasks),
    ):
        raise ValueError("invalid Claude tasks")
    visible = [step for node in nodes.values() for step in node.steps]
    if len({id(step) for step in visible}) != len(visible):
        raise ValueError("duplicate visible Claude step")
    if any(step.child and step.child not in node_ids for step in visible):
        raise ValueError("invalid Claude step child")
    if any(step.dispatch and step.dispatch not in node_ids for step in visible):
        raise ValueError("invalid Claude step dispatch")


def _step_map(value: object) -> bool:
    return _string_map(value, _valid_step)


def _step_list(value: object) -> bool:
    return type(value) is list and all(_valid_step(step) for step in value)


_FIELDS = {
    "path", "_offset", "_main_file_id", "_main_stat",
    "_burst",
    "_dispatch_count", "nodes", "_spawn", "_spawn_owner", "_pending_done",
    "_pending_result", "_pending_spawn_children", "_spawn_meta", "_step_by_tid",
    "_pending_tool_results", "_bg_pending", "_bg_steps", "session_dir",
    "_wf_offsets", "_tail_file_ids", "_tail_stats", "_tail_rewound",
    "_wf_name", "_wf_tool_name", "_workflow_nodes", "_meta_cache",
    "_flat_nodes", "_seen_agent_records", "_agent_source_counts", "_team_cache",
    "task_list", "_task_sig", "_task_file_cache", "provider",
    "_source_signatures", "_reload_directories", "_reload_denials",
}


def _agent_record_key(value: object) -> bool:
    return type(value) is tuple and (
        (len(value) == 2 and value[0] == "uuid" and type(value[1]) is str)
        or (
            len(value) == 4
            and value[0] == "fallback"
            and all(type(item) is str for item in value[1:])
        )
    )


def _seen_agent_identity(value: object) -> bool:
    return _agent_record_key(value) or (
        type(value) is tuple
        and len(value) == 2
        and _agent_record_key(value[0])
        and type(value[1]) is int
        and value[1] >= 0
    )


def _validate_step_aliases(state: dict) -> None:
    visible = {
        id(step): (node_id, step)
        for node_id, node in state["nodes"].items()
        for step in node.steps
    }
    visible_steps = {
        marker: step
        for marker, (_, step) in visible.items()
    }
    step_index = state["_step_by_tid"]
    if any(
        id(step) not in visible_steps or step.tid != tid
        for tid, step in step_index.items()
    ):
        raise ValueError("Claude step index must alias visible steps")
    if any(
        step.tid and step_index.get(step.tid) is not step
        for step in visible_steps.values()
    ):
        raise ValueError("visible Claude step missing from index")
    for tid, child in state["_spawn"].items():
        step = step_index.get(tid)
        if (
            not tid
            or child not in state["nodes"]
            or step is None
            or step.kind != "spawn"
            or step.child != child
            or state["_spawn_owner"].get(tid) != visible[id(step)][0]
        ):
            raise ValueError("Claude spawn index must match visible step")
    for node_id, step in visible.values():
        if (
            step.kind == "spawn"
            and step.tid
            and step.child
            and state["nodes"][step.child].kind != "workflow"
            and (
                state["_spawn"].get(step.tid) != step.child
                or state["_spawn_owner"].get(step.tid) != node_id
            )
        ):
            raise ValueError("visible Claude spawn missing from index")
    background = state["_bg_steps"]
    if (
        len({id(step) for step in background}) != len(background)
        or any(id(step) not in visible_steps or not step.bg_path for step in background)
    ):
        raise ValueError("Claude background index must alias visible steps")
    if any(
        tid not in step_index or id(step_index[tid]) not in visible_steps
        for tid in state["_bg_pending"]
    ):
        raise ValueError("Claude pending background step is missing")


def _state(model: ClaudeModel) -> dict:
    reload_directories, reload_denials = reload_acceptance_state(model)
    missing_optional = {
        _real_path(step.bg_path)
        for step in model._bg_steps
        if step.bg_path and not os.path.lexists(_real_path(step.bg_path))
    }
    wf_offsets = {
        path: offset for path, offset in model._wf_offsets.items()
        if _real_path(path) not in missing_optional
    }
    tail_file_ids = {
        path: identity for path, identity in model._tail_file_ids.items()
        if _real_path(path) not in missing_optional
    }
    tail_stats = {
        path: signature for path, signature in model._tail_stats.items()
        if _real_path(path) not in missing_optional
    }
    return {
        "path": model.path,
        "_offset": model._offset,
        "_main_file_id": model._main_file_id,
        "_main_stat": model._main_stat,
        "_burst": model._burst,
        "_dispatch_count": model._dispatch_count,
        "nodes": model.nodes,
        "_spawn": model._spawn,
        "_spawn_owner": model._spawn_owner,
        "_pending_done": model._pending_done,
        "_pending_result": model._pending_result,
        "_pending_spawn_children": model._pending_spawn_children,
        "_spawn_meta": model._spawn_meta,
        "_step_by_tid": model._step_by_tid,
        "_pending_tool_results": model._pending_tool_results,
        "_bg_pending": model._bg_pending,
        "_bg_steps": model._bg_steps,
        "session_dir": model.session_dir,
        "_wf_offsets": wf_offsets,
        "_tail_file_ids": tail_file_ids,
        "_tail_stats": tail_stats,
        "_tail_rewound": model._tail_rewound,
        "_wf_name": model._wf_name,
        "_wf_tool_name": model._wf_tool_name,
        "_workflow_nodes": model._workflow_nodes,
        "_meta_cache": model._meta_cache,
        "_flat_nodes": model._flat_nodes,
        "_seen_agent_records": model._seen_agent_records,
        "_agent_source_counts": model._agent_source_counts,
        "_team_cache": model._team_cache,
        "task_list": model.task_list,
        "_task_sig": model._task_sig,
        "_task_file_cache": model._task_file_cache,
        "provider": model.provider,
        "_source_signatures": {},
        "_reload_directories": reload_directories,
        "_reload_denials": reload_denials,
    }


def _validate_state(state: object) -> dict:
    if type(state) is not dict or set(state) != _FIELDS:
        raise ValueError("invalid Claude snapshot fields")
    scalar_types = {
        "path": str,
        "_offset": int,
        "_burst": int,
        "session_dir": str,
        "provider": str,
    }
    if any(type(state[name]) is not value_type for name, value_type in scalar_types.items()):
        raise ValueError("invalid Claude snapshot field type")
    if state["provider"] != "claude" or state["_offset"] < 0 or state["_burst"] < 0:
        raise ValueError("invalid Claude snapshot identity")
    if not valid_reload_acceptance_state(
        state["_reload_directories"], state["_reload_denials"],
    ):
        raise ValueError("invalid Claude reload acceptance")
    for name, size in (("_main_file_id", 2), ("_main_stat", 6)):
        if state[name] is not None and not _int_tuple(state[name], size):
            raise ValueError("invalid Claude source identity")
    _validate_shared_state(state["nodes"], state["task_list"])
    string_maps = (
        "_spawn", "_spawn_owner", "_pending_done", "_pending_result",
        "_pending_spawn_children", "_wf_name", "_wf_tool_name",
        "_workflow_nodes", "_flat_nodes",
    )
    if any(not _string_map(state[name], lambda value: type(value) is str)
           for name in string_maps):
        raise ValueError("invalid Claude string index")
    flat_nodes = state["_flat_nodes"]
    if (
        len(set(flat_nodes.values())) != len(flat_nodes)
        or any(
            node_id == "main"
            or node_id not in state["nodes"]
            or not (
                node_id == agent_id
                or (
                    state["nodes"][node_id].kind == "teammate"
                    and node_id.startswith("tm:")
                    and node_id.rsplit(":", 1)[-1] == agent_id
                )
            )
            for agent_id, node_id in flat_nodes.items()
        )
    ):
        raise ValueError("invalid Claude flat agent index")
    if not _string_map(
        state["_dispatch_count"],
        lambda count: type(count) is int and count >= 0,
    ):
        raise ValueError("invalid Claude dispatch count")
    if not _string_map(state["_spawn_meta"], lambda value: (
        type(value) is tuple
        and len(value) == 3
        and type(value[0]) is str
        and (value[1] is None or type(value[1]) is str)
        and type(value[2]) is str
    )):
        raise ValueError("invalid Claude spawn metadata")
    if not _step_map(state["_step_by_tid"]):
        raise ValueError("invalid Claude step index")
    if not _string_map(state["_pending_tool_results"], lambda value: (
        type(value) is tuple
        and len(value) == 2
        and all(type(item) is str for item in value)
    )):
        raise ValueError("invalid Claude pending results")
    if not _step_list(state["_bg_steps"]):
        raise ValueError("invalid Claude background steps")
    for name in ("_bg_pending", "_tail_rewound"):
        if type(state[name]) is not set or any(type(item) is not str for item in state[name]):
            raise ValueError("invalid Claude string set")
    if state["_tail_rewound"]:
        raise ValueError("transient Claude rewind cannot be restored")
    if not _string_map(
        state["_wf_offsets"],
        lambda offset: type(offset) is int and offset >= 0,
    ):
        raise ValueError("invalid Claude tail offsets")
    for name, size in (("_tail_file_ids", 2), ("_tail_stats", 6)):
        if not _string_map(state[name], lambda value, size=size: _int_tuple(value, size)):
            raise ValueError("invalid Claude tail identity")
    if not _string_map(state["_meta_cache"], lambda value: (
        type(value) is tuple
        and len(value) == 2
        and _int_tuple(value[0], 6)
        and type(value[1]) is dict
        and _json_value(value[1])
    )):
        raise ValueError("invalid Claude metadata cache")
    for _, metadata in state["_meta_cache"].values():
        _validate_claude_metadata(metadata)
    if not _string_map(state["_seen_agent_records"], lambda value: (
        type(value) is set and all(_seen_agent_identity(item) for item in value)
    )):
        raise ValueError("invalid Claude record cache")
    if not _string_map(state["_agent_source_counts"], lambda sources: (
        _string_map(sources, lambda counts: (
            type(counts) is dict
            and all(
                _agent_record_key(key)
                and type(count) is int
                and count >= 0
                for key, count in counts.items()
            )
        ))
    )):
        raise ValueError("invalid Claude source counts")
    if not _string_map(state["_team_cache"], lambda value: (
        type(value) is tuple
        and len(value) == 2
        and _int_tuple(value[0], 6)
        and type(value[1]) is set
        and all(item is None or type(item) is str for item in value[1])
    )):
        raise ValueError("invalid Claude team cache")
    if not _string_map(state["_task_sig"], lambda signature: (
        type(signature) is tuple
        and all(
            type(item) is tuple
            and len(item) == 5
            and all(type(field) is str for field in item)
            for item in signature
        )
    )):
        raise ValueError("invalid Claude task signature")
    if not _string_map(state["_task_file_cache"], lambda value: (
        type(value) is tuple
        and len(value) == 2
        and _int_tuple(value[0], 6)
        and _valid_task(value[1])
    )):
        raise ValueError("invalid Claude task cache")
    if not _string_map(
        state["_source_signatures"], lambda value: _int_tuple(value, 6),
    ):
        raise ValueError("invalid Claude source signatures")
    _validate_step_aliases(state)
    return state


def _base_files(state: dict) -> set[str]:
    files = {_real_path(state["path"])}
    files.update(_real_path(path) for path in state["_wf_offsets"])
    files.update(_real_path(path) for path in state["_meta_cache"])
    files.update(_real_path(path) for path in state["_task_file_cache"])
    files.update(
        _real_path(step.bg_path)
        for step in state["_bg_steps"]
        if step.bg_path and os.path.lexists(_real_path(step.bg_path))
    )
    for node_id in state["nodes"]:
        if node_id.startswith("team:"):
            team = node_id.split(":", 1)[1]
            team_dir = _real_path(os.path.join(TEAMS, team))
            config = _real_path(os.path.join(team_dir, "config.json"))
            if os.path.lexists(config):
                files.add(config)
    return files


def _declared_base_files(state: dict) -> set[str]:
    files = {_real_path(state["path"])}
    files.update(_real_path(path) for path in state["_wf_offsets"])
    files.update(_real_path(path) for path in state["_meta_cache"])
    files.update(_real_path(path) for path in state["_task_file_cache"])
    files.update(
        _real_path(step.bg_path)
        for step in state["_bg_steps"]
        if step.bg_path
    )
    files.update(
        _real_path(os.path.join(TEAMS, node_id.split(":", 1)[1], "config.json"))
        for node_id in state["nodes"]
        if node_id.startswith("team:")
    )
    return files


def _claims(state: dict) -> tuple[SourceClaim, ...]:
    signatures = {
        _real_path(path): signature
        for path, signature in state["_source_signatures"].items()
    }
    offsets = {_real_path(state["path"]): state["_offset"]}
    offsets.update({
        _real_path(path): offset for path, offset in state["_wf_offsets"].items()
    })
    directories = {
        _real_path(path): True
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
        + [
            SourceClaim(path, "directory", recursive=directories[path])
            for path in sorted(directories)
        ]
    )


def _flat_metadata_paths(state: dict) -> set[str]:
    root = _real_path(os.path.join(state["session_dir"], "subagents"))
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return set()
    return {
        _real_path(os.path.join(root, name))
        for name in names
        if name.startswith("agent-") and name.endswith(".meta.json")
    }


def _stat_signature(path: str) -> tuple[int, ...]:
    return _stamp_tuple(_regular_file_stamp(path))


def _capture_source_signatures(state: dict) -> dict[str, tuple[int, ...]]:
    files = _base_files(state)
    return {path: _stat_signature(path) for path in sorted(files)}


def _validate_parsed_main_signature(state: dict) -> None:
    captured = state["_source_signatures"].get(_real_path(state["path"]))
    parsed = state["_main_stat"]
    if captured is None or parsed is None or state["_main_file_id"] != parsed[:2]:
        raise ValueError("Claude parsed main source is missing")
    if captured[3] == parsed[3]:
        coherent = captured == parsed
    else:
        coherent = captured[3] > parsed[3] and captured[:3] == parsed[:3]
    if not coherent:
        raise ValueError("Claude parsed main source changed before snapshot capture")


def _validate_claim_set(snapshot: Snapshot, state: dict) -> None:
    expected = {claim.path: claim for claim in _claims(state)}
    actual = {claim.path: claim for claim in snapshot.sources}
    if actual != expected or len(actual) != len(snapshot.sources):
        raise ValueError("Claude snapshot source claims do not match state")


def _validate_cached_signatures(state: dict) -> None:
    signatures = {
        _real_path(path): signature
        for path, signature in state["_source_signatures"].items()
    }
    if len(signatures) != len(state["_source_signatures"]):
        raise ValueError("duplicate Claude source signature")
    base_files = _declared_base_files(state)
    metadata_root = _real_path(os.path.join(state["session_dir"], "subagents"))
    for path in set(signatures) - base_files:
        name = os.path.basename(path)
        if (
            os.path.dirname(path) != metadata_root
            or not name.startswith("agent-")
            or not name.endswith(".meta.json")
        ):
            raise ValueError("invalid Claude metadata source")
    _validate_parsed_main_signature(state)
    for cache_name in ("_meta_cache", "_task_file_cache"):
        for path, cached in state[cache_name].items():
            if cached[0] != signatures.get(_real_path(path)):
                raise ValueError("Claude cached source signature mismatch")
    for team, cached in state["_team_cache"].items():
        path = _real_path(os.path.join(TEAMS, team, "config.json"))
        if cached[0] != signatures.get(path):
            raise ValueError("Claude cached team signature mismatch")
    for team, signature in state["_task_sig"].items():
        tasks = state["task_list"].get(team)
        if tasks is None or signature != tuple(
            (task.id, task.status, task.owner, task.subject, task.description)
            for task in tasks
        ):
            raise ValueError("Claude task signature mismatch")


class ClaudeSnapshotCodec:
    provider = "claude"
    schema_version = 4

    def capture(self, model) -> Snapshot:
        if type(model) is not ClaudeModel or model.provider != self.provider:
            raise ValueError("wrong provider for Claude snapshot")
        state = _state(model)
        state["_source_signatures"] = _capture_source_signatures(state)
        state = _validate_state(state)
        _validate_cached_signatures(state)
        return Snapshot(self.schema_version, _claims(state), state)

    def restore(self, requested_path, snapshot):
        if snapshot.schema_version != self.schema_version:
            raise ValueError("wrong Claude snapshot schema")
        state = _validate_state(snapshot.payload)
        if _real_path(state["path"]) != _real_path(requested_path):
            raise ValueError("Claude snapshot requested path mismatch")
        _validate_claim_set(snapshot, state)
        _validate_cached_signatures(state)
        model = ClaudeModel.__new__(ClaudeModel)
        model.provider = state["provider"]
        model.path = state["path"]
        model._offset = state["_offset"]
        model._main_file_id = state["_main_file_id"]
        model._main_stat = state["_main_stat"]
        model._burst = state["_burst"]
        model._dispatch_count = state["_dispatch_count"]
        model.nodes = state["nodes"]
        model._spawn = state["_spawn"]
        model._spawn_owner = state["_spawn_owner"]
        model._pending_done = state["_pending_done"]
        model._pending_result = state["_pending_result"]
        model._pending_spawn_children = state["_pending_spawn_children"]
        model._spawn_meta = state["_spawn_meta"]
        model._step_by_tid = state["_step_by_tid"]
        model._pending_tool_results = state["_pending_tool_results"]
        model._bg_pending = state["_bg_pending"]
        model._bg_steps = state["_bg_steps"]
        model.session_dir = state["session_dir"]
        model._wf_offsets = state["_wf_offsets"]
        model._tail_file_ids = state["_tail_file_ids"]
        model._tail_stats = state["_tail_stats"]
        model._tail_rewound = state["_tail_rewound"]
        model._wf_name = state["_wf_name"]
        model._wf_tool_name = state["_wf_tool_name"]
        model._workflow_nodes = state["_workflow_nodes"]
        model._meta_cache = state["_meta_cache"]
        model._flat_nodes = state["_flat_nodes"]
        model._seen_agent_records = state["_seen_agent_records"]
        model._agent_source_counts = state["_agent_source_counts"]
        model._team_cache = state["_team_cache"]
        model.task_list = state["task_list"]
        model._task_sig = state["_task_sig"]
        model._task_file_cache = state["_task_file_cache"]
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
