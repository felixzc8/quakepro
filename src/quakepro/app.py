"""QuakePro — a live circle-tree TUI for Claude Code and Codex sessions.

Run beside an agent session in a tmux pane:
    quakepro                               # auto-attach to the newest session
    quakepro --provider codex
    ~/.claude/quakepro/quakepro --project myproject
    ~/.claude/quakepro/quakepro --file <session>.jsonl

Tabs across the top: `session` for the main loop and its Task/Agent sub-agents,
plus one tab per Claude workflow run and agent team. Each tab draws that run as
an indented tree with neutral status icons (✓ done, ▶ running, … waiting, ✗ error)
joined to their parent by curved branches, like Claude Code's own /workflow view.
Click-and-drag anywhere in a pane to pan it (there are no scrollbars).
Keys: ↑↓ or jk move through the agent tree · ⇧↑↓ jump between agents of the same layer · →
expand a node's children, ← collapse them · ⏎ toggle a dispatch or drill into an agent's action
graph (a bottom panel opens) · its available instructions head the panel under an
"instructions" header (↑ from the first action scrolls up to read them) · ↑↓ walk its
actions, → / ⏎ reveal a step's exact input & output (↑↓ then page through a long body),
← collapse it · c collapse/expand all branches · a next active agent · ? keys · esc close · q quit
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
from dataclasses import replace
from datetime import datetime

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static, Tab, Tabs

from .agent_roles import classify_roles, role_icon, role_style
from .navigation import (
    NavigationContext,
    NavigationEvent,
    NavigationState,
    transition,
)
from .overview import RunOverview, resolve_roots
from .sessions import PROVIDERS, latest_session, open_model
from .graph import (render_tree, render_actions, render_task_board, task_style,
                   status_glyph, status_icon_style, sanitize_inline_text, sanitize_text,
                   wrap_text, render_timeline, ACTION_DETAIL, HEADER)
from .observation import ObservationError, SessionObservation, reconcile_async
from .session_model import SessionModel
from .session_story import (
    SessionStory,
    SessionStoryProjector,
    project_session_story,
    project_session_timeline,
)
from .team_messaging import TeammateMessenger, default_messenger


VIEW_MODES = {
    "tree": "Tree",
    "timeline": "Timeline",
}

SHORTCUTS_HELP = """QuakePro shortcuts

Move
  j / ↓        next node or action
  k / ↑        previous node or action
  h / ←        collapse
  l / →        expand
  gg / G       first / last item
  ctrl-d/u     half-page down / up
  ctrl-f/b     full-page down / up
  [[ / ]]      previous / next same-depth node
  0 / $        left / right edge

Tabs and branches
  gt / gT      next / previous tab
  zo / zc      open / close branch
  za           toggle branch
  zR / zM      open / close all branches
  zt / zz / zb place selection at top / middle / bottom

Actions
  enter        drill in or toggle detail
  a / ]a       next running or failed agent
  [a           previous running or failed agent
  c            toggle all branches
  v            switch Tree / Timeline
  s            toggle skill details
  i            message selected teammate
  esc          back or cancel message
  ?            show / close this page
  q            quit

Arrow and shift-arrow shortcuts remain available.
"""


class PanClick(Message):
    """A press-and-release with no drag — a click, at content coords (x, y)."""
    def __init__(self, source: "PanScroll", x: int, y: int) -> None:
        super().__init__()
        self.source = source
        self.x = x
        self.y = y


class PanScroll(ScrollableContainer):
    """A ScrollableContainer you move by click-and-drag instead of the trackpad.
    Two-finger / wheel scrolling is swallowed entirely — on this terminal it bounced
    sideways and never settled — so the only way to move the view is to press and
    drag, panning the content under the pointer. Scrollbars are hidden via CSS.
    A press with no drag posts a PanClick so the app can move the cursor there."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pan: tuple[int, int, int, int] | None = None
        self._moved = False

    def _swallow(self, event: events.Event) -> None:
        event.stop()

    # Kill every wheel/two-finger axis; dragging is the only way to scroll.
    _on_mouse_scroll_down = _swallow
    _on_mouse_scroll_up = _swallow
    _on_mouse_scroll_right = _swallow
    _on_mouse_scroll_left = _swallow

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.capture_mouse()
        self._pan = (event.screen_x, event.screen_y, self.scroll_offset.x, self.scroll_offset.y)
        self._moved = False
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._pan is None:
            return
        sx, sy, ox, oy = self._pan
        if event.screen_x != sx or event.screen_y != sy:
            self._moved = True
        self.scroll_to(ox - (event.screen_x - sx), oy - (event.screen_y - sy),
                       animate=False, force=True)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._pan is not None:
            self.release_mouse()
            self._pan = None
            if not self._moved:
                self.post_message(PanClick(self, self.scroll_offset.x + event.x,
                                           self.scroll_offset.y + event.y))
            event.stop()


class WholeKeyFooter(Footer):
    async def _remove_overflow(self) -> None:
        keys = list(self.query("FooterKey"))
        palette = next((key for key in keys if key.has_class("-command-palette")), None)
        boundary = palette.region.x if palette is not None else self.region.right
        overflow = [
            key for key in keys
            if key is not palette and key.region.right > boundary
        ]
        if overflow:
            await self.remove_children(overflow)

    async def recompose(self) -> None:
        await super().recompose()
        self.call_after_refresh(self._remove_overflow)

    def on_resize(self, _event: events.Resize) -> None:
        self.call_after_refresh(self.recompose)


class ShortcutsScreen(ModalScreen[None]):
    CSS = """
    ShortcutsScreen { align: center middle; background: $background 70%; }
    #shortcuts-scroll {
        width: 76;
        max-width: 94%;
        height: 34;
        max-height: 92%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #shortcuts { width: auto; height: auto; }
    """
    BINDINGS = [
        Binding("escape,question_mark,q", "close_help", show=False),
    ]

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="shortcuts-scroll"):
            yield Static(Text(SHORTCUTS_HELP), id="shortcuts")

    def action_close_help(self) -> None:
        self.dismiss()


