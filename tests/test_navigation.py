from __future__ import annotations

import pytest

from quakepro.navigation import (
    NavigationContext,
    NavigationEvent,
    NavigationState,
    transition,
)
from quakepro.session_model import Node, Step


def state(**changes):
    values = {
        "roots": ("main",),
        "active_root": "main",
        "view": "tree",
        "zone": "graph",
        "selected": "main",
        "collapsed": frozenset(),
        "manual_branches": frozenset(),
        "show_skills": False,
        "drill_open": False,
        "drill_cursor": 0,
        "drill_expanded": frozenset(),
    }
    values.update(changes)
    return NavigationState(**values)


def context(nodes=None, **changes):
    nodes = nodes or {"main": Node("main", "main", kind="main")}
    values = {
        "roots": ("main",),
        "nodes": nodes,
        "visible_order": tuple(nodes),
        "depths": {node_id: 0 for node_id in nodes},
        "stop_kinds": (),
        "instruction_present": False,
        "action_count": 0,
    }
    values.update(changes)
    return NavigationContext(**values)


def test_navigation_state_copies_branch_inputs_and_rejects_duplicate_ids():
    collapsed = {"branch"}
    choices = [("branch", True)]

    current = state(collapsed=collapsed, manual_branches=choices)
    collapsed.clear()
    choices[0] = ("branch", False)

    assert current.collapsed == frozenset({"branch"})
    assert current.manual_branches == frozenset({("branch", True)})
    assert isinstance(current.collapsed, frozenset)
    assert isinstance(current.manual_branches, frozenset)

    with pytest.raises(ValueError, match="duplicate.*branch"):
        state(manual_branches=[("branch", True), ("branch", False)])


def test_transition_is_pure_and_unknown_events_are_rejected():
    nodes = {
        "main": Node("main", "main", kind="main", children=["branch"]),
        "branch": Node("branch", "branch", parent="main", children=["leaf"]),
        "leaf": Node("leaf", "leaf", parent="branch"),
    }
    original = state(selected="branch")
    current_context = context(
        nodes,
        visible_order=("main", "branch", "leaf"),
        depths={"main": 0, "branch": 1, "leaf": 2},
    )

    first = transition(original, NavigationEvent("move", "left"), current_context)
    second = transition(original, NavigationEvent("move", "left"), current_context)

    assert first == second
    assert original.collapsed == frozenset()
    assert original.manual_branches == frozenset()
    assert first.state.collapsed == frozenset({"branch"})
    assert first.state.manual_branches == frozenset({("branch", True)})
    assert first.state.manual_branches is not original.manual_branches

    with pytest.raises(ValueError):
        transition(original, NavigationEvent("not-an-event"), current_context)

    invalid = state(view="timeline", zone="tabs", selected=None)
    assert transition(invalid, NavigationEvent("move", "down"), current_context).state == invalid


def test_refresh_applies_dispatch_defaults_and_preserves_forced_manual_choice():
    nodes = {"main": Node("main", "main", kind="main")}
    for node_id, status, child_count in (
        ("done-large", "done", 4),
        ("done-small", "done", 3),
        ("running", "running", 4),
        ("error", "error", 4),
    ):
        children = [f"{node_id}:{index}" for index in range(child_count)]
        nodes[node_id] = Node(
            node_id,
            node_id,
            parent="main",
            kind="dispatch",
            status=status,
            children=children,
        )
        nodes["main"].children.append(node_id)
        for child_id in children:
            nodes[child_id] = Node(child_id, child_id, parent=node_id, status=status)

    current_context = context(nodes)
    refreshed = transition(
        state(
            collapsed=frozenset({"ghost"}),
            manual_branches=frozenset(
                {("running", True), ("done-large", False), ("ghost", True)}
            ),
        ),
        NavigationEvent("refresh"),
        current_context,
    ).state

    assert refreshed.collapsed == frozenset({"running"})
    assert refreshed.manual_branches == frozenset(
        {("running", True), ("done-large", False)}
    )

    defaults = transition(
        state(), NavigationEvent("refresh"), current_context
    ).state.collapsed
    assert "done-large" in defaults
    assert "done-small" not in defaults
    assert "running" not in defaults
    assert "error" not in defaults


