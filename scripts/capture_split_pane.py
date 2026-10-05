"""Render the current contents of a live, two-pane tmux window.

uv run --with resvg-py scripts/capture_split_pane.py --socket quakepro-readme \
    --target review:0 --output docs/assets/quakepro-codex-tree.png
"""
from __future__ import annotations

import argparse
from io import StringIO
from pathlib import Path
import subprocess

from rich.console import Console
from rich.text import Text

from screenshot_render import write_screenshot


def render(panes: list[str], widths: list[int], output: Path) -> None:
    lines = [pane.splitlines() for pane in panes]
    console = Console(
        width=sum(widths) + 1, height=max(map(len, lines)), record=True, file=StringIO(),
        force_terminal=True, color_system="truecolor",
    )
    for index in range(max(map(len, lines))):
        row = Text()
        for pane, width in zip(lines, widths):
            if row:
                row.append("│", style="grey50")
            cell = Text.from_ansi(pane[index]) if index < len(pane) else Text()
            cell.truncate(width, pad=True)
            row.append_text(cell)
        console.print(row, soft_wrap=True)
    write_screenshot(console.export_svg(title="Codex + QuakePro"), output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    command = ["tmux", "-L", args.socket]
    layout = subprocess.check_output(command + [
        "list-panes", "-t", args.target, "-F",
        "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height}",
    ], text=True)
    panes = sorted((line.split() for line in layout.splitlines()), key=lambda p: int(p[1]))
    if len(panes) != 2 or any(p[2] != "0" for p in panes) or panes[0][4] != panes[1][4]:
        parser.error("choose a window with two side-by-side panes")
    contents = [subprocess.check_output(command + [
        "capture-pane", "-e", "-p", "-t", pane[0],
    ], text=True) for pane in panes]
    render(contents, [int(pane[3]) for pane in panes], args.output)


if __name__ == "__main__":
    main()
