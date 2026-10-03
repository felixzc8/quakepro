"""Capture the real UI with synthetic waves and nesting from prompts/showcase-*.md.

Run from the checkout: uv run --with resvg-py scripts/capture_showcase.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import resvg_py

from quakepro.app import QuakePro
from quakepro.session_model import Node, Step


ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
START = 1_780_000_000.0


class ShowcaseModel:
    provider = "claude"
    path = "synthetic-showcase"
    task_list = {}

    def __init__(self):
        self.nodes = {
            "main": Node(
                "main", "release showcase", kind="main", status="running",
                children=["alpha", "beta", "coordinator"],
                steps=[Step("text", "text: coordinate release checks", ts=START)],
                last_ts=START + 180,
            ),
            "alpha": Node(
                "alpha", "wave alpha", kind="dispatch", status="done",
                parent="main", children=["researcher", "coder", "tester"],
            ),
            "beta": Node(
                "beta", "wave beta", kind="dispatch", status="running",
                parent="main", children=["designer", "reviewer", "debugger"],
            ),
        }
        rows = [
            ("researcher", "alpha", "done", 5, 33, "map the repository", "spec"),
            ("coder", "alpha", "done", 7, 48, "inspect the UI modules", "implement"),
            ("tester", "alpha", "done", 9, 55, "find regression coverage", "implement"),
            ("designer", "beta", "done", 60, 96, "check terminal styles", "spec"),
            ("reviewer", "beta", "running", 62, 180, "review the release diff", "review"),
            ("debugger", "beta", "error", 64, 104, "inspect the expected failure", "review"),
            ("coordinator", "main", "running", 110, 180, "coordinate the release", "orchestrating"),
            ("planner", "coordinator", "running", 119, 178, "plan documentation checks", "spec"),
            ("docs-writer", "planner", "running", 130, 176, "check the user guide", "implement"),
            ("qa-reviewer", "docs-writer", "running", 144, 174, "review setup instructions", "review"),
            ("release-tester", "qa-reviewer", "waiting", 156, 171, "verify release commands", "review"),
        ]
        for index, (name, parent, status, begin, end, goal, phase) in enumerate(rows):
            self.nodes[name] = Node(
                name, name, detail=goal, parent=parent, status=status,
                phase=phase, tokens=420 + index * 137,
                steps=[
                    Step("prompt", "task: " + goal, body=goal, ts=START + begin),
                    Step("tool", "bash: pwd", body='{"command": "pwd"}',
                         output="/demo/quakepro", ts=START + begin + 2),
                    Step("text", "text: " + goal, body=goal, ts=START + end),
                ],
                last_ts=START + end,
            )
            if parent not in {"main", "alpha", "beta"}:
                self.nodes[parent].children.append(name)
                self.nodes[parent].steps.append(Step(
                    "spawn", "spawn: " + name, ts=START + begin,
                    child=name, goal=goal,
                ))
        debugger = self.nodes["debugger"]
        debugger.steps = [
            Step("tool", "bash: inspect error handling", ts=START + 64,
                 body='{"command": "rg -n \'except|raise\' src/quakepro/claude_model.py"}',
                 output="Error handling paths found."),
            Step("tool", "bash: false", ts=START + 104, status="error",
                 body='{"command": "false"}',
                 output="Process exited with code 1.\nExpected failure from the mock workflow; no retry."),
        ]
        debugger.result = "Expected showcase failure: exit code 1"
        self.nodes["reviewer"].skills = ["code-review"]
        self.nodes["docs-writer"].skills = ["docs"]


class StaticObservation:
    def reconcile(self):
        return False

    async def changes(self):
        await asyncio.Event().wait()
        yield frozenset()


def capture(app: QuakePro, name: str) -> None:
    svg = app.export_screenshot(title="QuakePro · synthetic showcase")
    ASSETS.mkdir(parents=True, exist_ok=True)
    fonts = Path("/System/Library/Fonts")
    (ASSETS / f"quakepro-{name}.png").write_bytes(resvg_py.svg_to_bytes(
        svg_string=svg, zoom=1.5, font_family="Menlo",
        monospace_family="Menlo", sans_serif_family="Arial",
        font_dirs=[str(fonts)] if fonts.is_dir() else None,
    ))


async def main():
    app = QuakePro("synthetic-showcase", model=ShowcaseModel(), observation=StaticObservation())
    async with app.run_test(size=(104, 22)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        app.selected = "reviewer"
        await pilot.pause()
        capture(app, "tree")
        await pilot.press("v")
        await pilot.pause()
        capture(app, "timeline")
        await pilot.press("v")
        app.selected = "alpha"
        await pilot.press("left")
        app.selected = "coordinator"
        await pilot.press("left")
        await pilot.resize_terminal(104, 28)
        app.selected = "debugger"
        await pilot.press("enter", "down", "down", "right")
        await pilot.pause()
        capture(app, "inspector")


if __name__ == "__main__":
    asyncio.run(main())