def test_selected_descendant_temporarily_opens_dispatch_then_manual_choice_resumes():
    children = [f"dispatch:{index}" for index in range(4)]
    nodes = {
        "main": Node("main", "main", kind="main", children=["dispatch"]),
        "dispatch": Node(
            "dispatch",
            "dispatch",
            parent="main",
            kind="dispatch",
            status="done",
            children=children,
        ),
        **{
            child_id: Node(child_id, child_id, parent="dispatch", status="done")
            for child_id in children
        },
    }
    current_context = context(nodes)
    choice = frozenset({("dispatch", True)})

    selected_child = transition(
        state(selected=children[0], collapsed=frozenset({"dispatch"}), manual_branches=choice),
        NavigationEvent("refresh"),
        current_context,
    ).state
    assert "dispatch" not in selected_child.collapsed
    assert selected_child.manual_branches == choice

    moved_away = transition(
        state(selected="main", manual_branches=selected_child.manual_branches),
        NavigationEvent("refresh"),
        current_context,
    ).state
    assert "dispatch" in moved_away.collapsed


def test_toggle_all_visits_collapsed_descendants_and_stops_at_tab_boundaries():
    nodes = {
        "main": Node("main", "main", kind="main", children=["outer", "team:red"]),
        "outer": Node("outer", "outer", parent="main", children=["inner"]),
        "inner": Node("inner", "inner", parent="outer", children=["leaf"]),
        "leaf": Node("leaf", "leaf", parent="inner"),
        "team:red": Node(
            "team:red", "team", parent="main", kind="team", children=["teammate"]
        ),
        "teammate": Node("teammate", "teammate", parent="team:red"),
    }
    expanded_context = context(
        nodes,
        visible_order=("main", "outer", "inner", "leaf", "team:red"),
        depths={"main": 0, "outer": 1, "inner": 2, "leaf": 3, "team:red": 1},
        stop_kinds=("team",),
    )
    collapsed_context = context(
        nodes,
        visible_order=("main", "outer", "team:red"),
        depths={"main": 0, "outer": 1, "team:red": 1},
        stop_kinds=("team",),
    )

    outer_closed = transition(
        state(selected="outer"),
        NavigationEvent("branch", "close"),
        expanded_context,
    ).state
    collapsed = transition(
        outer_closed, NavigationEvent("toggle-all"), collapsed_context
    ).state
    assert collapsed.collapsed == frozenset({"main", "outer", "inner"})
    assert collapsed.manual_branches == frozenset(
        {("main", True), ("outer", True), ("inner", True)}
    )
    assert not any(node_id == "team:red" for node_id, _ in collapsed.manual_branches)

    opened = transition(
        collapsed, NavigationEvent("toggle-all"), collapsed_context
    ).state
    assert opened.collapsed == frozenset()
    assert opened.manual_branches == frozenset(
        {("main", False), ("outer", False), ("inner", False)}
    )


def test_main_navigation_events_cover_tabs_graph_views_and_active_selection():
    nodes = {
        "main": Node(
            "main", "main", kind="main", status="done", children=["done", "live", "batch"]
        ),
        "done": Node("done", "done", parent="main", status="done"),
        "live": Node("live", "live", parent="main", status="running"),
        "batch": Node("batch", "batch", parent="main", kind="dispatch", status="error"),
        "wf:one": Node("wf:one", "workflow", kind="workflow"),
    }
    current_context = context(
        nodes,
        roots=("main", "wf:one"),
        visible_order=("main", "done", "live", "batch"),
        depths={"main": 0, "done": 1, "live": 1, "batch": 1, "wf:one": 0},
    )

    synced = transition(
        state(roots=("main", "stale"), active_root="stale", selected="gone", zone="drill",
              drill_open=True, drill_cursor=2, drill_expanded=frozenset({2})),
        NavigationEvent("sync-roots"),
        current_context,
    ).state
    assert synced.roots == ("main", "wf:one")
    assert synced.active_root == "main"
    assert synced.selected == "main"
    assert synced.zone == "graph"
    assert not synced.drill_open

    activated = transition(
        synced, NavigationEvent("activate-root", "wf:one"), current_context
    ).state
    assert (activated.active_root, activated.selected) == ("wf:one", "wf:one")

    tabs = state(zone="tabs", selected=None)
    assert transition(tabs, NavigationEvent("move", "left"), current_context).commands == (
        "prior-tab",
    )
    assert transition(tabs, NavigationEvent("move", "right"), current_context).commands == (
        "next-tab",
    )
    entered = transition(tabs, NavigationEvent("enter"), current_context).state
    assert (entered.zone, entered.selected) == ("graph", "main")

    next_active = transition(
        state(selected="done"), NavigationEvent("next-active"), current_context
    ).state
    assert next_active.selected == "live"
    wrapped = transition(
        state(selected="live"), NavigationEvent("next-active"), current_context
    ).state
    assert wrapped.selected == "live"

    toggled = transition(state(), NavigationEvent("toggle-view"), current_context).state
    assert toggled.view == "timeline"
    tree_again = transition(
        toggled, NavigationEvent("toggle-view"), current_context,
    ).state
    assert tree_again.view == "tree"
    assert transition(tree_again, NavigationEvent("toggle-skills"), current_context).state.show_skills


