"""QuakePro data model: turn a Claude Code session JSONL into a live agent tree.

Sources (all append-only JSON/JSONL on disk, no API needed):
  ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl   the transcript
    type=assistant  main-loop turns; tool_use blocks named Task/Agent/Workflow are agent nodes
    type=progress   data.type=agent_progress: one sub-agent's private substream
                    top-level toolUseID    -> the tool_use that spawned this agent
                    top-level parentToolUseID -> for agents that spawn agents
                    data.agentId           -> stable short id, one per agent
                    data.prompt            -> the agent's task
                    data.message           -> one step (assistant turn / tool_use / tool_result)
"""
from __future__ import annotations
import glob
import json
import os
import re
from dataclasses import dataclass

from . import skill_mode
from . import workflow
from .batch_labels import batch_label, preferred_goal
from .batch_model import sync_batch_statuses
from .provider_reload import (
    CapturedCohort,
    DirectoryRule,
    ReloadPlan,
    advance_visible_revision as _advance_visible_revision,
    poll_append,
    source_revision as _source_revision,
    visible_revision as _visible_revision,
)
from .provider_schema import (
    optional_exact_type as _optional_exact_type,
    optional_type as _optional_type,
    validate_recognizer_inputs,
)
from .session_model import (
    BODY_CLIP,
    DETAIL_CLIP,
    RESULT_CLIP,
    LifecycleFact,
    Node,
    Step,
    Task,
    clip_text,
    one_line,
    parse_timestamp,
)
from .source_cohort import _finite_json_value

# Honor Claude Code's own config-dir override so QuakePro reads the same tree.
CONFIG = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS = os.path.join(CONFIG, "projects")
TEAMS = os.path.join(CONFIG, "teams")
TASKS = os.path.join(CONFIG, "tasks")   # shared team task list: tasks/<team>/<id>.json
SPAWN_TOOLS = {"Task", "Agent", "Workflow"}
def _claude_session_relevant(name: str, kind: str) -> bool:
    return kind == "directory" or name.endswith((".jsonl", ".meta.json"))


def _claude_task_relevant(name: str, _kind: str) -> bool:
    return name.endswith(".json")


def _claude_team_relevant(name: str, _kind: str) -> bool:
    return name == "config.json"


def _claude_all_team_relevant(name: str, kind: str) -> bool:
    return kind == "directory" or name == "config.json"


def _claude_all_task_relevant(name: str, kind: str) -> bool:
    return kind == "directory" or name.endswith(".json")


def latest_session(project_filter: str | None = None) -> str | None:
    """Newest .jsonl across all project dirs (optionally filtered by path substring)."""
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    if project_filter:
        files = [f for f in files if project_filter in f]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    out.append(c.get("text", ""))
                elif c.get("type") == "tool_result":
                    out.append(_text_of(c.get("content")))
        return "\n".join(out)
    return ""


def _claude_text(value: object) -> bool:
    if type(value) is str:
        return True
    if type(value) is not list:
        return False
    for block in value:
        if type(block) is not dict or not _optional_type(block, "type", str):
            return False
        if block.get("type") == "text" and not _optional_type(block, "text", str):
            return False
        if block.get("type") == "tool_result" and (
            not _optional_type(block, "tool_use_id", str)
            or not _optional_type(block, "is_error", bool)
            or ("content" in block and not _claude_text(block["content"]))
        ):
            return False
    return True


def _validate_claude_message(message: object, assistant: bool = False) -> None:
    if type(message) is not dict:
        raise ValueError("invalid Claude message")
    if not all(
        _optional_type(message, field, str)
        for field in ("timestamp", "role", "model", "uuid")
    ):
        raise ValueError("invalid Claude message fields")
    usage = message.get("usage")
    if usage is not None and (
        type(usage) is not dict or not _optional_type(usage, "output_tokens", int)
    ):
        raise ValueError("invalid Claude usage")
    content = message.get("content", [])
    if assistant and type(content) is not list:
        raise ValueError("invalid Claude assistant content")
    if type(content) is str:
        return
    if type(content) is not list:
        raise ValueError("invalid Claude content")
    for block in content:
        if type(block) is not dict or not _optional_type(block, "type", str):
            raise ValueError("invalid Claude content block")
        kind = block.get("type")
        if kind == "thinking" and not _optional_type(block, "thinking", str):
            raise ValueError("invalid Claude thinking")
        if kind == "text" and not _optional_type(block, "text", str):
            raise ValueError("invalid Claude text")
        if kind == "tool_use":
            if not all(
                _optional_type(block, field, str) for field in ("id", "name")
            ) or ("input" in block and type(block["input"]) is not dict):
                raise ValueError("invalid Claude tool use")
            inputs = block.get("input", {})
            name = block.get("name") or ""
            validate_recognizer_inputs(name, inputs)
            known_text = ()
            if name in SPAWN_TOOLS:
                known_text = (
                    "subagent_type", "description", "prompt", "name", "script",
                )
            elif name == "Bash":
                known_text = ("command",)
            elif name in {"Grep", "Glob"}:
                known_text = ("pattern", "path")
            elif name == "WebFetch":
                known_text = ("url", "prompt")
            if not all(
                _optional_type(inputs, field, str) for field in known_text
            ) or (
                name == "Bash"
                and not _optional_type(inputs, "run_in_background", bool)
            ):
                raise ValueError("invalid Claude tool input")
        if kind == "tool_result" and (
            not _optional_type(block, "tool_use_id", str)
            or not _optional_type(block, "is_error", bool)
            or ("content" in block and not _claude_text(block["content"]))
        ):
            raise ValueError("invalid Claude tool result")


def _validate_claude_record(record: object) -> None:
    if (
        type(record) is not dict
        or not _finite_json_value(record)
        or not _optional_type(record, "type", str)
    ):
        raise ValueError("invalid Claude replacement record")
    if not all(
        _optional_type(record, field, str) for field in ("timestamp", "uuid")
    ):
        raise ValueError("invalid Claude record fields")
    kind = record.get("type")
    if kind in ("assistant", "user"):
        if "isSidechain" in record and type(record["isSidechain"]) is not bool:
            raise ValueError("invalid Claude sidechain marker")
        _validate_claude_message(record.get("message"), kind == "assistant")
    elif kind == "progress":
        data = record.get("data")
        if type(data) is not dict or not _optional_type(data, "type", str):
            raise ValueError("invalid Claude progress")
        if data.get("type") != "agent_progress":
            return
        if not all(
            _optional_type(record, field, str)
            for field in ("toolUseID", "parentToolUseID")
        ) or not all(
            _optional_type(data, field, str) for field in ("agentId", "prompt")
        ):
            raise ValueError("invalid Claude progress identity")
        if "message" not in data:
            return
        envelope = data.get("message")
        if type(envelope) is not dict or not all(
            _optional_type(envelope, field, str) for field in ("timestamp", "uuid")
        ):
            raise ValueError("invalid Claude progress envelope")
        _validate_claude_message(envelope.get("message"), True)
    elif "message" in record:
        message = record["message"]
        _validate_claude_message(
            message, type(message) is dict and message.get("role") == "assistant",
        )


