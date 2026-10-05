"""Provider-neutral projection from session nodes to recorded agent runs."""
from __future__ import annotations

from dataclasses import dataclass, field

from .agent_roles import role_from_name
from .session_model import Node, SessionModel, Step


_STRUCTURAL_KINDS = {"dispatch", "root", "task", "team"}


@dataclass
class StoryAgent:
    id: str
    label: str
    parent: str
    status: str
    started_at: float
    latest_at: float
    role: str = "other"


@dataclass
class SessionStory:
    agents: list[StoryAgent] = field(default_factory=list)
    status: str = "waiting"


@dataclass
class TimelineLane:
    label: str
    agent_id: str = ""
    role: str = "other"
    status: str = "waiting"
    started_at: float = 0.0
    latest_at: float = 0.0
    cues: list[str] = field(default_factory=list)
    parallel_section: int | None = None


@dataclass
class SessionTimeline:
    lanes: list[TimelineLane] = field(default_factory=list)
    status: str = "waiting"
    started_at: float = 0.0
    latest_at: float = 0.0


@dataclass
class _NodeFacts:
    id: str
    label: str
    detail: str
    parent: str | None
    status: str
    children: tuple[str, ...]
    last_ts: float
    kind: str
    skills: tuple[str, ...]
    source: Node
    steps_source: list[Step]
    step_count: int = 0
    first_step_at: float = 0.0
    last_step_at: float = 0.0
    spawns: dict[str, tuple[int, Step]] = field(default_factory=dict)
    version: int = 1


@dataclass(frozen=True)
class _VisitProjection:
    key: tuple
    agent: StoryAgent | None


def _node_state(node: Node) -> tuple:
    return (
        node.id,
        node.label,
        node.detail,
        node.parent,
        node.status,
        tuple(node.children),
        node.last_ts,
        node.kind,
        tuple(node.skills),
    )


def _fact_state(fact: _NodeFacts) -> tuple:
    return (
        fact.id,
        fact.label,
        fact.detail,
        fact.parent,
        fact.status,
        fact.children,
        fact.last_ts,
        fact.kind,
        fact.skills,
    )


def _replace_fact_state(fact: _NodeFacts, state: tuple) -> None:
    (
        fact.id,
        fact.label,
        fact.detail,
        fact.parent,
        fact.status,
        fact.children,
        fact.last_ts,
        fact.kind,
        fact.skills,
    ) = state


def _record_step(fact: _NodeFacts, step: Step, index: int) -> None:
    if step.ts > 0:
        if fact.first_step_at <= 0:
            fact.first_step_at = step.ts
        else:
            fact.first_step_at = min(fact.first_step_at, step.ts)
        fact.last_step_at = max(fact.last_step_at, step.ts)
    if step.kind == "spawn" and step.child:
        fact.spawns[step.child] = (index, step)


def _reset_step_facts(fact: _NodeFacts) -> None:
    fact.step_count = 0
    fact.first_step_at = 0.0
    fact.last_step_at = 0.0
    fact.spawns.clear()


def _role(fact: _NodeFacts) -> str:
    return role_from_name(fact.label, fact.detail)


def _started_from_fact(fact: _NodeFacts, spawn: Step | None) -> float:
    return min(
        (
            ts for ts in (
                getattr(spawn, "ts", 0.0),
                fact.first_step_at,
            ) if ts > 0
        ),
        default=0.0,
    )


def _latest_from_fact(fact: _NodeFacts, spawn: Step | None) -> float:
    return max(
        getattr(spawn, "ts", 0.0),
        fact.last_ts,
        fact.last_step_at,
    )


def _spawn_key(spawn: Step | None) -> tuple:
    if spawn is None:
        return ()
    return (id(spawn), spawn.goal, spawn.body, spawn.ts)


def _aggregate_status(agents: list[StoryAgent]) -> str:
    statuses = {agent.status for agent in agents}
    for status in ("error", "running", "waiting"):
        if status in statuses:
            return status
    return "done" if agents else "waiting"


def _ordered_agents(agents: list[StoryAgent]) -> list[StoryAgent]:
    return sorted(
        agents,
        key=lambda agent: (agent.started_at <= 0, agent.started_at),
    )