def test_vim_edges_pages_branches_and_reverse_active_jump():
    nodes = {
        "main": Node(
            "main", "main", kind="main", children=["one", "two", "three"]
        ),
        "one": Node("one", "one", parent="main", status="running"),
        "two": Node("two", "two", parent="main", status="done"),
        "three": Node("three", "three", parent="main", status="error"),
    }
    current_context = context(
        nodes,
        visible_order=("main", "one", "two", "three"),
        depths={"main": 0, "one": 1, "two": 1, "three": 1},
    )

    last = transition(state(), NavigationEvent("edge", "last"), current_context).state
    assert last.selected == "three"
    first = transition(last, NavigationEvent("edge", "first"), current_context).state
    assert first.selected == "main"
    paged = transition(state(), NavigationEvent("page", 2), current_context).state
    assert paged.selected == "two"
    previous = transition(
        last, NavigationEvent("previous-active"), current_context
    ).state
    assert previous.selected == "one"

    closed = transition(
        state(), NavigationEvent("branch", "close"), current_context
    ).state
    assert closed.collapsed == frozenset({"main"})
    opened = transition(
        closed, NavigationEvent("branch", "open"), current_context
    ).state
    assert opened.collapsed == frozenset()


def test_graph_move_layer_click_dispatch_enter_and_back_events():
    nodes = {
        "main": Node("main", "main", kind="main", children=["first", "second"]),
        "first": Node("first", "first", parent="main", children=["nested"]),
        "nested": Node("nested", "nested", parent="first"),
        "second": Node("second", "second", parent="main"),
        "dispatch": Node(
            "dispatch",
            "dispatch",
            parent="main",
            kind="dispatch",
            children=["worker"],
        ),
        "worker": Node("worker", "worker", parent="dispatch"),
    }
    current_context = context(
        nodes,
        visible_order=("main", "first", "nested", "second", "dispatch", "worker"),
        depths={"main": 0, "first": 1, "nested": 2, "second": 1,
                "dispatch": 1, "worker": 2},
    )

    assert transition(
        state(selected="first"), NavigationEvent("move", "down"), current_context
    ).state.selected == "nested"
    same_layer = transition(
        state(selected="first"), NavigationEvent("same-layer", "down"), current_context
    ).state
    assert same_layer.selected == "second"
    assert transition(
        state(selected="main"), NavigationEvent("move", "up"), current_context
    ).state.zone == "tabs"

    clicked = transition(
        state(selected="first", zone="drill", drill_open=True),
        NavigationEvent("click-node", "second"),
        current_context,
    ).state
    assert (clicked.selected, clicked.zone, clicked.drill_open) == ("second", "graph", False)
    assert transition(
        clicked, NavigationEvent("click-node", "stale"), current_context
    ).state == clicked

    collapsed = transition(
        state(selected="dispatch"), NavigationEvent("enter"), current_context
    ).state
    assert collapsed.collapsed == frozenset({"dispatch"})
    assert collapsed.manual_branches == frozenset({("dispatch", True)})
    reopened = transition(
        collapsed, NavigationEvent("enter"), current_context
    ).state
    assert "dispatch" not in reopened.collapsed
    assert reopened.manual_branches == frozenset({("dispatch", False)})
    assert transition(
        reopened, NavigationEvent("back"), current_context
    ).state.zone == "tabs"


def test_drill_events_keep_cursor_and_expansion_in_valid_range():
    nodes = {
        "main": Node("main", "main", kind="main", children=["agent"]),
        "agent": Node(
            "agent",
            "agent",
            parent="main",
            detail="instructions",
            steps=[Step("text", "one"), Step("tool", "two")],
        ),
    }
    current_context = context(
        nodes,
        visible_order=("main", "agent"),
        instruction_present=True,
        action_count=2,
    )

    opened = transition(
        state(selected="agent"), NavigationEvent("enter"), current_context
    ).state
    assert (opened.zone, opened.drill_open, opened.drill_cursor) == ("drill", True, -1)

    expanded = transition(opened, NavigationEvent("enter"), current_context).state
    assert expanded.drill_expanded == frozenset({-1})
    first_action = transition(
        expanded, NavigationEvent("move", "down"), current_context
    ).state
    assert first_action.drill_cursor == 0
    clicked = transition(
        first_action, NavigationEvent("click-drill-row", 1), current_context
    ).state
    assert clicked.drill_cursor == 1
    assert transition(
        clicked, NavigationEvent("click-drill-row", 8), current_context
    ).state == clicked

    closed = transition(clicked, NavigationEvent("back"), current_context).state
    assert (closed.zone, closed.drill_open) == ("graph", False)
    assert closed.drill_expanded == frozenset()
