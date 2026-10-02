"""Per-run overview: aggregate every agent session of a run into one tree.

A run spans several working directories (typically git worktrees of one repo) and
several agent clients. This module discovers the newest session of each provider
under each root and mirrors all of their node trees into a single tree:

    ◉ run
    ├─ ⌂ <root>
    │  ╰─ ◉ <provider> session … its own agent subtree
    ╰─ ⌂ <root> …

Mirrored nodes share their step lists with the underlying model, so drilling in
still shows live actions. QuakePro never talks to the process that launched the
run; it is pointed at the roots.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
from dataclasses import dataclass
from typing import Literal

from .session_model import BODY_CLIP, DETAIL_CLIP, Node, SessionModel, Step


_PROVIDERS = ("claude", "codex", "pi")
_STAGES = ("discover", "open", "poll", "watch")


def _failure_key(root: str, provider: str, path: str, stage: str) -> str:
    value = "\0".join((root, provider, path, stage)).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


@dataclass(frozen=True)
class OverviewFailure:
    key: str
    root: str
    provider: Literal["claude", "codex", "pi"]
    path: str
    stage: Literal["discover", "open", "poll", "watch"]
    error_type: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or not self.root or not os.path.isabs(self.root):
            raise TypeError("overview failure root must be absolute")
        if self.provider not in _PROVIDERS:
            raise TypeError("unknown overview failure provider")
        if self.stage not in _STAGES:
            raise TypeError("unknown overview failure stage")
        if not isinstance(self.path, str):
            raise TypeError("overview failure path must be a string")
        if self.stage == "discover" and self.path:
            raise TypeError("discovery failure path must be empty")
        if self.stage != "discover" and (not self.path or not os.path.isabs(self.path)):
            raise TypeError("member failure path must be absolute")
        if not isinstance(self.error_type, str) or not self.error_type:
            raise TypeError("overview failure error type must be nonempty")
        if not isinstance(self.message, str) or len(self.message) > BODY_CLIP:
            raise TypeError("overview failure message exceeds BODY_CLIP")
        expected = _failure_key(self.root, self.provider, self.path, self.stage)
        if self.key != expected:
            raise TypeError("overview failure key does not match identity")


def git_worktrees(cwd: str = ".") -> list[str]:
    """Every worktree path of the repository containing `cwd`, main one first.

    Returns an empty list when `cwd` is not a repository or git is unavailable.
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    found: list[str] = []
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            path = os.path.abspath(line[len("worktree "):].strip())
            if path and path not in found:
                found.append(path)
    return found


def resolve_roots(roots=(), worktrees: bool = False, cwd: str = ".") -> list[str]:
    """Ordered, de-duplicated absolute roots from an explicit list plus, when
    asked, the worktrees of the repository containing `cwd`."""
    resolved: list[str] = []
    for raw in list(roots) + (git_worktrees(cwd) if worktrees else []):
        path = os.path.abspath(os.path.expanduser(str(raw)))
        if path not in resolved:
            resolved.append(path)
    return resolved


def _aggregate_status(statuses) -> str:
    statuses = list(statuses)
    if not statuses:
        return "waiting"
    if "error" in statuses:
        return "error"
    if "running" in statuses:
        return "running"
    if "waiting" in statuses:
        return "waiting"
    return "done"