def _validate_claude_metadata(value: object) -> None:
    if type(value) is not dict or not _finite_json_value(value) or not all(
        _optional_exact_type(value, field, str)
        for field in (
            "agentType", "description", "taskKind", "teamName", "name", "model",
            "toolUseId",
        )
    ):
        raise ValueError("invalid Claude metadata")


def _validate_claude_team(value: object) -> None:
    if type(value) is not dict or type(value.get("members", [])) is not list:
        raise ValueError("invalid Claude team config")
    if any(
        type(member) is not dict
        or not all(_optional_type(member, field, str) for field in ("name", "agentType"))
        for member in value.get("members", [])
    ):
        raise ValueError("invalid Claude team member")


def _validate_claude_task(value: object) -> None:
    if type(value) is not dict or not all(
        _optional_type(value, field, str)
        for field in ("id", "subject", "status", "owner", "description")
    ):
        raise ValueError("invalid Claude task")


def _validate_claude_workflow_event(value: object) -> None:
    if type(value) is not dict or not _optional_type(value, "type", str) or not all(
        _optional_type(value, field, str) for field in ("agentId", "result")
    ):
        raise ValueError("invalid Claude workflow event")


def _claude_followup(candidate) -> ReloadPlan:
    return candidate._reload_request()


@dataclass(frozen=True)
class _PreparedClaudeAppend:
    cohort: CapturedCohort
    metadata: tuple[tuple[str, dict], ...]
    teams: tuple[tuple[str, set[str]], ...]
    tasks: tuple[tuple[str, str, Task], ...]