def _tabid(nid: str) -> str:
    """Return a stable, reversible Textual widget ID for a model node ID."""
    return "t_" + nid.encode("utf-8").hex()


class QuakePro(App):
    CSS = """
    Tabs { dock: top; }
    PanScroll { scrollbar-size: 0 0; }
    #canvaswrap { width: 1fr; height: 1fr; align: left top; }
    #canvas { padding: 1 2; width: auto; box-sizing: content-box; }
    #drill { dock: bottom; height: 45%; display: none; padding: 0 1; }
    #drillhead { height: auto; max-height: 45%; border-bottom: solid gray; }
    #drillinstr { width: 1fr; }
    #drillscroll { height: 1fr; }
    #drillbody { width: auto; }
    #msginput { dock: bottom; display: none; margin-top: 1; }
    """
    BINDINGS = [
        ("q", "quit", "quit"),
        ("left", "nav('left')", "collapse"),
        ("right", "nav('right')", "expand"),
        ("up", "nav('up')", "▲"),
        ("down", "nav('down')", "▼"),
        Binding("h", "nav('left')", show=False),
        Binding("l", "nav('right')", show=False),
        Binding("k", "nav('up')", show=False),
        Binding("j", "nav('down')", show=False),
        ("shift+up", "nav_layer('up')", "▲ layer"),
        ("shift+down", "nav_layer('down')", "▼ layer"),
        ("shift+left", "switch_tab('left')", "◀ tab"),
        ("shift+right", "switch_tab('right')", "tab ▶"),
        ("a", "vim_a", "next active"),
        ("c", "vim_c", "toggle branches"),
        ("s", "toggle_skills", "skills"),
        ("v", "toggle_view", "/".join(VIEW_MODES.values())),
        ("enter", "enter", "drill in"),
        ("i", "message", "message"),
        ("question_mark", "shortcuts", "keys"),
        ("escape", "back", "back"),
        Binding("ctrl+d", "vim_page('down', 'half')", show=False),
        Binding("ctrl+u", "vim_page('up', 'half')", show=False),
        Binding("ctrl+f", "vim_page('down', 'full')", show=False),
        Binding("ctrl+b", "vim_page('up', 'full')", show=False),
        Binding("G", "vim_edge('last')", show=False),
        Binding("0", "vim_horizontal_edge('left')", show=False),
        Binding("dollar_sign", "vim_horizontal_edge('right')", show=False),
        Binding("g", "vim_g", show=False),
        Binding("z", "vim_z", show=False),
        Binding("t", "vim_t", show=False),
        Binding("T", "vim_shift_t", show=False),
        Binding("o", "vim_o", show=False),
        Binding("b", "vim_b", show=False),
        Binding("R", "vim_shift_r", show=False),
        Binding("M", "vim_shift_m", show=False),
        Binding("left_square_bracket", "vim_left_bracket", show=False),
        Binding("right_square_bracket", "vim_right_bracket", show=False),
    ]

    def __init__(
        self,
        path: str,
        provider: str = "auto",
        model: SessionModel | None = None,
        observation=None,
        messenger: TeammateMessenger | None = None,
    ):
        super().__init__()
        self.model = model or open_model(path, provider)
        self.observation = observation or SessionObservation(self.model)
        self.messenger = messenger if messenger is not None else default_messenger()
        self.tab_root: dict[str, str] = {}   # tab id -> root node id
        self._navigation = NavigationState(
            roots=("main",),
            active_root="main",
            view="tree",
            zone="tabs",
            selected=None,
            collapsed=frozenset(),
            manual_branches=frozenset(),
            show_skills=False,
            drill_open=False,
            drill_cursor=0,
            drill_expanded=frozenset(),
        )
        self._drill_sel_row = 0              # first row of the selected action, for scroll-into-view
        self._drill_sel_end = 0              # last row of the selected action (incl. expanded body)
        self._note = ""                      # transient line shown atop the drill (send result)
        self._message_recipient: str | None = None
        self._updated_at = ""
        self._observation_error = ""
        self._order: list[str] = []          # last drawn DFS order; row i holds node i
        self._view: dict = {}                # last drawn id -> ViewNode (parent/children/depth)
        self._drill_rowmap: list[tuple] = [] # last drawn drill: (body row -> target: -1 instr | step idx)
        self._story_cache_key: tuple[int, str, int] | None = None
        self._story_cache: SessionStory | None = None
        self._story_projectors: dict[tuple[int, str], SessionStoryProjector] = {}
        self._vim_prefix: str | None = None
        self._vim_prefix_timer = None

    @property
    def active_root(self) -> str:
        return self._navigation.active_root

    @property
    def view_mode(self) -> str:
        return self._navigation.view

    @property
    def zone(self) -> str:
        return self._navigation.zone

    @property
    def selected(self) -> str | None:
        return self._navigation.selected

    @selected.setter
    def selected(self, node_id: str | None) -> None:
        if node_id is None:
            return
        self._navigate(NavigationEvent("click-node", node_id))

    @property
    def drill_open(self) -> bool:
        return self._navigation.drill_open

    @property
    def collapsed(self) -> frozenset[str]:
        return self._navigation.collapsed

    @property
    def show_skills(self) -> bool:
        return self._navigation.show_skills

    @property
    def drill_sel(self) -> int:
        return self._navigation.drill_cursor

    @property
    def drill_expanded(self) -> frozenset[int]:
        return self._navigation.drill_expanded

    def _navigation_context(
        self, roots: tuple[str, ...] | None = None
    ) -> NavigationContext:
        node = self.model.nodes.get(self.selected or "")
        steps = [step for step in node.steps if step.kind != "prompt"] if node else []
        instructions = bool(node and self._instructions(node))
        return NavigationContext(
            roots=roots if roots is not None else self._navigation.roots,
            nodes=self.model.nodes,
            visible_order=tuple(self._order),
            depths={node_id: self._vdepth(node_id) for node_id in self._order},
            stop_kinds=self._stop(),
            instruction_present=instructions,
            action_count=len(steps),
        )

    def _navigate(
        self,
        event: NavigationEvent,
        context: NavigationContext | None = None,
    ) -> None:
        previous = self._navigation
        effect = transition(
            previous, event, context or self._navigation_context()
        )
        self._navigation = effect.state
        self._apply_navigation_commands(effect.commands, previous)

    def _apply_navigation_commands(
        self, commands: tuple[str, ...], previous: NavigationState
    ) -> None:
        for command in commands:
            if command == "render":
                self._render_canvas()
            elif command == "scroll-node":
                self._scroll_to_selected()
            elif command == "scroll-drill":
                expanding = len(self.drill_expanded) > len(previous.drill_expanded)
                self._scroll_drill(to_top=expanding)
            elif command == "prior-tab":
                self._tabs.action_previous_tab()
            elif command == "next-tab":
                self._tabs.action_next_tab()
            elif command == "bell":
                self.bell()
            elif command == "focus-message":
                self._input.can_focus = True
                self._input.focus()
            elif command == "cancel-message":
                self._cancel_message()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Tabs(id="tabs")
        with PanScroll(id="canvaswrap"):
            yield Static("", id="canvas")
        with Vertical(id="drill"):
            with ScrollableContainer(id="drillhead"):
                yield Static("", id="drillinstr")
            with PanScroll(id="drillscroll"):
                yield Static("", id="drillbody")
            yield Input(id="msginput", placeholder="message this agent — enter to send, esc to cancel")
        yield WholeKeyFooter()

    def on_mount(self) -> None:
        # ansi-dark uses the terminal's own 16-color palette and default
        # background, so QuakePro blends into the surrounding terminal theme.
        try:
            self.theme = "ansi-dark"
        except Exception:
            pass
        self._tabs = self.query_one("#tabs", Tabs)
        self._canvas = self.query_one("#canvas", Static)
        self._canvaswrap = self.query_one("#canvaswrap", PanScroll)
        self._drill = self.query_one("#drill", Vertical)
        self._drillinstr = self.query_one("#drillinstr", Static)
        self._drillbody = self.query_one("#drillbody", Static)
        self._drillscroll = self.query_one("#drillscroll", PanScroll)
        self._input = self.query_one("#msginput", Input)
        # Keep all arrow keys flowing to our zone-aware handler: no child widget
        # may grab focus (Tabs has its own left/right bindings that would steal it).
        # The message Input stays focusable — it only holds focus while you type.
        self._tabs.can_focus = False
        self._drill.can_focus = False
        self.query_one("#drillhead", ScrollableContainer).can_focus = False
        self._drillscroll.can_focus = False
        self.query_one("#canvaswrap", PanScroll).can_focus = False
        self._input.can_focus = False
        self.set_focus(None)   # blur Tabs (auto-focused at mount) so arrows reach us
        self._mark_updated()
        self.sync_tabs()
        self.render_canvas()
        self.run_worker(self._observe_session(), name="session-observation")

    async def _observe_session(self) -> None:
        try:
            await reconcile_async(self.observation.reconcile)
            self._mark_updated()
            self.sync_tabs()
            self.render_canvas()
            if getattr(self.observation, "ended", False):
                self.exit()
                return
            async for _ in self.observation.changes():
                self._mark_updated()
                self.sync_tabs()
                self.render_canvas()
                if getattr(self.observation, "ended", False):
                    self.exit()
                    return
        except ObservationError as exc:
            self._observation_error = str(exc)
            self.render_canvas()
            self.notify(str(exc), title="Session observation stopped", severity="error")

    def _mark_updated(self) -> None:
        self._updated_at = datetime.now().astimezone().strftime("%H:%M:%S")

    def _update_subtitle(self) -> None:
        view = VIEW_MODES[self.view_mode]
        where = self.zone if self.view_mode == "tree" else "timeline"
        self.title = "QuakePro"
        provider = {"claude": "Claude Code", "codex": "Codex", "pi": "pi",
                    "overview": "Run overview"}.get(
            getattr(self.model, "provider", ""), "Session")
        state = f"error: {self._observation_error}" if self._observation_error else (
            f"updated {self._updated_at}" if self._updated_at else "waiting"
        )
        self.sub_title = f"{provider} · {state} · [{view} · {where}]"

    def _run_roots(self) -> list[str]:
        # Each workflow run and each agent team gets its own tab.
        return [n for n in self.model.nodes
                if n.startswith("wf:") or n.startswith("team:")]

    def sync_tabs(self) -> None:
        desired = [("session", "main")]
        for r in self._run_roots():
            label = self.model.nodes[r].label.replace("⚙ ", "") or "run"
            desired.append((sanitize_inline_text(label), r))
        desired_ids = {_tabid(nid) for _, nid in desired}
        for label, nid in desired:
            tid = _tabid(nid)
            if tid not in self.tab_root:
                self.tab_root[tid] = nid
                self._tabs.add_tab(Tab(Text(label), id=tid))
            else:
                tab = self._tabs.get_tab(tid)
                if tab is not None and tab.label_text != label:
                    tab.label = Text(label)
        stale_ids = [tid for tid in self.tab_root if tid not in desired_ids]
        active_was_stale = self._tabs.active in stale_ids
        if active_was_stale:
            self._note = ""
            self._tabs.active = _tabid("main")
        for tid in stale_ids:
            self.tab_root.pop(tid, None)
            self._tabs.remove_tab(tid)
        roots = tuple(node_id for _, node_id in desired)
        self._navigate(
            NavigationEvent("sync-roots"),
            self._navigation_context(roots),
        )

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        root = self.tab_root.get(event.tab.id, "main")
        if root != self.active_root:
            self._note = ""
        self._navigate(NavigationEvent("activate-root", root))

    # ---- tree navigation helpers (respect the active tab's stop_kinds) ------
    def _stop(self) -> tuple:
        # On the session tab, workflows and teams render as leaf circles —
        # they each have their own tab where they expand. A run overview has one
        # tab only, so everything it aggregates expands in place.
        if getattr(self.model, "provider", "") == "overview":
            return ()
        return ("workflow", "team") if self.active_root == "main" else ()

    def _vdepth(self, vid: str) -> int:
        vn = self._view.get(vid)
        return vn.depth if vn else 0

    def action_nav(self, direction: str) -> None:
        self._note = ""
        self._clear_vim_prefix()
        if self.view_mode == "timeline":
            delta = {
                "up": (0, -1),
                "down": (0, 1),
                "left": (-1, 0),
                "right": (1, 0),
            }.get(direction)
            if delta is not None:
                self._canvaswrap.scroll_relative(
                    x=delta[0], y=delta[1], animate=False, force=True
                )
            return
        if self.zone == "drill" and direction in ("up", "down"):
            if self._page_within(direction):
                return
        self._navigate(NavigationEvent("move", direction))

    def _clear_vim_prefix(self) -> None:
        self._vim_prefix = None
        if self._vim_prefix_timer is not None:
            self._vim_prefix_timer.stop()
            self._vim_prefix_timer = None

    def _set_vim_prefix(self, prefix: str) -> None:
        self._clear_vim_prefix()
        self._vim_prefix = prefix
        self._vim_prefix_timer = self.set_timer(1.0, self._clear_vim_prefix)

    def _take_vim_prefix(self) -> str | None:
        prefix = self._vim_prefix
        self._clear_vim_prefix()
        return prefix

    def action_vim_g(self) -> None:
        if self._vim_prefix == "g":
            self._clear_vim_prefix()
            self.action_vim_edge("first")
            return
        self._set_vim_prefix("g")

    def action_vim_z(self) -> None:
        if self._vim_prefix == "z":
            self._clear_vim_prefix()
            self._place_selection("middle")
            return
        self._set_vim_prefix("z")

    def action_vim_t(self) -> None:
        prefix = self._take_vim_prefix()
        if prefix == "g":
            self.action_switch_tab("right")
        elif prefix == "z":
            self._place_selection("top")

    def action_vim_shift_t(self) -> None:
        if self._take_vim_prefix() == "g":
            self.action_switch_tab("left")

    def action_vim_o(self) -> None:
        if self._take_vim_prefix() == "z":
            self._set_branch("open")

    def action_vim_c(self) -> None:
        prefix = self._take_vim_prefix()
        if prefix == "z":
            self._set_branch("close")
        elif prefix is None:
            self.action_toggle_branches()

    def action_vim_a(self) -> None:
        prefix = self._take_vim_prefix()
        if prefix == "z":
            self._set_branch("toggle")
        elif prefix == "[":
            self._navigate(NavigationEvent("previous-active"))
        elif prefix in (None, "]"):
            self._navigate(NavigationEvent("next-active"))

    def action_vim_b(self) -> None:
        if self._take_vim_prefix() == "z":
            self._place_selection("bottom")

    def action_vim_shift_r(self) -> None:
        if self._take_vim_prefix() == "z":
            self._navigate(NavigationEvent("all-branches", False))

    def action_vim_shift_m(self) -> None:
        if self._take_vim_prefix() == "z":
            self._navigate(NavigationEvent("all-branches", True))

    def action_vim_left_bracket(self) -> None:
        if self._vim_prefix == "[":
            self._clear_vim_prefix()
            self.action_nav_layer("up")
            return
        self._set_vim_prefix("[")

    def action_vim_right_bracket(self) -> None:
        if self._vim_prefix == "]":
            self._clear_vim_prefix()
            self.action_nav_layer("down")
            return
        self._set_vim_prefix("]")

    def action_vim_edge(self, edge: str) -> None:
        self._clear_vim_prefix()
        if self.view_mode == "timeline":
            self._canvaswrap.scroll_to(
                y=0 if edge == "first" else self._canvaswrap.max_scroll_y,
                animate=False,
                force=True,
            )
            return
        self._navigate(NavigationEvent("edge", edge))

    def action_vim_page(self, direction: str, amount: str) -> None:
        self._clear_vim_prefix()
        scroll = self._drillscroll if self.zone == "drill" else self._canvaswrap
        height = max(scroll.size.height - 2, 1)
        distance = height if amount == "full" else max(height // 2, 1)
        signed = distance if direction == "down" else -distance
        if self.view_mode == "timeline" or self.zone == "drill":
            scroll.scroll_relative(y=signed, animate=False, force=True)
            return
        self._navigate(NavigationEvent("page", signed))

    def action_vim_horizontal_edge(self, edge: str) -> None:
        self._clear_vim_prefix()
        scroll = self._drillscroll if self.zone == "drill" else self._canvaswrap
        scroll.scroll_to(
            x=0 if edge == "left" else scroll.max_scroll_x,
            animate=False,
            force=True,
        )

    def _set_branch(self, mode: str) -> None:
        if self.view_mode != "tree":
            return
        if self.zone == "drill":
            if mode == "toggle":
                self._navigate(NavigationEvent("enter"))
            else:
                self._navigate(
                    NavigationEvent("move", "right" if mode == "open" else "left")
                )
            return
        self._navigate(NavigationEvent("branch", mode))

    def _place_selection(self, place: str) -> None:
        if self.view_mode != "tree" or self.zone not in ("graph", "drill"):
            return
        scroll = self._drillscroll if self.zone == "drill" else self._canvaswrap
        row = self._drill_sel_row if self.zone == "drill" else (
            (self._view[self.selected].row + 1)
            if self.selected in self._view
            else 0
        )
        height = max(scroll.size.height, 1)
        offset = {"top": 0, "middle": height // 2, "bottom": height - 2}[place]
        scroll.scroll_to(y=max(row - offset, 0), animate=False, force=True)

    def _collapse(self, nid: str) -> None:
        if nid not in self.model.nodes:
            return
        synthetic = replace(
            self._navigation, view="tree", zone="graph", selected=nid
        )
        effect = transition(
            synthetic,
            NavigationEvent("move", "left"),
            self._navigation_context(),
        )
        self._navigation = replace(
            self._navigation,
            collapsed=effect.state.collapsed,
            manual_branches=effect.state.manual_branches,
        )

    def action_toggle_skills(self) -> None:
        self._navigate(NavigationEvent("toggle-skills"))

    def action_toggle_view(self) -> None:
        self._navigate(NavigationEvent("toggle-view"))

    def action_toggle_branches(self) -> None:
        self._navigate(NavigationEvent("toggle-all"))

    def _page_within(self, direction: str) -> bool:
        # Scroll one page through the content that belongs to the cursor before it
        # moves on, so long bodies are readable by keyboard. The readable span runs
        # from `lo` (the top of the instructions block when on the first step, else the
        # step's own row) through `hi` (the bottom of its expanded body). Returns True
        # while it scrolls; False once the edge is reached, letting the cursor move.
        top = self._drillscroll.scroll_offset.y
        vh = max(self._drillscroll.size.height, 1)
        page = max(vh - 2, 1)
        lo = 0 if self.drill_sel == 0 else self._drill_sel_row
        hi = self._drill_sel_end if self.drill_sel in self.drill_expanded else self._drill_sel_row
        if direction == "down":
            if hi <= top + vh - 1:
                return False
            self._drillscroll.scroll_to(y=min(top + page, hi - vh + 2),
                                        animate=False, force=True)
            return True
        if top <= lo:
            return False
        self._drillscroll.scroll_to(y=max(top - page, lo), animate=False, force=True)
        return True

    def action_switch_tab(self, direction: str) -> None:
        # Cycle tabs from any zone; on_tabs_tab_activated re-syncs selection.
        self._note = ""
        if direction == "left":
            self._tabs.action_previous_tab()
        else:
            self._tabs.action_next_tab()

    def action_nav_layer(self, direction: str) -> None:
        self._note = ""
        self._navigate(NavigationEvent("same-layer", direction))

    def action_next_active(self) -> None:
        self._navigate(NavigationEvent("next-active"))

    def _drill_steps(self) -> list:
        # The action steps the agent took. The prompt is not an action — it renders
        # as the "instructions" block atop the drill body instead (see _node_detail).
        n = self.model.nodes.get(self.selected or "")
        return [s for s in n.steps if s.kind != "prompt"] if n else []

    def _instructions(self, n) -> str:
        # The exact prompt the agent was handed: its captured prompt step, or the
        # short description if that's all we have.
        p = next((s for s in n.steps if s.kind == "prompt"), None)
        return (p.body if p else n.detail or "").strip()

    def _wrap_instr(self, text: str) -> list[str]:
        # Hard-wrap the instruction prose to the drill panel so a long single-line
        # prompt reads top-to-bottom instead of running off the right edge.
        w = max(self._drillscroll.size.width - 1, 20)
        return wrap_text(text, w)

    def _enter_drill(self) -> None:
        self._navigate(NavigationEvent("enter"))

    def _exit_drill(self) -> None:
        self._navigate(NavigationEvent("back"))

    def _scroll_drill(self, to_top: bool = False) -> None:
        # Keep the highlighted action on screen as you walk the action graph. On
        # expand, jump its header to the top of the frame so the freshly revealed
        # body is visible — a body opening below a bottom-of-frame step otherwise
        # gives no on-screen feedback.
        if self.zone != "drill":
            return
        row = self._drill_sel_row
        if to_top:
            self._drillscroll.scroll_to(y=row, animate=False, force=True)
            return
        top = self._drillscroll.scroll_offset.y
        vh = max(self._drillscroll.size.height, 1)
        if row < top:
            self._drillscroll.scroll_to(y=row, animate=False, force=True)
        elif row >= top + vh - 1:
            self._drillscroll.scroll_to(y=row - vh + 2, animate=False, force=True)

    def on_pan_click(self, event: PanClick) -> None:
        # A click (no drag) moves the cursor to whatever row was clicked.
        if event.source is self._canvaswrap:
            if self.view_mode == "tree":
                self._click_tree(event.y)
        elif event.source is self._drillscroll and self.drill_open:
            self._click_drill(event.y)

    def _click_tree(self, content_y: int) -> None:
        row = content_y - 1               # row 0 is the canvas's top padding
        target = next(
            (node_id for node_id in self._order
             if self._view.get(node_id) and self._view[node_id].row == row),
            None,
        )
        if target:
            self._note = ""
            self._navigate(NavigationEvent("click-node", target))

    def _click_drill(self, content_y: int) -> None:
        target = None
        for row, t in self._drill_rowmap:
            if row <= content_y:
                target = t
            else:
                break
        if target is not None:
            self._navigate(NavigationEvent("click-drill-row", target))

    def action_enter(self) -> None:
        self._navigate(NavigationEvent("enter"))

    def action_shortcuts(self) -> None:
        self._clear_vim_prefix()
        self.push_screen(ShortcutsScreen())

    def action_message(self) -> None:
        """Focus the message box for the highlighted agent (team teammates only —
        they're the one agent kind with a file inbox QuakePro can write to)."""
        if (self.view_mode != "tree" or self.zone not in ("graph", "drill")
                or not self._messageable(self.selected)):
            self.bell()
            return
        if not self.drill_open:
            self._enter_drill()
        if self._message_recipient != self.selected:
            self._input.value = ""
        self._message_recipient = self.selected
        self._input.can_focus = True
        self._input.focus()

    def action_back(self) -> None:
        if self.view_mode != "tree":
            return
        if self._input.has_focus:
            self._cancel_message()
            return
        self._navigate(NavigationEvent("back"))

    def _session_story(self, root_id: str) -> SessionStory:
        missing = object()
        revision = getattr(self.model, "visible_revision", missing)
        if revision is missing:
            revision = getattr(self.model, "_provider_reload_revision", missing)
        if type(revision) is not int or revision < 0:
            self._story_cache_key = None
            self._story_cache = None
            return project_session_story(self.model, root_id)
        key = (id(self.model), root_id, revision)
        if key == self._story_cache_key and self._story_cache is not None:
            return self._story_cache
        projector_key = (id(self.model), root_id)
        projector = self._story_projectors.get(projector_key)
        if projector is None:
            projector = SessionStoryProjector()
            self._story_projectors[projector_key] = projector
        story = projector.project(self.model, root_id)
        self._story_cache_key = key
        self._story_cache = story
        return story

    def _draw_canvas(self) -> None:
        if self.view_mode == "timeline":
            story = self._session_story(self.active_root)
            timeline = project_session_timeline(story)
            usable_width = max(self._canvaswrap.size.width - 4, 1)
            txt, width = render_timeline(timeline, usable_width)
            self._canvas.update(txt)
            self._canvas.styles.width = max(width, 1)
            return
        root = self.active_root
        stop = self._stop()
        sel = self.selected if self.zone in ("graph", "drill") else None
        usable_width = max(self._canvaswrap.size.width - 4, 1)
        txt, w, order, view = render_tree(
            self.model, root, stop_kinds=stop, selected=sel, collapsed=self.collapsed,
            skills=self.show_skills, available_width=usable_width,
        )
        self._order = order
        self._view = view
        if root.startswith("team:"):    # team tab: shared task list as a board below the tree
            tasks = self.model.task_list.get(root.split(":", 1)[1], [])
            if tasks:
                w = max(w, render_task_board(txt, tasks))
        self._canvas.update(txt)
        self._canvas.styles.width = max(w, 20)

    def _scroll_to_selected(self) -> None:
        # Keep the highlighted node on screen as you arrow around — the only auto
        # scroll there is, so a free drag-pan is never yanked back by a repaint.
        # Both axes: a drag-pan can put the cursor off-screen sideways too, so
        # up/down must recover x as well or it'd never come back into frame.
        if self.zone != "graph" or self.selected not in self._order:
            return
        vn = self._view.get(self.selected)
        row = (vn.row if vn else self._order.index(self.selected)) + 1
        off = self._canvaswrap.scroll_offset
        vh = max(self._canvaswrap.size.height, 1)
        vw = max(self._canvaswrap.size.width, 1)
        y, x = off.y, off.x
        if row < off.y:
            y = row
        elif row >= off.y + vh - 1:
            y = row - vh + 2
        if vn:
            if vn.x < off.x:
                x = vn.x
            elif vn.x + vn.w > off.x + vw:
                x = max(vn.x + vn.w - vw, 0)
        if (y, x) != (off.y, off.x):
            self._canvaswrap.scroll_to(x=x, y=y, animate=False, force=True)

    def render_canvas(self) -> None:
        effect = transition(
            self._navigation,
            NavigationEvent("refresh"),
            self._navigation_context(),
        )
        self._navigation = effect.state
        self._render_canvas()

    def _render_canvas(self) -> None:
        self._draw_canvas()
        show_drill = self.drill_open and self.view_mode == "tree"
        self._drill.styles.display = "block" if show_drill else "none"
        self._sync_input()
        self._update_subtitle()
        if show_drill:
            self.render_detail()

    # ---- messaging the highlighted agent -----------------------------------
    def _messageable(self, nid: str | None) -> bool:
        return bool(nid and self.messenger.target_for(self.model, nid))

    def _sync_input(self) -> None:
        show = (
            self.view_mode == "tree"
            and self.drill_open
            and self._messageable(self.selected)
        )
        self._input.styles.display = "block" if show else "none"
        recipient = self.selected if show else None
        if recipient != self._message_recipient:
            self._cancel_message()

    def _cancel_message(self) -> None:
        self._input.value = ""
        self._message_recipient = None
        if self._input.has_focus:
            self.set_focus(None)
        self._input.can_focus = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        nid = self._message_recipient
        self._input.value = ""
        self._message_recipient = None
        self.set_focus(None)
        self._input.can_focus = False
        target = self.messenger.target_for(self.model, nid) if nid else None
        if text and target is not None:
            result = self.messenger.send(target, text)
            self._note = (
                "✓ sent to " + result.recipient
                if result.ok
                else "✗ " + result.detail
            )
        self.render_detail()

    def render_detail(self) -> None:
        """Fill the bottom panel for the selected agent: a pinned header with its name
        and status, then the scrollable body — the exact instructions it was given
        under a collapsible "instructions" row up top, its action graph (each thought,
        spoken turn, and tool call as a step-node) below. The instructions row and each
        step start collapsed; ⏎ (or a click) reveals the selected row's exact detail."""
        head, body = Text(), Text()
        if self.selected:
            self._node_detail(self.selected, head, body)
        self._drillinstr.update(head)
        self._drillbody.update(body)

    def _node_detail(self, sel: str, head: Text, body: Text) -> None:
        n = self.model.nodes.get(sel)
        if not n:
            head.append("(gone)\n", "bright_black")
            return
        if n.kind == "task":
            g, col = task_style(n.status)
        else:
            g = status_glyph(n.status)
            col = status_icon_style(n.status)
        name = sanitize_inline_text(n.label.replace("⚙ ", "") or n.id)
        role = classify_roles(self.model.nodes).get(sel, "")
        name_style = role_style(role) if role else "bold"
        head.append(f"{g} ", col)
        if role:
            head.append(f"{role_icon(role)} ", name_style)
        head.append(name, f"bold {name_style}" if role else name_style)
        meta = f"   {sanitize_inline_text(n.status)}"
        if role:
            meta += f" · {role}"
        if n.model:
            meta += f" · {sanitize_inline_text(n.model)}"
        if n.tokens:
            meta += f" · {n.tokens} tok"
        head.append(meta + "\n", "bright_black")
        if self._note:
            head.append(sanitize_inline_text(self._note) + "\n",
                        "green" if self._note.startswith("✓") else "red")
        elif self._messageable(sel):
            head.append("press i to message this agent\n", "bright_black")
        # The instructions the agent was given lead the scrollable body under their
        # own header — content to read, not an action — with the action graph below.
        instr = self._instructions(n)
        sel_instr = self.zone == "drill" and self.drill_sel == -1
        ins_open = -1 in self.drill_expanded
        pre = 0
        if instr:
            caret = "▾ " if ins_open else "▸ "
            body.append(caret + "instructions\n", "reverse bold" if sel_instr else HEADER)
            rows = 1
            if ins_open:
                for ln in self._wrap_instr(sanitize_text(instr)):
                    body.append(sanitize_text(ln) + "\n", "bright_black")
                    rows += 1
            body.append("\n")
            pre = rows + 1
            if sel_instr:
                self._drill_sel_row, self._drill_sel_end = 0, rows - 1
        rowmap = [(0, -1)] if instr else []
        depth = self._vdepth(sel)
        steps = self._drill_steps()
        if steps:
            cursor = min(self.drill_sel, len(steps) - 1)
            if self.zone != "drill" or cursor < 0:
                cursor = None
            acts: list = []
            sr, se = render_actions(body, steps, cursor, self.drill_expanded,
                                    depth, acts,
                                    width=max(self._drillscroll.size.width - 1, 20))
            rowmap += [(pre + r, i) for r, i in acts]
            if not sel_instr:
                self._drill_sel_row, self._drill_sel_end = sr + pre, se + pre
        elif not sel_instr:
            # No actions yet — let the cursor page through the instructions alone.
            self._drill_sel_row = self._drill_sel_end = max(pre - 1, 0)
        if n.result:
            body.append("\nresult\n", ACTION_DETAIL)
            body.append(sanitize_text(n.result) + "\n", ACTION_DETAIL)
        if not steps and not n.result:
            body.append("(no recorded steps yet)\n", "bright_black")
        self._drill_rowmap = rowmap

    def on_resize(self, event: events.Resize) -> None:
        # Re-fit the tree and drill panes on resize — but the very first Resize can
        # arrive before on_mount has cached the widgets, so skip until they exist.
        if hasattr(self, "_canvas"):
            self.render_canvas()


def main(argv: list[str] | None = None) -> int:
    if (Path(__file__).resolve().parents[2] / "DISABLED").exists():
        print("QuakePro is disabled.", file=sys.stderr)
        return 0
    ap = argparse.ArgumentParser(
        description="Monitor Claude Code, Codex, and pi agent sessions")
    ap.add_argument("--file", help="explicit Claude, Codex, or pi session .jsonl path")
    ap.add_argument("--project", help="filter newest session by project path substring")
    ap.add_argument("--provider", choices=PROVIDERS, default="auto",
                    help="session provider to monitor (default: auto)")
    ap.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="working directory of a run to overview (repeatable)")
    ap.add_argument("--worktrees", action="store_true",
                    help="also overview every git worktree of the current repository")
    args = ap.parse_args(argv)
    if args.root or args.worktrees:
        roots = resolve_roots(args.root, args.worktrees)
        if not roots:
            raise SystemExit("QuakePro: no run roots found.")
        model = RunOverview(roots)
        QuakePro(roots[0], model=model).run()
        return 0
    path = args.file or latest_session(args.project, args.provider)
    if not path or not os.path.exists(path):
        raise SystemExit(
            "QuakePro: no matching Claude Code, Codex, or pi session transcript found.")
    project_root = (
        args.project
        if args.project and os.path.isdir(os.path.expanduser(args.project))
        else None
    )
    try:
        model = open_model(path, args.provider, project_root=project_root)
    except ValueError as exc:
        raise SystemExit(f"QuakePro: {exc}") from exc
    QuakePro(path, model=model).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