class RunOverview:
    """One read-only tree over every session of a run, grouped by root."""

    provider = "overview"

    def __init__(self, roots, discover=None, open_session=None):
        from .sessions import (
            DiscoveryCache,
            RootDiscovery,
            discovery_observation_paths,
            open_model,
            sessions_for_root,
        )

        self._discovery_cache = DiscoveryCache()
        self._discover = (
            discover
            if discover is not None
            else lambda root: sessions_for_root(root, self._discovery_cache)
        )
        self._discovery_type = RootDiscovery
        self._open = open_session or open_model
        self._open_uses_root = open_session is None
        self.roots = [os.path.abspath(os.path.expanduser(str(r))) for r in roots]
        self.path = self.roots[0] if self.roots else os.path.abspath(".")
        self._members: dict[str, tuple[str, str, SessionModel]] = {}
        self._slots: dict[tuple[str, str], str] = {}
        self._member_roots: dict[str, Node | None] = {}
        self._mirrors: dict[str, dict[str, Node]] = {}
        self._member_tasks: dict[str, dict] = {}
        self._watch_paths: dict[tuple[str, str, str], frozenset[str]] = {}
        self._failure_records: dict[tuple[str, str, str, str], OverviewFailure] = {}
        self._member_count = 0
        self._visible_revision = 0
        self._pending_observations: set[str] = set()
        self._settling_observation = False
        self._discovery_paths = set()
        for root in self.roots:
            try:
                paths = discovery_observation_paths(root)
            except Exception:
                continue
            if isinstance(paths, (set, frozenset)):
                self._discovery_paths.update(
                    path for path in paths
                    if isinstance(path, str) and path and os.path.isabs(path)
                )
        self.nodes: dict[str, Node] = {}
        self.task_list: dict = {}
        self._rebuild()

    # ---- membership ---------------------------------------------------------
    def _root_id(self, root: str) -> str:
        return "root:%d" % self.roots.index(root)

    def _member_for(self, root: str, provider: str):
        prefix = self._slots.get((root, provider))
        if prefix is None:
            return None
        return prefix, self._members[prefix]

    def _capture_member(self, prefix: str, root: str, path: str,
                        provider: str, model: SessionModel):
        mirrored = _mirror(prefix + ":", model, self._root_id(root))
        head = prefix + ":main"
        if head in mirrored:
            mirrored[head].label = "%s %s" % (provider, _session_name(path))
        tasks = dict(getattr(model, "task_list", {}) or {})
        main = getattr(model, "nodes", {}).get("main")
        return mirrored, tasks, main

    def _refresh_member(self, prefix: str, root: str, path: str,
                        provider: str, model: SessionModel) -> None:
        mirrored = self._mirrors.setdefault(prefix, {})
        _refresh_mirror(
            mirrored,
            prefix + ":",
            model,
            self._root_id(root),
        )
        head = prefix + ":main"
        if head in mirrored:
            mirrored[head].label = "%s %s" % (provider, _session_name(path))
        self._member_tasks[prefix] = dict(getattr(model, "task_list", {}) or {})
        self._member_roots[prefix] = getattr(model, "nodes", {}).get("main")

    def _install_member(self, prefix: str, root: str, provider: str, path: str,
                        model: SessionModel, prepared, fallback: frozenset[str]) -> None:
        self._member_count += 1
        self._slots[(root, provider)] = prefix
        self._members[prefix] = (root, path, model)
        self._watch_paths[(root, provider, path)] = fallback
        self._mirrors[prefix], self._member_tasks[prefix], self._member_roots[prefix] = prepared

    def _remove_prefix(self, prefix: str) -> None:
        self._members.pop(prefix, None)
        self._member_roots.pop(prefix, None)
        self._mirrors.pop(prefix, None)
        self._member_tasks.pop(prefix, None)

    def _replace_member(
        self, root: str, provider: str, path: str, model: SessionModel
    ) -> None:
        new_prefix = "s%d" % self._member_count
        prepared = self._capture_member(new_prefix, root, path, provider, model)
        current = self._member_for(root, provider)
        fallback = frozenset()
        if current is not None:
            old_prefix, (_, old_path, _) = current
            fallback = self._watch_paths.pop((root, provider, old_path), frozenset())
            self._clear_failures(
                lambda failure: failure.root == root
                and failure.provider == provider
                and failure.stage in {"poll", "watch"}
            )
            self._remove_prefix(old_prefix)
        self._install_member(
            new_prefix, root, provider, path, model, prepared, fallback
        )

    def _rekey_member(self, prefix: str, provider: str) -> str:
        root, path, model = self._members[prefix]
        replacement = "s%d" % self._member_count
        prepared = self._capture_member(replacement, root, path, provider, model)
        self._member_count += 1
        self._slots[(root, provider)] = replacement
        self._members[replacement] = self._members.pop(prefix)
        self._remove_prefix(prefix)
        (
            self._mirrors[replacement],
            self._member_tasks[replacement],
            self._member_roots[replacement],
        ) = prepared
        return replacement

    def _detach_provider(self, root: str, provider: str) -> bool:
        changed = False
        current = self._member_for(root, provider)
        if current is not None:
            prefix, (_, path, _) = current
            self._slots.pop((root, provider), None)
            self._remove_prefix(prefix)
            self._watch_paths.pop((root, provider, path), None)
            changed = True
        stale_watch = [
            key for key in self._watch_paths
            if key[0] == root and key[1] == provider
        ]
        for key in stale_watch:
            self._watch_paths.pop(key, None)
            changed = True
        changed |= self._clear_failures(
            lambda failure: failure.root == root and failure.provider == provider
        )
        return changed

    # ---- failures -----------------------------------------------------------
    @property
    def failures(self) -> tuple[OverviewFailure, ...]:
        root_order = {root: index for index, root in enumerate(self.roots)}
        provider_order = {provider: index for index, provider in enumerate(_PROVIDERS)}
        stage_order = {stage: index for index, stage in enumerate(_STAGES)}
        return tuple(sorted(
            self._failure_records.values(),
            key=lambda failure: (
                root_order[failure.root],
                provider_order[failure.provider],
                stage_order[failure.stage],
                failure.path,
            ),
        ))

    def _set_failure(self, root: str, provider: str, path: str, stage: str,
                     error_type: str, message: str) -> bool:
        message = message[:BODY_CLIP]
        identity = (root, provider, path, stage)
        failure = OverviewFailure(
            _failure_key(*identity), root, provider, path, stage, error_type, message
        )
        if self._failure_records.get(identity) == failure:
            return False
        self._failure_records[identity] = failure
        return True

    def _set_exception(self, root: str, provider: str, path: str, stage: str,
                       exc: Exception) -> bool:
        return self._set_failure(
            root, provider, path, stage, type(exc).__name__, str(exc)
        )

    def _clear_failures(self, predicate) -> bool:
        removed = [
            identity for identity, failure in self._failure_records.items()
            if predicate(failure)
        ]
        for identity in removed:
            self._failure_records.pop(identity, None)
        return bool(removed)

    # ---- discovery ----------------------------------------------------------
    def _open_member(self, root: str, path: str):
        return (self._open(path, project_root=root)
                if self._open_uses_root else self._open(path))

    def _apply_discovery(self, root: str, result) -> bool:
        provider = result.provider
        if result.error is not None:
            return self._set_failure(
                root,
                provider,
                "",
                "discover",
                result.error.error_type,
                result.error.message,
            )

        changed = self._clear_failures(
            lambda failure: failure.root == root
            and failure.provider == provider
            and failure.stage == "discover"
        )
        path = result.path
        if path is None:
            return changed | self._detach_provider(root, provider)

        current = self._member_for(root, provider)
        if current is not None and current[1][1] == path:
            changed |= self._clear_failures(
                lambda failure: failure.root == root
                and failure.provider == provider
                and failure.stage == "open"
            )
            return changed

        changed |= self._clear_failures(
            lambda failure: failure.root == root
            and failure.provider == provider
            and failure.stage == "open"
            and failure.path != path
        )
        try:
            model = self._open_member(root, path)
            self._replace_member(root, provider, path, model)
        except Exception as exc:
            return changed | self._set_exception(root, provider, path, "open", exc)

        changed |= self._clear_failures(
            lambda failure: failure.root == root
            and failure.provider == provider
            and failure.stage == "open"
        )
        return True

    def _discover_members(self) -> bool:
        changed = False
        for root in self.roots:
            discovery = self._discover(root)
            if not isinstance(discovery, self._discovery_type):
                raise TypeError("session discovery must return RootDiscovery")
            if discovery.root != root:
                raise TypeError("session discovery returned a different root")
            for result in discovery.providers:
                changed |= self._apply_discovery(root, result)
        return changed

    # ---- polling and watch paths -------------------------------------------
    def _ordered_members(self):
        for root in self.roots:
            for provider in _PROVIDERS:
                current = self._member_for(root, provider)
                if current is not None:
                    yield provider, current[0], current[1]

    @staticmethod
    def _valid_watch_paths(value) -> frozenset[str]:
        if not isinstance(value, (set, frozenset)):
            raise TypeError("member observation paths must be a set")
        if any(
            not isinstance(path, str) or not path or not os.path.isabs(path)
            for path in value
        ):
            raise TypeError("member observation paths must be absolute nonempty strings")
        return frozenset(value)

    def _poll_members(self) -> bool:
        changed = False
        for provider, prefix, (root, path, model) in list(self._ordered_members()):
            try:
                member_changed = bool(model.poll())
            except Exception as exc:
                self._member_roots[prefix] = getattr(model, "nodes", {}).get("main")
                changed |= self._set_exception(root, provider, path, "poll", exc)
                continue

            changed |= self._clear_failures(
                lambda failure: failure.root == root
                and failure.provider == provider
                and failure.path == path
                and failure.stage == "poll"
            )
            main = getattr(model, "nodes", {}).get("main")
            if main is not self._member_roots.get(prefix):
                self._rekey_member(prefix, provider)
                changed = True
            elif member_changed:
                self._refresh_member(prefix, root, path, provider, model)
                changed = True
        return changed

    def _read_watch_paths(self) -> tuple[bool, bool]:
        changed = False
        visible = False
        for provider, _, (root, path, model) in list(self._ordered_members()):
            identity = (root, provider, path)
            try:
                paths = self._valid_watch_paths(model.observation_paths())
            except Exception as exc:
                failure_changed = self._set_exception(
                    root, provider, path, "watch", exc,
                )
                changed |= failure_changed
                visible |= failure_changed
                continue

            failure_changed = self._clear_failures(
                lambda failure: failure.root == root
                and failure.provider == provider
                and failure.path == path
                and failure.stage == "watch"
            )
            changed |= failure_changed
            visible |= failure_changed
            if self._watch_paths.get(identity) != paths:
                self._watch_paths[identity] = paths
                changed = True
        return changed, visible

    def _discovery_needed(self) -> bool:
        if not self._settling_observation:
            return True
        observed = self._pending_observations
        self._pending_observations = set()
        if not observed:
            return False
        known = set()
        for paths in self._watch_paths.values():
            known.update(paths)
        for _, _, (_, path, _) in self._ordered_members():
            known.add(path)
        return any(
            path.endswith(".jsonl") and path not in known
            for path in observed
        )

    # ---- provider-model surface -------------------------------------------
    def poll(self) -> bool:
        visible = self._discover_members() if self._discovery_needed() else False
        visible |= self._poll_members()
        watch_changed, watch_visible = self._read_watch_paths()
        visible |= watch_visible
        changed = visible | watch_changed
        if visible:
            self._rebuild()
            self._visible_revision += 1
        if self._settling_observation and not changed:
            self._settling_observation = False
        return changed

    @property
    def visible_revision(self) -> int:
        return self._visible_revision

    def observation_paths(self) -> set[str]:
        paths = set(self._discovery_paths)
        for cached in self._watch_paths.values():
            paths.update(cached)
        return paths

    def accepts_observation(self, changed_paths) -> bool:
        if isinstance(changed_paths, (set, frozenset)):
            self._pending_observations.update(
                path for path in changed_paths
                if isinstance(path, str) and path and os.path.isabs(path)
            )
        self._settling_observation = True
        return True

    def lifecycle_identity(self):
        # Hook journals are per session; a run aggregates many, so the overview
        # keeps no lifecycle of its own.
        return None

    # ---- tree ---------------------------------------------------------------
    def _rebuild(self) -> None:
        nodes = self.nodes
        wanted: dict[str, Node] = {}
        task_list: dict = {}
        root_nodes: dict[str, Node] = {}
        for root in self.roots:
            rid = self._root_id(root)
            node = nodes.get(rid)
            if node is None or node.kind != "root":
                node = Node(id=rid, label="", parent="main", kind="root")
            node.label = os.path.basename(root) or root
            node.detail = root[-DETAIL_CLIP:]
            node.parent = "main"
            node.children = []
            node.status = "waiting"
            root_nodes[rid] = node
        for _, prefix, (root, _, _) in self._ordered_members():
            rid = self._root_id(root)
            if rid not in root_nodes:
                continue
            mirrored = self._mirrors.get(prefix, {})
            if not mirrored:
                continue
            wanted.update(mirrored)
            head = prefix + ":main"
            root_nodes[rid].children.append(head)
            task_list.update(self._member_tasks.get(prefix, {}))
        for failure in self.failures:
            rid = self._root_id(failure.root)
            node_id = "failure:" + failure.key
            detail = (failure.error_type + ": " + failure.message)[:DETAIL_CLIP]
            node = nodes.get(node_id)
            if node is None or node.kind != "issue":
                node = Node(id=node_id, label="", kind="issue")
            node.label = "%s %s failed" % (failure.provider, failure.stage)
            node.detail = detail
            node.parent = rid
            node.status = "error"
            node.steps = [Step(
                    kind="text",
                    title="%s: %s" % (failure.stage, failure.error_type),
                    body=failure.message[:BODY_CLIP],
                )]
            wanted[node_id] = node
            root_nodes[rid].children.append(node_id)
        for rid, node in root_nodes.items():
            node.status = _aggregate_status(wanted[c].status for c in node.children)
            wanted[rid] = node
        main = nodes.get("main")
        if main is None or main.kind != "main":
            main = Node(id="main", label="run", parent=None, kind="main")
        main.label = "run"
        main.parent = None
        main.status = _aggregate_status(n.status for n in root_nodes.values())
        main.children = [self._root_id(root) for root in self.roots]
        wanted["main"] = main
        for node_id in set(nodes) - set(wanted):
            nodes.pop(node_id, None)
        nodes.update(wanted)
        self.task_list.clear()
        self.task_list.update(task_list)