class SessionStoryProjector:
    """Incremental story projection over append-only session nodes."""

    def __init__(self) -> None:
        self._facts: dict[str, _NodeFacts] = {}
        self._visits: dict[str, _VisitProjection] = {}

    def _sync(self, nodes: dict[str, Node]) -> None:
        present = set(nodes)
        for node_id in tuple(self._facts):
            if node_id not in present:
                del self._facts[node_id]
                self._visits.pop(node_id, None)

        for node_id, node in nodes.items():
            state = _node_state(node)
            steps = node.steps
            fact = self._facts.get(node_id)
            existing = fact is not None and fact.source is node
            if fact is None or fact.source is not node:
                fact = _NodeFacts(
                    *state,
                    source=node,
                    steps_source=steps,
                )
                self._facts[node_id] = fact
                start = 0
            else:
                changed = False
                if _fact_state(fact) != state:
                    _replace_fact_state(fact, state)
                    changed = True
                if fact.steps_source is not steps or len(steps) < fact.step_count:
                    fact.steps_source = steps
                    _reset_step_facts(fact)
                    start = 0
                    changed = True
                else:
                    start = fact.step_count
                if changed:
                    fact.version += 1

            if len(steps) > start:
                for index in range(start, len(steps)):
                    _record_step(fact, steps[index], index)
                fact.step_count = len(steps)
                if existing:
                    fact.version += 1

    def _spawns(self) -> dict[str, Step]:
        spawns: dict[str, tuple[int, int, Step]] = {}
        for node_order, fact in enumerate(self._facts.values()):
            for child, (step_order, step) in fact.spawns.items():
                candidate = (node_order, step_order, step)
                prior = spawns.get(child)
                if prior is None or candidate[:2] > prior[:2]:
                    spawns[child] = candidate
        return {child: value[2] for child, value in spawns.items()}

    def project(self, model: SessionModel, root_id: str) -> SessionStory:
        self._sync(model.nodes)
        nodes = self._facts
        spawns = self._spawns()
        active_visits: set[str] = set()
        agents: list[StoryAgent] = []
        seen: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in seen:
                return
            seen.add(node_id)
            fact = nodes.get(node_id)
            if fact is None:
                return
            spawn = spawns.get(node_id)
            started_at = _started_from_fact(fact, spawn)
            key = (fact.version, _spawn_key(spawn))
            projection = self._visits.get(node_id)
            if projection is None or projection.key != key:
                agent = None
                if fact.kind not in _STRUCTURAL_KINDS:
                    agent = StoryAgent(
                        node_id,
                        " ".join((fact.label or fact.id).replace("⚙ ", "").split()),
                        fact.parent or "",
                        fact.status,
                        started_at,
                        _latest_from_fact(fact, spawn),
                        _role(fact) if fact.kind in {"agent", "teammate"} else "other",
                    )
                projection = _VisitProjection(key, agent)
                self._visits[node_id] = projection
            active_visits.add(node_id)
            if projection.agent is not None:
                agents.append(projection.agent)
            for child_id in fact.children:
                visit(child_id)

        visit(root_id)
        for node_id in tuple(self._visits):
            if node_id not in active_visits:
                del self._visits[node_id]
        child_agents = [agent for agent in agents if agent.id != root_id]
        return SessionStory(
            _ordered_agents(child_agents),
            _aggregate_status(agents),
        )


def project_session_story(model: SessionModel, root_id: str) -> SessionStory:
    return SessionStoryProjector().project(model, root_id)


def _lanes_overlap(left: TimelineLane, right: TimelineLane) -> bool:
    return (
        left.started_at > 0
        and left.latest_at > 0
        and right.started_at > 0
        and right.latest_at > 0
        and max(left.started_at, right.started_at)
        < min(left.latest_at, right.latest_at)
    )


def _assign_parallel_sections(lanes: list[TimelineLane]) -> None:
    timed = sorted(
        (
            lane for lane in lanes
            if lane.started_at > 0 and lane.latest_at > lane.started_at
        ),
        key=lambda lane: (lane.started_at, lane.latest_at),
    )
    sections: list[list[TimelineLane]] = []
    current: list[TimelineLane] = []
    section_end = 0.0
    for lane in timed:
        if current and lane.started_at >= section_end:
            if len(current) > 1:
                sections.append(current)
            current = []
            section_end = 0.0
        current.append(lane)
        section_end = max(section_end, lane.latest_at)
    if len(current) > 1:
        sections.append(current)

    for section, members in enumerate(sections):
        for lane in members:
            lane.parallel_section = section


def project_session_timeline(story: SessionStory) -> SessionTimeline:
    """Project one SessionStory onto shared recorded time without provider details."""
    lanes = []
    for agent in story.agents:
        lane = TimelineLane(
            label=agent.label,
            agent_id=agent.id,
            role=agent.role,
            status=agent.status,
            started_at=agent.started_at,
            latest_at=agent.latest_at,
        )
        lanes.append(lane)

    for lane in lanes:
        cues = []
        if any(_lanes_overlap(lane, other) for other in lanes if other is not lane):
            cues.append("parallel")
        if lane.status == "running":
            cues.append("active")
        if lane.status == "error":
            cues.append("failed")
        if lane.started_at <= 0 or lane.latest_at <= 0:
            cues.append("missing-time")
        lane.cues = cues

    _assign_parallel_sections(lanes)

    known_agents = [
        agent for agent in story.agents
        if agent.started_at > 0 and agent.latest_at > 0
    ]
    return SessionTimeline(
        lanes,
        story.status,
        min((agent.started_at for agent in known_agents), default=0.0),
        max((agent.latest_at for agent in known_agents), default=0.0),
    )