class ClaudeModel:
    """Incrementally tail one session file and maintain the node tree."""

    provider = "claude"

    def __init__(self, path: str):
        self.provider = "claude"
        self.path = os.path.realpath(path)
        self._offset = 0
        self._main_file_id: tuple[int, int] | None = None
        self._main_stat: tuple | None = None
        self._burst = 0
        self._dispatch_count: dict[str, int] = {}
        self.nodes: dict[str, Node] = {
            "main": Node(id="main", label="main loop", parent=None, status="running", kind="main")
        }
        # tool_use id -> the agentId it spawned (filled as substreams appear)
        self._spawn: dict[str, str] = {}
        # tool_use id -> owner node id (who issued the Task/Agent call)
        self._spawn_owner: dict[str, str] = {}
        # spawn tool ids whose result arrived before we knew the agent: id -> "done"|"error"
        self._pending_done: dict[str, str] = {}
        # spawn tool ids whose result text arrived before the agent node existed
        self._pending_result: dict[str, str] = {}
        # spawn ids seen on child progress before their tool_use record arrives
        self._pending_spawn_children: dict[str, str] = {}
        # tool_use id -> (toolName, subagent_type, description)
        self._spawn_meta: dict[str, tuple] = {}
        # tool_use id -> the Step that issued it, so its result can be paired back
        self._step_by_tid: dict[str, Step] = {}
        self._pending_tool_results: dict[str, tuple[str, str]] = {}
        # background shells (Bash run_in_background): launch acks with an id + an
        # output file the command keeps streaming to; we tail it instead of
        # marking the step done. _bg_pending: tool ids awaiting that ack.
        self._bg_pending: set[str] = set()
        self._bg_steps: list[Step] = []          # steps whose bg_path we tail live
        # Workflow runs live in a separate store: <sessionId>/subagents/workflows/<runId>/
        self.session_dir = (
            self.path[:-6] if self.path.endswith(".jsonl") else self.path + ".d"
        )
        self._wf_offsets: dict[str, int] = {}   # workflow file path -> byte offset
        self._tail_file_ids: dict[str, tuple[int, int]] = {}
        self._tail_stats: dict[str, tuple] = {}
        self._tail_rewound: set[str] = set()
        self._wf_name: dict[str, str] = {}       # runId -> human name (from script meta)
        self._wf_tool_name: dict[str, str] = {}  # Workflow tool_use id -> name (pre-runId)
        self._workflow_nodes: dict[str, str] = {}  # recorded agentId -> workflow node id
        # Flat subagents (simple Task/Agent + team teammates) live under
        # <session_dir>/subagents/agent-<id>.{jsonl,meta.json}; the meta discriminates.
        self._meta_cache: dict[str, tuple[tuple, dict]] = {}
        self._flat_nodes: dict[str, str] = {}    # on-disk agent id -> current node id
        self._seen_agent_records: dict[str, set[tuple]] = {}
        self._agent_source_counts: dict[str, dict[str, dict[tuple, int]]] = {}
        self._team_cache: dict[str, tuple] = {}  # teamName -> (signature, member-name set)
        self.task_list: dict[str, list[Task]] = {}   # teamName -> all its tasks (for the board)
        self._task_sig: dict[str, tuple] = {}        # teamName -> change signature
        self._task_file_cache: dict[str, tuple[tuple, Task]] = {}
        self._cohort_proof = None

    def poll(self) -> bool:
        return poll_append(self)

    @property
    def source_revision(self) -> int:
        return _source_revision(self)

    @property
    def visible_revision(self) -> int:
        return _visible_revision(self)

    def _reload_request(self) -> ReloadPlan:
        paths = {self.path}
        optional_paths = {
            step.bg_path for step in self._bg_steps if step.bg_path
        }
        teams = set(self.task_list)
        teams.update(
            node_id.split(":", 1)[1]
            for node_id in self.nodes
            if node_id.startswith("team:")
        )
        teams.update(
            metadata.get("teamName")
            for _, metadata in self._meta_cache.values()
            if type(metadata.get("teamName")) is str
        )
        directories = [
            DirectoryRule(
                self.session_dir,
                _claude_session_relevant,
                recursive=True,
            ),
        ]
        for team in teams:
            directories.append(DirectoryRule(
                os.path.realpath(os.path.join(TEAMS, team)),
                _claude_team_relevant,
            ))
            directories.append(DirectoryRule(
                os.path.realpath(os.path.join(TASKS, team)),
                _claude_task_relevant,
            ))
        return ReloadPlan(
            tuple(sorted(paths)),
            tuple(directories),
            optional_files=tuple(sorted(optional_paths)),
            followup=_claude_followup,
        )

    def observation_paths(self) -> set[str]:
        paths = {self.path, self.session_dir}
        for node_id in self.nodes:
            if not node_id.startswith("team:"):
                continue
            team = node_id.split(":", 1)[1]
            paths.add(os.path.realpath(os.path.join(TEAMS, team)))
            paths.add(os.path.realpath(os.path.join(TASKS, team)))
        paths.update(
            step.bg_path
            for step in self._bg_steps
            if step.bg_path and step.status == "running"
        )
        return paths

    def accepts_observation(self, paths: frozenset[str]) -> bool:
        return True

    def lifecycle_identity(self) -> tuple[str, str]:
        name = os.path.basename(self.path)
        session_id = name[:-6] if name.endswith(".jsonl") else name
        return "claude", session_id

    def apply_lifecycle(self, fact: LifecycleFact, target: str, status: str,
                        preserve_error: bool = False) -> bool:
        if target == "main":
            node = self.nodes["main"]
            if node.status == "error" and (
                status != "running" or fact.recorded_ns <= node.last_ts * 1_000_000_000
            ):
                return False
        else:
            node_id = self._flat_nodes.get(target, target)
            node = self.nodes.get(node_id)
            if node is None:
                if status in {"done", "error"} and not fact.agent_type:
                    return False
                node = Node(
                    id=node_id,
                    label=fact.agent_type or "agent",
                    parent="main",
                    status="running",
                    kind="agent",
                )
                self.nodes[node_id] = node
                self.nodes["main"].children.append(node_id)
                self._flat_nodes[target] = node_id
        if preserve_error and node.status == "error":
            return False
        changed = node.status != status
        if target == "main" and node.status == "error" and status == "running":
            node.result = ""
        node.status = status
        result = fact.result[:RESULT_CLIP]
        if result and status in {"done", "error"} and node.result != result:
            node.result = result
            changed = True
        changed |= sync_batch_statuses(self.nodes)
        if changed:
            _advance_visible_revision(self)
        return changed

    def _settle_spawns(self) -> bool:
        """Apply spawn result after current reconciliation burst drains.

        Claude writes result before buffered child progress. Result remains
        authority; burst boundary keeps late backfill from reopening child.
        """
        changed = False
        for n in self.nodes.values():
            if n.finish and n.status == "running" and n.active_burst < self._burst:
                n.status = n.finish
                changed = True
        return changed

    def reload_plan(self):
        return self._reload_request()

    def prepare_append(self, cohort: CapturedCohort) -> _PreparedClaudeAppend:
        sources = {source.path: source for source in cohort.files}
        main_path = os.path.abspath(self.path)
        main = sources.get(main_path)
        if cohort.action == "bootstrap" and main is None:
            raise ValueError("missing Claude main source")
        metadata = {}
        team_members: dict[str, set[str]] = {}
        tasks = []
        for source in cohort.files:
            path = source.path
            if path.endswith(".jsonl"):
                for record in cohort.records_for(path):
                    if os.path.basename(path) == "journal.jsonl":
                        _validate_claude_workflow_event(record)
                    else:
                        _validate_claude_record(record)
            elif path.endswith(".meta.json"):
                if source.value is None:
                    continue
                _validate_claude_metadata(source.value)
                metadata[path] = source.value
            elif os.path.basename(path) == "config.json" and self._under(path, TEAMS):
                if source.value is None:
                    continue
                _validate_claude_team(source.value)
                team = os.path.basename(os.path.dirname(path))
                names = {
                    member.get("name")
                    for member in source.value.get("members", [])
                    if member.get("agentType") != "team-lead"
                }
                team_members[team] = names
            elif path.endswith(".json") and self._under(path, TASKS):
                if source.value is None:
                    continue
                _validate_claude_task(source.value)
                value = source.value
                tasks.append((
                    os.path.basename(os.path.dirname(path)),
                    path,
                    Task(
                        id=value.get("id") or os.path.basename(path)[:-5],
                        subject=value.get("subject") or "",
                        status=value.get("status") or "pending",
                        owner=value.get("owner") or "",
                        description=value.get("description") or "",
                    ),
                ))
        return _PreparedClaudeAppend(
            cohort,
            tuple(sorted(metadata.items())),
            tuple(sorted(team_members.items())),
            tuple(sorted(tasks, key=lambda item: (item[0], item[1]))),
        )

    def apply_append(self, prepared: object) -> bool:
        batch = prepared
        if type(batch) is not _PreparedClaudeAppend:
            raise TypeError("invalid prepared Claude append")
        cohort = batch.cohort
        prior_nodes = self.nodes
        prior_tasks = self.task_list
        if cohort.action == "bootstrap":
            path = self.path
            self.__init__(path)
        changed = False
        self._burst += 1
        sources = {source.path: source for source in cohort.files}
        main_path = os.path.abspath(self.path)
        main = sources.get(main_path)
        if main is not None:
            for record in cohort.records_for(main_path):
                changed |= self._ingest(record)
            self._offset = main.offset
            self._main_file_id = main.signature[:2]
            self._main_stat = main.signature

        for path, value in batch.metadata:
            source = sources[path]
            self._meta_cache[path] = (source.signature, value)
        for team, names in batch.teams:
            path = next(
                path for path in sources
                if os.path.basename(path) == "config.json"
                and os.path.basename(os.path.dirname(path)) == team
            )
            self._team_cache[team] = (sources[path].signature, names)
        for _team, path, task in batch.tasks:
            self._task_file_cache[path] = (sources[path].signature, task)

        metadata = {
            path: value
            for path, (_signature, value) in self._meta_cache.items()
        }
        team_members = {
            team: names for team, (_signature, names) in self._team_cache.items()
        }
        changed |= self._parse_workflows(cohort)
        refresh_agents = {
            source.path
            for source in cohort.files
            if source.path.endswith(".jsonl")
            and cohort.records_for(source.path)
        }
        refresh_agents.update(
            path[:-len(".meta.json")] + ".jsonl"
            for path, _value in batch.metadata
        )
        changed_teams = {team for team, _names in batch.teams}
        refresh_agents.update(
            path[:-len(".meta.json")] + ".jsonl"
            for path, (_signature, value) in self._meta_cache.items()
            if value.get("teamName") in changed_teams
        )
        changed |= self._parse_flat_agents(
            cohort, metadata, team_members, refresh_agents,
        )
        changed |= self._parse_tasks()
        changed |= self._parse_background(cohort)

        for source in cohort.files:
            if source.path == main_path or not source.path.endswith(".jsonl"):
                continue
            self._wf_offsets[source.path] = source.offset
            self._tail_file_ids[source.path] = source.signature[:2]
            self._tail_stats[source.path] = source.signature
        self._tail_rewound.clear()
        changed |= self._settle_spawns()
        changed |= sync_batch_statuses(self.nodes)
        if cohort.action == "bootstrap":
            replacement_nodes = self.nodes
            prior_main = prior_nodes["main"]
            prior_main.__dict__.update(replacement_nodes["main"].__dict__)
            replacement_nodes["main"] = prior_main
            prior_nodes.clear()
            prior_nodes.update(replacement_nodes)
            self.nodes = prior_nodes
            prior_tasks.clear()
            prior_tasks.update(self.task_list)
            self.task_list = prior_tasks
        return changed

    def settle_idle(self) -> bool:
        if not any(
            node.finish
            and node.status == "running"
            and node.active_burst <= self._burst
            for node in self.nodes.values()
        ):
            return False
        self._burst += 1
        changed = self._settle_spawns()
        changed |= sync_batch_statuses(self.nodes)
        return changed

    @staticmethod
    def _under(path: str, root: str) -> bool:
        try:
            target = os.path.realpath(path)
            canonical_root = os.path.realpath(root)
            return os.path.commonpath((target, canonical_root)) == canonical_root
        except ValueError:
            return False

    def _parse_workflows(self, cohort: CapturedCohort) -> bool:
        changed = False
        root = os.path.abspath(os.path.join(self.session_dir, "subagents", "workflows"))
        runs = {
            os.path.basename(stamp.path)
            for stamp in cohort.directories
            if os.path.dirname(stamp.path) == root
        }
        for source in cohort.files:
            if not self._under(source.path, root):
                continue
            relative = os.path.relpath(source.path, root).split(os.sep)
            if len(relative) == 2:
                runs.add(relative[0])
        for run_id in sorted(runs):
            wnid = "wf:" + run_id
            label = "⚙ " + self._wf_name.get(run_id, "workflow")
            if wnid not in self.nodes:
                self.nodes[wnid] = Node(
                    id=wnid,
                    label=label,
                    parent="main",
                    status="running",
                    kind="workflow",
                )
                self.nodes["main"].children.append(wnid)
                changed = True
            elif self.nodes[wnid].label != label:
                self.nodes[wnid].label = label
                changed = True
            run_root = os.path.join(root, run_id)
            journal = cohort.file(os.path.join(run_root, "journal.jsonl"))
            if journal is not None:
                for event in cohort.records_for(journal.path):
                    _validate_claude_workflow_event(event)
                    agent_id = event.get("agentId")
                    if not agent_id:
                        continue
                    prior_nodes = len(self.nodes)
                    agent_node = self._wf_agent(agent_id, run_id, wnid)
                    changed |= len(self.nodes) != prior_nodes
                    node = self.nodes[agent_node]
                    if event.get("type") == "started":
                        if node.status != "running" or node.result:
                            node.status = "running"
                            node.result = ""
                            changed = True
                    elif event.get("type") == "result":
                        result = event.get("result", "")[:RESULT_CLIP]
                        if node.status != "done" or (
                            event.get("result") is not None and node.result != result
                        ):
                            node.status = "done"
                            if event.get("result") is not None:
                                node.result = result
                            changed = True
            prefix = run_root + os.sep + "agent-"
            for source in sorted(cohort.files, key=lambda item: item.path):
                if not source.path.startswith(prefix) or not source.path.endswith(".jsonl"):
                    continue
                agent_id = os.path.basename(source.path)[len("agent-"):-len(".jsonl")]
                prior_nodes = len(self.nodes)
                agent_node = self._wf_agent(agent_id, run_id, wnid)
                changed |= len(self.nodes) != prior_nodes
                node = self.nodes[agent_node]
                changed |= self._ingest_captured_agent(
                    source.path, node, cohort.records_for(source.path),
                )
            children = self.nodes[wnid].children
            status = (
                "done"
                if children and all(self.nodes[child].status == "done" for child in children)
                else "running"
            )
            if self.nodes[wnid].status != status:
                self.nodes[wnid].status = status
                changed = True
        return changed

    def _ingest_captured_agent(self, path: str, node: Node, records) -> bool:
        source = "disk:" + path
        changed = False
        for record in records:
            message = record.get("message", {})
            if type(message) is not dict:
                continue
            raw_timestamp = record.get("timestamp") or ""
            changed |= self._ingest_agent_message(
                node,
                record,
                message,
                parse_timestamp(raw_timestamp),
                raw_timestamp,
                source,
            )
        return changed

    def _parse_flat_agents(
        self,
        cohort: CapturedCohort,
        metadata: dict[str, dict],
        team_members: dict[str, set],
        paths,
    ) -> bool:
        changed = False
        root = os.path.abspath(os.path.join(self.session_dir, "subagents"))
        for path in sorted(paths):
            if os.path.dirname(path) != root:
                continue
            name = os.path.basename(path)
            if not name.startswith("agent-") or not name.endswith(".jsonl"):
                continue
            agent_id = name[len("agent-"):-len(".jsonl")]
            meta_path = os.path.join(root, f"agent-{agent_id}.meta.json")
            meta = metadata.get(meta_path, {})
            if meta.get("agentType") == "workflow-subagent":
                changed |= self._drop_provisional_flat_node(agent_id)
                continue
            records = cohort.records_for(path)
            if meta.get("taskKind") == "in_process_teammate":
                team = meta.get("teamName") or "team"
                changed |= self._ingest_team_member(
                    path,
                    agent_id,
                    meta,
                    records=records,
                    members=team_members.get(team),
                )
            else:
                changed |= self._ingest_task_agent(
                    path,
                    agent_id,
                    meta,
                    records=records,
                )
        return changed

    def _parse_tasks(self) -> bool:
        changed = False
        teams = {
            node_id.split(":", 1)[1]
            for node_id in self.nodes
            if node_id.startswith("team:")
        }
        for team in sorted(teams):
            tasks = [
                task
                for path, (_signature, task) in self._task_file_cache.items()
                if os.path.basename(os.path.dirname(path)) == team
            ]
            tasks.sort(key=lambda task: int(task.id) if task.id.isdigit() else 1 << 30)
            changed |= self._apply_tasks(team, tasks)
        return changed

    def _apply_tasks(self, team: str, tasks: list[Task]) -> bool:
        team_node = "team:" + team
        signature = tuple(
            (task.id, task.status, task.owner, task.subject, task.description)
            for task in tasks
        )
        changed = self._task_sig.get(team) != signature
        self._task_sig[team] = signature
        if changed:
            self.task_list[team] = tasks
        names = {
            node.label: node_id
            for node_id, node in self.nodes.items()
            if node.kind == "teammate" and node.parent == team_node
        }
        wanted = {
            f"task:{team}:{task.id}": (names[task.owner], task)
            for task in tasks
            if task.owner in names
        }
        for node_id in [
            node_id for node_id in self.nodes
            if node_id.startswith(f"task:{team}:") and node_id not in wanted
        ]:
            self._detach(node_id)
            changed = True
        for node_id, (parent, task) in wanted.items():
            node = self.nodes.get(node_id)
            if node is None:
                node = self.nodes[node_id] = Node(
                    id=node_id, label="", parent=parent, kind="task",
                )
                self.nodes[parent].children.append(node_id)
                changed = True
            elif node.parent != parent:
                self._detach(node_id, keep=True)
                node.parent = parent
                self.nodes[parent].children.append(node_id)
                changed = True
            label = f"#{task.id} {task.subject}"
            if (node.label, node.status, node.result) != (
                label, task.status, task.description,
            ):
                node.label = label
                node.status = task.status
                node.result = task.description
                changed = True
        return changed

    def _parse_background(self, cohort: CapturedCohort) -> bool:
        changed = False
        for step in self._bg_steps:
            if not step.bg_path or step.status != "running":
                continue
            source = cohort.file(step.bg_path)
            if source is None or not source.available:
                continue
            chunk = source.text if source.text is not None else source.raw.decode("utf-8")
            output = (step.output + chunk)[-BODY_CLIP:]
            if step.output != output:
                step.output = output
                changed = True
            if chunk:
                activity = source.stamp.mtime_ns / 1_000_000_000
                step.ts = activity
                for node in self.nodes.values():
                    if any(item is step for item in node.steps):
                        node.last_ts = max(node.last_ts, activity)
                        break
            self._wf_offsets[source.path] = source.offset
            self._tail_file_ids[source.path] = source.signature[:2]
            self._tail_stats[source.path] = source.signature
        return changed

    def _node(self, nid: str) -> Node:
        n = self.nodes.get(nid)
        if n is None:
            n = Node(id=nid, label="agent", parent="main")
            self.nodes[nid] = n
            self.nodes["main"].children.append(nid)
        return n

    def _ingest(self, r: dict) -> bool:
        t = r.get("type")
        if t == "assistant" and not r.get("isSidechain"):
            return self._ingest_main_turn(r)
        if t == "user" and not r.get("isSidechain"):
            return self._ingest_user_turn(r)
        if t == "progress" and r.get("data", {}).get("type") == "agent_progress":
            return self._ingest_progress(r)
        return False

    def _record_tool_use(self, owner: str, c: dict, ts: float) -> Step:
        """Register a tool_use issued by `owner` and return its Step. Spawn calls
        (Task/Agent/Workflow) become kind "spawn" and wire up the tree — their step
        later nests the agent it launched (step.child). Every tool step is tracked
        by tool id so its result can be paired back into step.output/status."""
        name = c.get("name", "?")
        inp = c.get("input", {}) or {}
        tid = c.get("id") or ""
        node = self._node(owner)
        skill = skill_mode.skill_of(name, inp)
        if skill and (not node.skills or node.skills[-1] != skill):
            node.skills.append(skill)
        workflow.note(node, name, inp, skill)
        if name in SPAWN_TOOLS:
            sub = inp.get("subagent_type")
            description = inp.get("description")
            desc = description or sub or inp.get("name") or name
            goal = preferred_goal(description, sub)
            self._spawn_owner[tid] = owner
            self._spawn_meta[tid] = (name, sub, desc)
            if name == "Workflow":
                # Workflow agents live in a separate store, not as agent_progress here.
                # Capture the run's name; the store reader (keyed by runId) builds the tree.
                m = re.search(r"name:\s*['\"]([^'\"]+)", inp.get("script", "") or "")
                wfname = m.group(1) if m else "workflow"
                self._wf_tool_name[tid] = wfname
                step = Step("spawn", clip_text(f"Workflow: {wfname}", 78), "", ts, status="running", tid=tid)
            else:
                # The exact instructions the subagent was handed — kept whole, not
                # truncated, so expanding the spawn step shows the real prompt.
                body = inp.get("prompt") or json.dumps(inp, indent=2)
                step = Step("spawn", clip_text(f"{name}: {desc}", 78), body,
                            ts, status="running", tid=tid, goal=goal)
            if tid:
                self._step_by_tid[tid] = step
                child = self._pending_spawn_children.pop(tid, None)
                if child:
                    self._link_spawn_child(tid, child)
                pending = self._pending_tool_results.pop(tid, None)
                if pending:
                    self._apply_tool_result(tid, pending[0], pending[1])
                    self._resolve_spawn_done(tid, pending[1], pending[0])
            return step
        one = skill or next((
            value for value in (
                inp.get("command"), inp.get("description"), inp.get("file_path"),
                inp.get("pattern"), inp.get("path"), inp.get("url"),
            ) if isinstance(value, str) and value
        ), "")
        if one and one in (inp.get("file_path"), inp.get("path")):
            one = os.path.basename(one.rstrip("/")) or one   # timeline shows the filename; full path is in the body
        title = clip_text(f"{name}: {one}" if one else name, 78)
        step = Step("tool", title, json.dumps(inp, indent=2)[:BODY_CLIP], ts, status="running", tid=tid)
        if name == "Bash" and inp.get("run_in_background") and tid:
            self._bg_pending.add(tid)
        if tid:
            self._step_by_tid[tid] = step
            pending = self._pending_tool_results.pop(tid, None)
            if pending:
                self._apply_tool_result(tid, pending[0], pending[1])
        return step

    def _link_spawn_child(self, tid: str, nid: str) -> bool:
        """Resolve a spawn edge once both its tool call and child node are known."""
        if tid not in self._spawn_owner or nid not in self.nodes:
            return False
        changed = self._spawn.get(tid) != nid
        self._spawn[tid] = nid
        step = self._step_by_tid.get(tid)
        if step and step.child != nid:
            step.child = nid
            changed = True
        node = self.nodes[nid]
        meta = self._spawn_meta.get(tid)
        if meta and node.label == "agent":
            node.label = meta[1] or "agent"
            changed = True
        if tid in self._pending_done:
            node.finish = node.status = self._pending_done.pop(tid)
            changed = True
        result = self._pending_result.pop(tid, "")
        if result and not node.result:
            node.result = result
            changed = True
        owner = self._spawn_owner.get(tid)
        parent = step.dispatch if step and step.dispatch in self.nodes else owner
        if (parent and parent in self.nodes and node.kind != "teammate"
                and parent != nid and node.parent != parent):
            self._reparent(nid, parent)
            changed = True
        for candidate, child in list(self._pending_spawn_children.items()):
            if child == nid:
                self._pending_spawn_children.pop(candidate, None)
        return changed

    def _add_content_steps(self, node: "Node", content, ts: float) -> bool:
        """Turn one assistant turn's content blocks into steps on `node`: a thought,
        spoken text, or a tool call each become one node in the agent's timeline."""
        changed = False
        message_spawns = []
        for c in content or []:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "thinking":
                tx = (c.get("thinking") or "").strip()
                if tx:
                    node.steps.append(Step("thinking", "thinking: " + one_line(tx), tx[:BODY_CLIP], ts))
                    changed = True
            elif t == "text":
                tx = (c.get("text") or "").strip()
                if tx:
                    node.steps.append(Step("text", "text: " + one_line(tx), tx[:BODY_CLIP], ts))
                    changed = True
            elif t == "tool_use":
                step = self._record_tool_use(node.id, c, ts)
                node.steps.append(step)
                if self._spawn_meta.get(step.tid, ("",))[0] in {"Task", "Agent"}:
                    message_spawns.append(step)
                changed = True
        if len(message_spawns) > 1:
            changed |= self._group_dispatch(node.id, message_spawns)
        return changed

    def _group_dispatch(self, owner: str, steps: list[Step]) -> bool:
        """Group Task and Agent calls from one Claude assistant message."""
        self._dispatch_count[owner] = self._dispatch_count.get(owner, 0) + 1
        count = self._dispatch_count[owner]
        batch_id = f"dispatch:{owner}:{steps[0].tid or count}"
        batch = Node(id=batch_id, label=batch_label((step.goal for step in steps), count), parent=owner,
                     status="running", kind="dispatch")
        self.nodes[batch_id] = batch
        parent = self.nodes[owner]
        positions = [
            parent.children.index(step.child)
            for step in steps
            if step.child in parent.children
        ]
        parent.children.insert(min(positions) if positions else len(parent.children), batch_id)
        for step in steps:
            step.dispatch = batch_id
            self._place_in_dispatch(step)
        return True

    def _place_in_dispatch(self, step: Step) -> None:
        if not step.child or step.dispatch not in self.nodes:
            return
        batch = self.nodes[step.dispatch]
        child = self.nodes.get(step.child)
        if not child:
            return
        self._reparent(child.id, step.dispatch)
        owner = self.nodes.get(batch.parent or "")
        if owner:
            launch_order = {
                spawn.child: index
                for index, spawn in enumerate(owner.steps)
                if spawn.dispatch == batch.id and spawn.child
            }
            batch.children.sort(key=lambda child_id: launch_order.get(child_id, len(launch_order)))

    def _agent_record_key(self, envelope: dict, message: dict, raw_ts: str) -> tuple:
        """Identify one subagent transcript record across its live and disk copies."""
        stable = envelope.get("uuid") or message.get("uuid")
        if stable:
            return ("uuid", str(stable))
        content = message.get("content")
        try:
            canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError):
            canonical = repr(content)
        return ("fallback", message.get("role"), raw_ts or "", canonical)

    def _ingest_agent_message(self, node: "Node", envelope: dict,
                              message: dict, ts: float, raw_ts: str,
                              source: str) -> bool:
        """Ingest a subagent record once even when both transcript sources contain it."""
        role = message.get("role")
        if role not in ("user", "assistant"):
            return False
        key = self._agent_record_key(envelope, message, raw_ts)
        if key[0] == "uuid":
            identity = key
        else:
            source_counts = self._agent_source_counts.setdefault(node.id, {}).setdefault(source, {})
            occurrence = source_counts.get(key, 0)
            source_counts[key] = occurrence + 1
            identity = (key, occurrence)
        seen = self._seen_agent_records.setdefault(node.id, set())
        if identity in seen:
            return False
        seen.add(identity)
        node.last_ts = max(node.last_ts, ts)
        if role == "user":
            content = message.get("content")
            if not any(s.kind == "prompt" for s in node.steps):
                body = _text_of(content)
                if body.strip():
                    node.detail = node.detail or one_line(body)[:DETAIL_CLIP]
                    node.steps.insert(0, Step("prompt", "task: " + one_line(body)[:DETAIL_CLIP], body, ts))
            return self._apply_results_from(content) or True
        model = message.get("model", "")
        if model:
            node.model = model.split("-")[1] if "-" in model else model
        usage = message.get("usage", {})
        node.tokens += int(usage.get("output_tokens", 0) or 0)
        self._add_content_steps(node, message.get("content"), ts)
        return True

    def _reset_agent_source(self, nid: str, source: str) -> None:
        sources = self._agent_source_counts.get(nid)
        if sources:
            sources.pop(source, None)

    def _apply_tool_result(self, tid: str, text: str, status: str) -> bool:
        """Fold a tool_result back onto the step that issued the call."""
        st = self._step_by_tid.get(tid)
        if not st:
            if tid:
                self._pending_tool_results[tid] = (text, status)
            return False
        if tid in self._bg_pending:
            # A background shell only acks with its id + output file — the command
            # keeps running and streams to that file. Hold the step "running" and
            # tail the file (see _poll_bg_shells) rather than marking it done.
            self._bg_pending.discard(tid)
            m = re.search(r"written to:\s*(\S+)", text or "")
            if m:
                st.bg_path = m.group(1).rstrip(".")
                st.output = ""
                self._bg_steps.append(st)
            return True
        if status:
            st.status = status
        if text and not st.output:
            st.output = text[:BODY_CLIP]
        return True

    def _resolve_spawn_done(self, tid: str, status: str, text: str = "") -> bool:
        """Flip the agent a spawn launched to done/error when its result lands —
        regardless of whose transcript the result appears in (the main loop or a
        parent subagent), so nested agents finish too. The result text is the
        agent's final message: keep a short form on the node (like workflow agents
        carry from the journal). If the result arrives before the agent node
        exists, _pending_done/_pending_result carry it until the agent shows up."""
        if not tid or tid not in self._spawn_owner:
            return False
        res = text.strip()[:RESULT_CLIP]
        aid = self._spawn.get(tid)
        if aid and aid in self.nodes:
            n = self.nodes[aid]
            n.status = status          # healthy ordering: substream already streamed
            n.finish = status
            if res and not n.result:
                n.result = res
        else:
            self._pending_done[tid] = status
            if res:
                self._pending_result[tid] = res
        return True

    def _apply_results_from(self, content) -> bool:
        changed = False
        for c in content or []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                status = "error" if c.get("is_error") else "done"
                tid = c.get("tool_use_id")
                text = _text_of(c.get("content"))
                changed |= self._apply_tool_result(tid, text, status)
                if self._resolve_spawn_done(tid, status, text):
                    changed = True
        return changed

    def _ingest_main_turn(self, r: dict) -> bool:
        main = self.nodes["main"]
        ts = parse_timestamp(r.get("message", {}).get("timestamp") or r.get("timestamp"))
        main.last_ts = max(main.last_ts, ts)
        u = r.get("message", {}).get("usage", {})
        main.tokens += int(u.get("output_tokens", 0) or 0)
        self._add_content_steps(main, r.get("message", {}).get("content", []), ts)
        if r.get("isApiErrorMessage") is True:
            main.status = "error"
            main.result = _text_of(r.get("message", {}).get("content", []))[:RESULT_CLIP]
        elif main.status == "error":
            main.status = "running"
            main.result = ""
        return True

    def _ingest_user_turn(self, r: dict) -> bool:
        """Record root text and fold tool results onto their issuing steps."""
        changed = False
        content = r.get("message", {}).get("content", []) or []
        text = content if isinstance(content, str) else "\n".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
        text = text.strip()
        if text:
            ts = parse_timestamp(r.get("message", {}).get("timestamp") or r.get("timestamp"))
            main = self.nodes["main"]
            if main.status == "error":
                main.status = "running"
                main.result = ""
            main.steps.append(Step(
                "prompt", "task: " + one_line(text)[:DETAIL_CLIP],
                text[:BODY_CLIP], ts,
            ))
            main.last_ts = max(main.last_ts, ts)
            changed = True
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                tid = c.get("tool_use_id")
                status = "error" if c.get("is_error") else "done"
                text = _text_of(c.get("content"))
                changed |= self._apply_tool_result(tid, text, status)
                # learn the runId for a Workflow call so its store nodes get the real name
                if tid in self._wf_tool_name:
                    body = c.get("content")
                    if isinstance(body, list):
                        body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                    rm = re.search(r"wf_[a-z0-9-]+", body or "")
                    if rm:
                        self._wf_name[rm.group(0)] = self._wf_tool_name[tid]
                        st = self._step_by_tid.get(tid)
                        if st:
                            st.child = "wf:" + rm.group(0)
                        changed = True
                if self._resolve_spawn_done(tid, status, text):
                    changed = True
        return changed

    def _ingest_progress(self, r: dict) -> bool:
        data = r.get("data", {})
        aid = data.get("agentId")
        if not aid:
            return False
        nid = self._flat_nodes.get(aid, aid)
        self._flat_nodes[aid] = nid
        created = nid not in self.nodes
        node = self._node(nid)
        changed = created
        # A result seen on a prior line already flagged this agent finished. Any
        # progress that arrives afterward is backfill still streaming in, so it must
        # re-open the agent (see _settle_spawns); a first sighting must not.
        was_finished = bool(node.finish)
        # link spawn tool id -> this agent, and resolve parent
        tid = r.get("toolUseID")
        ptid = r.get("parentToolUseID")
        candidates = list(dict.fromkeys(cand for cand in (tid, ptid) if cand))
        recognized = [cand for cand in candidates if cand in self._spawn_owner]
        if recognized:
            for cand in recognized:
                changed |= self._link_spawn_child(cand, nid)
        else:
            for cand in candidates:
                self._pending_spawn_children.setdefault(cand, nid)

        # name the node by its declared subagent_type, not its model
        meta = self._spawn_meta.get(tid) or self._spawn_meta.get(ptid)
        if meta and node.label == "agent":
            node.label = meta[1] or "agent"
            changed = True

        if data.get("prompt") and not any(s.kind == "prompt" for s in node.steps):
            node.detail = node.detail or one_line(data["prompt"])[:DETAIL_CLIP]
            node.steps.insert(0, Step("prompt", "task: " + one_line(data["prompt"])[:DETAIL_CLIP], data["prompt"]))
            changed = True

        msg = data.get("message", {})
        inner = msg.get("message", {})
        raw_ts = msg.get("timestamp") or r.get("timestamp") or ""
        ts = parse_timestamp(raw_ts)
        message_changed = self._ingest_agent_message(
            node, msg, inner, ts, raw_ts, "live"
        )
        if message_changed:
            node.active_burst = self._burst
        if message_changed and was_finished and node.status in ("done", "error"):
            node.status = "running"   # backfill still arriving; _settle_spawns re-closes it
            message_changed = True
        return changed or message_changed

    def _wf_agent(self, agent_id: str, run_id: str, wnid: str) -> str:
        nid = f"wfa:{run_id}:{agent_id}"
        self._workflow_nodes[agent_id] = nid
        if nid not in self.nodes:
            self.nodes[nid] = Node(id=nid, label="agent", parent=wnid,
                                   status="running", kind="agent")
            self.nodes[wnid].children.append(nid)
        return nid

    def _adopt_flat_node(self, aid: str, nid: str, parent: str,
                         kind: str, label: str) -> tuple["Node", bool, bool]:
        """Move a provisional flat-agent node when late metadata changes its kind."""
        old_nid = self._flat_nodes.get(aid)
        if old_nid is None and aid in self.nodes:
            old_nid = aid
        old_node = self.nodes.get(old_nid or "")
        old_parent_id = old_node.parent if old_node else None
        old_kind = old_node.kind if old_node else None
        created = nid not in self.nodes
        moved = False
        if old_nid and old_nid != nid and old_nid in self.nodes and created:
            node = self.nodes.pop(old_nid)
            old_parent = self.nodes.get(node.parent or "")
            if old_parent and old_nid in old_parent.children:
                old_parent.children.remove(old_nid)
            node.id = nid
            self.nodes[nid] = node
            for child in node.children:
                if child in self.nodes:
                    self.nodes[child].parent = nid
            for tid, owner in list(self._spawn_owner.items()):
                if owner == old_nid:
                    self._spawn_owner[tid] = nid
            for tid, spawned in list(self._spawn.items()):
                if spawned == old_nid:
                    self._spawn[tid] = nid
            for step in self._step_by_tid.values():
                if step.child == old_nid:
                    step.child = nid
            seen = self._seen_agent_records.pop(old_nid, None)
            if seen is not None:
                self._seen_agent_records[nid] = seen
            counts = self._agent_source_counts.pop(old_nid, None)
            if counts is not None:
                self._agent_source_counts[nid] = counts
            for candidate, child in list(self._pending_spawn_children.items()):
                if child == old_nid:
                    self._pending_spawn_children[candidate] = nid
            created = False
            moved = True
        elif created:
            node = Node(id=nid, label=label, parent=parent, status="running", kind=kind)
            self.nodes[nid] = node
        else:
            node = self.nodes[nid]
        if old_kind == "teammate" and (kind != "teammate" or old_parent_id != parent):
            for child in list(node.children):
                if self.nodes.get(child) and self.nodes[child].kind == "task":
                    self._detach(child)
        self._flat_nodes[aid] = nid
        reclassified = old_kind is not None and old_kind != kind
        node.kind = kind
        self._reparent(nid, parent)
        if nid not in self.nodes[parent].children:
            self.nodes[parent].children.append(nid)
        if old_parent_id and old_parent_id != parent:
            self._drop_empty_team(old_parent_id)
        return node, created, moved or reclassified or old_parent_id != parent

    def _drop_empty_team(self, nid: str) -> bool:
        node = self.nodes.get(nid)
        if not node or node.kind != "team" or node.children:
            return False
        team = nid.split(":", 1)[1]
        self._detach(nid)
        self.task_list.pop(team, None)
        self._task_sig.pop(team, None)
        self._team_cache.pop(team, None)
        return True

    def _drop_provisional_flat_node(self, aid: str) -> bool:
        nid = self._flat_nodes.pop(aid, None)
        if nid is None:
            return False
        node = self.nodes.get(nid)
        if node:
            old_parent = node.parent
            owned_tids = {tid for tid, owner in self._spawn_owner.items() if owner == nid}
            node_tids = {step.tid for step in node.steps if step.tid}
            for tid in owned_tids | node_tids:
                self._spawn_owner.pop(tid, None)
                self._spawn.pop(tid, None)
                self._spawn_meta.pop(tid, None)
                self._wf_tool_name.pop(tid, None)
                self._pending_done.pop(tid, None)
                self._pending_result.pop(tid, None)
                self._pending_tool_results.pop(tid, None)
                self._pending_spawn_children.pop(tid, None)
                self._bg_pending.discard(tid)
                self._step_by_tid.pop(tid, None)
            node_step_ids = {id(step) for step in node.steps}
            self._bg_steps = [step for step in self._bg_steps if id(step) not in node_step_ids]
            for tid, spawned in list(self._spawn.items()):
                if spawned == nid:
                    self._spawn.pop(tid, None)
                    step = self._step_by_tid.get(tid)
                    if step and step.child == nid:
                        step.child = ""
            for step in self._step_by_tid.values():
                if step.child == nid:
                    step.child = ""
            for candidate, child in list(self._pending_spawn_children.items()):
                if child == nid:
                    self._pending_spawn_children.pop(candidate, None)
            for child in list(node.children):
                if self.nodes.get(child) and self.nodes[child].kind == "task":
                    self._detach(child)
                else:
                    self._reparent(child, "main")
            self._detach(nid)
            if old_parent:
                self._drop_empty_team(old_parent)
        self._seen_agent_records.pop(nid, None)
        self._agent_source_counts.pop(nid, None)
        return node is not None

    def _reparent(self, nid: str, parent: str) -> None:
        n = self.nodes[nid]
        if n.parent == parent or parent not in self.nodes or nid == parent:
            return
        old = n.parent
        if old and old in self.nodes and nid in self.nodes[old].children:
            self.nodes[old].children.remove(nid)
        n.parent = parent
        if nid not in self.nodes[parent].children:
            self.nodes[parent].children.append(nid)

    def _ingest_task_agent(
        self, fp: str, aid: str, meta: dict, records,
    ) -> bool:
        """A plain Task/Agent subagent. Keyed by agentId so it merges with any
        live agent_progress node for the same agent (disk is the source of truth,
        the live stream just fills in faster)."""
        tid = meta.get("toolUseId")
        st = self._step_by_tid.get(tid)
        owner = self._spawn_owner.get(tid)
        parent = st.dispatch if st and st.dispatch in self.nodes else owner or "main"
        n, created, changed = self._adopt_flat_node(
            aid, aid, parent, "agent", meta.get("agentType") or "agent"
        )
        desired_label = meta.get("agentType") or ("agent" if changed else n.label)
        if n.label != desired_label:
            n.label = desired_label
            changed = True
        if meta.get("description") and (changed or not n.detail):
            n.detail = meta["description"][:DETAIL_CLIP]
            changed = True
        if tid:
            self._spawn[tid] = aid
            if st and st.child != aid:
                st.child = aid
                changed = True
            if st:
                self._place_in_dispatch(st)
            if tid in self._pending_done:
                n.status = self._pending_done.pop(tid)
                changed = True
            r_txt = self._pending_result.pop(tid, "")
            if r_txt and not n.result:
                n.result = r_txt
                changed = True
        changed |= self._ingest_captured_agent(fp, n, records)
        return changed or created

    def _ingest_team_member(
        self,
        fp: str,
        aid: str,
        meta: dict,
        records,
        members,
    ) -> bool:
        team = meta.get("teamName") or "team"
        cnid = "team:" + team
        created = False
        changed = False
        if cnid not in self.nodes:
            self.nodes[cnid] = Node(id=cnid, label="team " + team.replace("session-", ""),
                                    detail="lead: team-lead", parent="main",
                                    status="running", kind="team")
            self.nodes["main"].children.append(cnid)
            created = True
        mnid = "tm:%s:%s" % (team, aid)
        n, node_created, node_changed = self._adopt_flat_node(
            aid, mnid, cnid, "teammate",
            meta.get("name") or meta.get("agentType") or "teammate",
        )
        created |= node_created
        changed |= node_changed
        desired_label = meta.get("name") or meta.get("agentType") or "teammate"
        if n.label != desired_label:
            n.label = desired_label
            changed = True
        if meta.get("description") and (node_changed or not n.detail):
            n.detail = meta["description"][:DETAIL_CLIP]
            changed = True
        if not n.model and meta.get("model"):
            m = meta["model"]
            n.model = m.split("-")[1] if "-" in m else m
            changed = True
        changed |= self._ingest_captured_agent(fp, n, records)
        # A teammate is done only when the lead drops it from the team roster — that
        # is the sole authoritative signal. Silence is NOT completion: a teammate can
        # sit quiet for minutes while alive (waiting on the lead, a lock, a slow tool),
        # so a quiet-but-listed teammate stays running rather than being guessed done.
        # If roster is never pruned, teammate remains running rather than falsely done.
        # A teammate re-added to roster flips back to running.
        nm = meta.get("name")
        gone = bool(members is not None and nm and nm not in members)
        if gone and n.status != "done":
            n.status = "done"
            changed = True
        elif not gone and n.status == "done":
            n.status = "running"
            changed = True
        kids = self.nodes[cnid].children
        target = "done" if kids and all(self.nodes[k].status == "done" for k in kids) else "running"
        if self.nodes[cnid].status != target:
            self.nodes[cnid].status = target
            changed = True
        return changed or created

    def _detach(self, nid: str, keep: bool = False) -> None:
        """Unlink a node from its parent; drop it from the registry unless keep."""
        n = self.nodes.get(nid)
        if n and n.parent:
            p = self.nodes.get(n.parent)
            if p and nid in p.children:
                p.children.remove(nid)
        if not keep:
            self.nodes.pop(nid, None)