def _session_name(path: str) -> str:
    name = os.path.basename(path)
    return name[:-6] if name.endswith(".jsonl") else name


def _mirror(prefix: str, model: SessionModel, parent_of_main: str) -> dict[str, Node]:
    """Copy a member model's nodes under an id prefix, re-parenting its root.

    Step lists are shared, not copied, so the drill-in panel stays live.
    """
    source = getattr(model, "nodes", None) or {}
    if "main" not in source:
        return {}
    out: dict[str, Node] = {}
    for nid, node in source.items():
        parent = parent_of_main if node.parent is None else prefix + node.parent
        out[prefix + nid] = dataclasses.replace(
            node, id=prefix + nid, parent=parent,
            children=[prefix + child for child in node.children if child in source])
    return out


def _refresh_mirror(
    mirrored: dict[str, Node],
    prefix: str,
    model: SessionModel,
    parent_of_main: str,
) -> None:
    source = getattr(model, "nodes", None) or {}
    wanted = {prefix + node_id for node_id in source}
    for node_id in set(mirrored) - wanted:
        mirrored.pop(node_id, None)
    for node_id, node in source.items():
        mirrored_id = prefix + node_id
        parent = parent_of_main if node.parent is None else prefix + node.parent
        children = [prefix + child for child in node.children if child in source]
        current = mirrored.get(mirrored_id)
        if current is None:
            mirrored[mirrored_id] = dataclasses.replace(
                node,
                id=mirrored_id,
                parent=parent,
                children=children,
            )
            continue
        current.label = node.label
        current.detail = node.detail
        current.parent = parent
        current.status = node.status
        current.steps = node.steps
        current.children = children
        current.tokens = node.tokens
        current.last_ts = node.last_ts
        current.model = node.model
        current.kind = node.kind
        current.result = node.result
        current.skills = node.skills
        current.phase = node.phase
        current.workflow = node.workflow
        current.finish = node.finish
        current.active_burst = node.active_burst
