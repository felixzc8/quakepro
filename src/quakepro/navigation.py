"""Pure navigation state transitions for QuakePro."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Mapping

from .session_model import Node


DISPATCH_AUTO_COLLAPSE_SIZE = 4
_EVENT_KINDS = frozenset(
    {
        "sync-roots",
        "activate-root",
        "move",
        "edge",
        "page",
        "same-layer",
        "next-active",
        "previous-active",
        "branch",
        "all-branches",
        "toggle-all",
        "toggle-view",
        "toggle-skills",
        "enter",
        "back",
        "click-node",
        "click-drill-row",
        "refresh",
    }
)
_VIEWS = ("tree", "timeline")


@dataclass(frozen=True)
class NavigationState:
    roots: tuple[str, ...]
    active_root: str
    view: Literal["tree", "timeline"]
    zone: Literal["tabs", "graph", "drill"]
    selected: str | None
    collapsed: frozenset[str]
    manual_branches: frozenset[tuple[str, bool]]
    show_skills: bool
    drill_open: bool
    drill_cursor: int
    drill_expanded: frozenset[int]

    def __post_init__(self) -> None:
        roots = tuple(self.roots)
        collapsed = frozenset(self.collapsed)
        choices = tuple(self.manual_branches)
        ids = [node_id for node_id, _ in choices]
        duplicate = next((node_id for node_id in ids if ids.count(node_id) > 1), None)
        if duplicate is not None:
            raise ValueError(f"duplicate manual branch id: {duplicate}")
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "collapsed", collapsed)
        object.__setattr__(self, "manual_branches", frozenset(choices))
        object.__setattr__(self, "drill_expanded", frozenset(self.drill_expanded))


@dataclass(frozen=True)
class NavigationEvent:
    kind: str
    value: object = None


@dataclass(frozen=True)
class NavigationContext:
    roots: tuple[str, ...]
    nodes: Mapping[str, Node]
    visible_order: tuple[str, ...]
    depths: Mapping[str, int]
    stop_kinds: tuple[str, ...]
    instruction_present: bool
    action_count: int


@dataclass(frozen=True)
class NavigationEffect:
    state: NavigationState
    commands: tuple[str, ...]


def _effect(
    state: NavigationState, *commands: str, previous: NavigationState | None = None
) -> NavigationEffect:
    if previous is not None and state == previous:
        return NavigationEffect(previous, ())
    return NavigationEffect(state, tuple(commands))


def _manual_map(state: NavigationState) -> dict[str, bool]:
    return dict(state.manual_branches)


def _with_branch_choice(
    state: NavigationState, node_id: str, collapse: bool
) -> NavigationState:
    choices = _manual_map(state)
    choices[node_id] = collapse
    collapsed = set(state.collapsed)
    if collapse:
        collapsed.add(node_id)
    else:
        collapsed.discard(node_id)
    return replace(
        state,
        collapsed=frozenset(collapsed),
        manual_branches=frozenset(choices.items()),
    )


def _children(context: NavigationContext, node_id: str) -> tuple[str, ...]:
    node = context.nodes.get(node_id)
    if node is None or node.kind in context.stop_kinds:
        return ()
    return tuple(child for child in node.children if child in context.nodes)


def _selected_ancestors(
    selected: str | None, nodes: Mapping[str, Node]
) -> frozenset[str]:
    ancestors = set()
    current = nodes.get(selected or "")
    parent = current.parent if current is not None else None
    while parent in nodes and parent not in ancestors:
        ancestors.add(parent)
        parent = nodes[parent].parent
    return frozenset(ancestors)


def _effective_collapsed(
    state: NavigationState,
    context: NavigationContext,
    manual: Mapping[str, bool] | None = None,
) -> frozenset[str]:
    choices = dict(manual) if manual is not None else _manual_map(state)
    forced_open = _selected_ancestors(state.selected, context.nodes)
    collapsed = set()
    for node_id, node in context.nodes.items():
        if not node.children or node_id in forced_open:
            continue
        choice = choices.get(node_id)
        if choice is True:
            collapsed.add(node_id)
        elif choice is None and (
            node.kind == "dispatch"
            and node.status == "done"
            and len(node.children) >= DISPATCH_AUTO_COLLAPSE_SIZE
        ):
            collapsed.add(node_id)
    return frozenset(collapsed)


def _refresh(state: NavigationState, context: NavigationContext) -> NavigationState:
    live = frozenset(context.nodes)
    manual = {
        node_id: collapsed
        for node_id, collapsed in state.manual_branches
        if node_id in live
    }
    selected = state.selected
    if state.zone == "graph" and selected not in live:
        selected = state.active_root
    drill_open = state.drill_open
    zone = state.zone
    drill_cursor = state.drill_cursor
    drill_expanded = frozenset(
        row for row in state.drill_expanded if _valid_drill_row(row, context)
    )
    selected_node = context.nodes.get(selected or "")
    if drill_open and (selected_node is None or selected_node.kind == "dispatch"):
        drill_open = False
        zone = "graph"
        drill_cursor = 0
        drill_expanded = frozenset()
    elif drill_open and not _valid_drill_row(drill_cursor, context):
        drill_cursor = _first_drill_row(context)
    refreshed = replace(
        state,
        selected=selected,
        zone=zone,
        drill_open=drill_open,
        drill_cursor=drill_cursor,
        drill_expanded=drill_expanded,
        manual_branches=frozenset(manual.items()),
    )
    return replace(
        refreshed,
        collapsed=_effective_collapsed(refreshed, context, manual),
    )


def _reachable_branches(context: NavigationContext, root: str) -> frozenset[str]:
    branches = set()
    pending = [root]
    visited = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        children = _children(context, node_id)
        if children:
            branches.add(node_id)
            pending.extend(children)
    return frozenset(branches)


def _first_drill_row(context: NavigationContext) -> int:
    return -1 if context.instruction_present else 0


def _valid_drill_row(row: object, context: NavigationContext) -> bool:
    if not isinstance(row, int):
        return False
    if row == -1:
        return context.instruction_present
    return 0 <= row < context.action_count


def _move_graph(
    state: NavigationState, direction: object, context: NavigationContext
) -> NavigationEffect:
    selected = state.selected if state.selected in context.visible_order else state.active_root
    if direction in ("up", "down") and selected in context.visible_order:
        index = context.visible_order.index(selected)
        if direction == "up" and index == 0:
            return _effect(
                replace(state, selected=selected, zone="tabs"),
                "render",
                previous=state,
            )
        offset = -1 if direction == "up" else 1
        target = index + offset
        if 0 <= target < len(context.visible_order):
            selected = context.visible_order[target]
        return _effect(
            replace(state, selected=selected),
            "render",
            "scroll-node",
            previous=state,
        )
    if direction in ("left", "right") and selected in context.nodes:
        if not _children(context, selected):
            return NavigationEffect(state, ())
        changed = _with_branch_choice(state, selected, direction == "left")
        return _effect(changed, "render", "scroll-node", previous=state)
    return NavigationEffect(state, ())


def _move_drill(
    state: NavigationState, direction: object, context: NavigationContext
) -> NavigationEffect:
    cursor = state.drill_cursor
    if direction == "down":
        if cursor == -1 and context.action_count:
            cursor = 0
        elif 0 <= cursor + 1 < context.action_count:
            cursor += 1
    elif direction == "up":
        if cursor == -1 or (cursor == 0 and not context.instruction_present):
            changed = replace(
                state,
                zone="graph",
                drill_open=False,
                drill_cursor=0,
                drill_expanded=frozenset(),
            )
            return _effect(changed, "render", "scroll-node", previous=state)
        if cursor == 0:
            cursor = -1
        elif cursor > 0:
            cursor -= 1
    elif direction in ("left", "right") and _valid_drill_row(cursor, context):
        expanded = set(state.drill_expanded)
        if direction == "right":
            expanded.add(cursor)
        else:
            expanded.discard(cursor)
        changed = replace(state, drill_expanded=frozenset(expanded))
        return _effect(changed, "render", "scroll-drill", previous=state)
    else:
        return NavigationEffect(state, ())
    changed = replace(state, drill_cursor=cursor)
    return _effect(changed, "render", "scroll-drill", previous=state)


def _move_to_edge(
    state: NavigationState, edge: object, context: NavigationContext
) -> NavigationEffect:
    if edge not in ("first", "last"):
        return NavigationEffect(state, ())
    if state.zone == "graph" and context.visible_order:
        selected = context.visible_order[0 if edge == "first" else -1]
        return _effect(
            replace(state, selected=selected),
            "render",
            "scroll-node",
            previous=state,
        )
    if state.zone == "drill":
        rows = ((-1,) if context.instruction_present else ()) + tuple(
            range(context.action_count)
        )
        if rows:
            cursor = rows[0 if edge == "first" else -1]
            return _effect(
                replace(state, drill_cursor=cursor),
                "render",
                "scroll-drill",
                previous=state,
            )
    return NavigationEffect(state, ())


def _move_page(
    state: NavigationState, distance: object, context: NavigationContext
) -> NavigationEffect:
    if not isinstance(distance, int) or distance == 0:
        return NavigationEffect(state, ())
    if state.zone == "graph" and context.visible_order:
        selected = (
            state.selected
            if state.selected in context.visible_order
            else state.active_root
        )
        if selected not in context.visible_order:
            return NavigationEffect(state, ())
        index = context.visible_order.index(selected)
        target_index = max(
            0, min(index + distance, len(context.visible_order) - 1)
        )
        target = context.visible_order[target_index]
        return _effect(
            replace(state, selected=target),
            "render",
            "scroll-node",
            previous=state,
        )
    if state.zone == "drill":
        rows = ((-1,) if context.instruction_present else ()) + tuple(
            range(context.action_count)
        )
        if not rows:
            return NavigationEffect(state, ())
        index = rows.index(state.drill_cursor) if state.drill_cursor in rows else 0
        cursor = rows[max(0, min(index + distance, len(rows) - 1))]
        return _effect(
            replace(state, drill_cursor=cursor),
            "render",
            "scroll-drill",
            previous=state,
        )
    return NavigationEffect(state, ())


def _set_all_branches(
    state: NavigationState, collapse: bool, context: NavigationContext
) -> NavigationEffect:
    branches = _reachable_branches(context, state.active_root)
    if not branches:
        return NavigationEffect(state, ())
    manual = _manual_map(state)
    manual.update((node_id, collapse) for node_id in branches)
    selected = state.selected
    if collapse and branches & _selected_ancestors(selected, context.nodes):
        selected = state.active_root
    changed = replace(
        state,
        selected=selected,
        manual_branches=frozenset(manual.items()),
    )
    changed = replace(
        changed,
        collapsed=_effective_collapsed(changed, context, manual),
    )
    return _effect(changed, "render", "scroll-node", previous=state)


def transition(
    state: NavigationState,
    event: NavigationEvent,
    context: NavigationContext,
) -> NavigationEffect:
    """Return next immutable navigation state and UI commands."""
    if event.kind not in _EVENT_KINDS:
        raise ValueError(f"unknown navigation event: {event.kind}")

    if event.kind == "refresh":
        return _effect(_refresh(state, context), "render", previous=state)

    if event.kind == "sync-roots":
        roots = tuple(context.roots)
        active = state.active_root
        reset = active not in roots
        if reset:
            active = "main" if "main" in roots else (roots[0] if roots else active)
        changed = replace(state, roots=roots, active_root=active)
        if reset:
            changed = replace(
                changed,
                selected=active,
                zone="graph" if state.zone == "drill" else state.zone,
                drill_open=False,
                drill_cursor=0,
                drill_expanded=frozenset(),
            )
        return _effect(changed, "cancel-message", "render", previous=state)

    if event.kind == "activate-root":
        root = event.value
        if not isinstance(root, str) or root not in state.roots:
            return NavigationEffect(state, ())
        changed = replace(
            state,
            active_root=root,
            selected=root,
            zone="graph" if state.zone == "drill" else state.zone,
            drill_open=False,
            drill_cursor=0,
            drill_expanded=frozenset(),
        )
        return _effect(changed, "cancel-message", "render", previous=state)

    if event.kind == "toggle-view":
        index = _VIEWS.index(state.view)
        changed = replace(state, view=_VIEWS[(index + 1) % len(_VIEWS)])
        return _effect(changed, "render", previous=state)

    if state.view != "tree":
        return NavigationEffect(state, ())

    if event.kind == "toggle-skills":
        return _effect(
            replace(state, show_skills=not state.show_skills),
            "render",
            previous=state,
        )

    if event.kind == "move":
        if state.zone == "tabs":
            if event.value == "left":
                return NavigationEffect(state, ("prior-tab",))
            if event.value == "right":
                return NavigationEffect(state, ("next-tab",))
            if event.value == "down":
                changed = replace(state, zone="graph", selected=state.active_root)
                return _effect(changed, "render", "scroll-node", previous=state)
            return NavigationEffect(state, ())
        if state.zone == "graph":
            return _move_graph(state, event.value, context)
        if state.zone == "drill":
            return _move_drill(state, event.value, context)
        return NavigationEffect(state, ())

    if event.kind == "edge":
        return _move_to_edge(state, event.value, context)

    if event.kind == "page":
        return _move_page(state, event.value, context)

    if event.kind == "same-layer":
        if state.zone != "graph" or event.value not in ("up", "down"):
            return NavigationEffect(state, ())
        selected = state.selected if state.selected in context.visible_order else None
        if selected is None:
            return NavigationEffect(state, ())
        depth = context.depths.get(selected, 0)
        index = context.visible_order.index(selected)
        indexes = (
            range(index - 1, -1, -1)
            if event.value == "up"
            else range(index + 1, len(context.visible_order))
        )
        target = next(
            (
                context.visible_order[i]
                for i in indexes
                if context.depths.get(context.visible_order[i], 0) == depth
            ),
            selected,
        )
        return _effect(
            replace(state, selected=target),
            "render",
            "scroll-node",
            previous=state,
        )

    if event.kind in ("next-active", "previous-active"):
        if state.zone != "graph" or not context.visible_order:
            return NavigationEffect(state, ())
        selected = (
            state.selected
            if state.selected in context.visible_order
            else state.active_root
        )
        if selected not in context.visible_order:
            return NavigationEffect(state, ())
        start = context.visible_order.index(selected)
        if event.kind == "next-active":
            order = (
                context.visible_order[start + 1 :]
                + context.visible_order[: start + 1]
            )
        else:
            order = tuple(reversed(context.visible_order[:start])) + tuple(
                reversed(context.visible_order[start:])
            )
        target = next(
            (
                node_id
                for node_id in order
                if context.nodes[node_id].kind != "dispatch"
                and context.nodes[node_id].status in ("running", "error")
            ),
            selected,
        )
        return _effect(
            replace(state, selected=target),
            "render",
            "scroll-node",
            previous=state,
        )

    if event.kind == "branch":
        if state.zone != "graph" or state.selected not in context.nodes:
            return NavigationEffect(state, ())
        if not _children(context, state.selected):
            return NavigationEffect(state, ())
        collapse = {
            "close": True,
            "open": False,
            "toggle": state.selected not in state.collapsed,
        }.get(event.value)
        if collapse is None:
            return NavigationEffect(state, ())
        changed = _with_branch_choice(state, state.selected, collapse)
        return _effect(changed, "render", "scroll-node", previous=state)

    if event.kind == "all-branches":
        if state.zone != "graph" or not isinstance(event.value, bool):
            return NavigationEffect(state, ())
        return _set_all_branches(state, event.value, context)

    if event.kind == "toggle-all":
        if state.zone != "graph":
            return NavigationEffect(state, ())
        branches = _reachable_branches(context, state.active_root)
        if not branches:
            return NavigationEffect(state, ())
        return _set_all_branches(
            state, not branches.issubset(state.collapsed), context
        )

    if event.kind == "enter":
        if state.zone == "tabs":
            changed = replace(state, zone="graph", selected=state.active_root)
            return _effect(changed, "render", "scroll-node", previous=state)
        if state.zone == "drill":
            if not _valid_drill_row(state.drill_cursor, context):
                return NavigationEffect(state, ())
            expanded = set(state.drill_expanded)
            if state.drill_cursor in expanded:
                expanded.remove(state.drill_cursor)
            else:
                expanded.add(state.drill_cursor)
            changed = replace(state, drill_expanded=frozenset(expanded))
            return _effect(changed, "render", "scroll-drill", previous=state)
        if state.zone != "graph":
            return NavigationEffect(state, ())
        node = context.nodes.get(state.selected or "")
        if node is None:
            return NavigationEffect(state, ())
        if node.kind == "dispatch":
            changed = _with_branch_choice(
                state, node.id, node.id not in state.collapsed
            )
            return _effect(changed, "render", "scroll-node", previous=state)
        changed = replace(
            state,
            zone="drill",
            drill_open=True,
            drill_cursor=_first_drill_row(context),
            drill_expanded=frozenset(),
        )
        return _effect(changed, "render", "scroll-drill", previous=state)

    if event.kind == "back":
        if state.zone == "drill":
            changed = replace(
                state,
                zone="graph",
                drill_open=False,
                drill_cursor=0,
                drill_expanded=frozenset(),
            )
            return _effect(changed, "cancel-message", "render", "scroll-node", previous=state)
        if state.zone == "graph":
            return _effect(replace(state, zone="tabs"), "render", previous=state)
        return NavigationEffect(state, ())

    if event.kind == "click-node":
        node_id = event.value
        if not isinstance(node_id, str) or node_id not in context.visible_order:
            return NavigationEffect(state, ())
        changed = replace(
            state,
            selected=node_id,
            zone="graph",
            drill_open=False,
            drill_cursor=0,
            drill_expanded=frozenset(),
        )
        return _effect(changed, "cancel-message", "render", "scroll-node", previous=state)

    if event.kind == "click-drill-row":
        if state.zone != "drill" or not state.drill_open:
            return NavigationEffect(state, ())
        if not _valid_drill_row(event.value, context):
            return NavigationEffect(state, ())
        changed = replace(state, drill_cursor=event.value)
        return _effect(changed, "render", "scroll-drill", previous=state)

    return NavigationEffect(state, ())
