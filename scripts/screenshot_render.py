"""Rasterize terminal SVGs with consistent monospace glyph rendering."""
from pathlib import Path
import copy
import xml.etree.ElementTree as ET

import resvg_py
from rich.cells import get_character_cell_size


def write_screenshot(svg: str, path: Path) -> None:
    root = ET.fromstring(svg)
    # Isolate glyph fallback so a missing symbol cannot change a whole text span's font.
    for group in root.iter():
        for element in list(group):
            if not element.tag.endswith("}text"):
                continue
            element.set("style", "font-family: Menlo, monospace")
            if "textLength" not in element.attrib or not element.text:
                continue
            cells = sum(max(0, get_character_cell_size(char)) for char in element.text)
            if not cells:
                continue
            width = float(element.attrib["textLength"]) / cells
            offset = float(element.attrib["x"])
            index = list(group).index(element)
            group.remove(element)
            for char in element.text:
                glyph = copy.deepcopy(element)
                glyph.text = char
                glyph.set("x", str(offset))
                glyph.attrib.pop("textLength")
                group.insert(index, glyph)
                index += 1
                offset += max(0, get_character_cell_size(char)) * width
    svg = ET.tostring(root, encoding="unicode")
    path.parent.mkdir(parents=True, exist_ok=True)
    fonts = Path("/System/Library/Fonts")
    path.write_bytes(resvg_py.svg_to_bytes(
        svg_string=svg, zoom=1.5, font_family="Menlo",
        monospace_family="Menlo", sans_serif_family="Menlo", serif_family="Menlo",
        font_dirs=[str(fonts)] if fonts.is_dir() else None,
    ))

