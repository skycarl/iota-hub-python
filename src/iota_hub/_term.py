"""Terminal styling for human output (conventions section 13).

Styling is a layer over the plain lines, never a different set of facts. A
stream is *styled* only when it is a terminal, ``NO_COLOR`` is unset and the
run is not ``--json``; everything else -- a pipe, a file, an agent, the tests --
gets the plain text. Glyphs beyond ASCII additionally need the stream's
encoding to be UTF-8, so a legacy Windows console with a cp1252 code page still
gets color, with ASCII stand-ins, and nothing it cannot print.
"""

from __future__ import annotations

import os
import sys
from functools import cache

from rich.console import Console, RenderableType
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

#: Text, ``(text, rich style)``, or an already styled :class:`Text`.
Part = str | tuple[str, str] | Text

#: name -> (UTF-8 glyph, ASCII stand-in)
_GLYPHS = {
    "ok": ("✓", "ok"),
    "warn": ("!", "!"),
    "fail": ("✗", "x"),
    "step": ("›", ">"),  # noqa: RUF001 - UTF-8 only
}

#: Set by the CLI under ``--json``: never style, whatever the terminal.
plain: bool = False


def console(err: bool = False) -> Console | None:
    """The rich console for a styled stream, or ``None`` to print plain text."""
    if plain or os.environ.get("NO_COLOR"):
        return None
    stream = sys.stderr if err else sys.stdout
    if not stream.isatty():
        return None
    return _console(err)


@cache
def _console(err: bool) -> Console:
    return Console(stderr=err, highlight=False)


def styled(err: bool = False) -> bool:
    return console(err) is not None


def glyph(name: str, err: bool = False) -> str:
    fancy, ascii_ = _GLYPHS[name]
    stream = console(err)
    if stream is not None and stream.encoding.lower().replace("-", "") == "utf8":
        return fancy
    return ascii_


def text(*parts: Part) -> Text:
    return Text.assemble(*parts)


#: Prose wraps at this width at most; a wide terminal does not make a
#: paragraph easier to read. Commands and URLs are never wrapped by it.
PROSE_WIDTH = 100


def show(
    renderable: RenderableType,
    *,
    err: bool = False,
    indent: int = 0,
    max_width: int | None = None,
    soft_wrap: bool = False,
) -> None:
    """Print a rich renderable on a styled stream (callers check :func:`styled`).

    ``soft_wrap`` prints the text as one unbroken line and leaves wrapping to
    the terminal, so a URL or a command stays one clickable, copyable string.
    """
    stream = console(err)
    assert stream is not None
    if indent and soft_wrap and isinstance(renderable, Text):
        renderable = Text(" " * indent) + renderable
    elif indent:
        renderable = Padding(renderable, (0, 0, 0, indent), expand=False)
    width = min(stream.width, max_width) if max_width else None
    stream.print(renderable, width=width, soft_wrap=soft_wrap)


def pairs(
    rows: list[tuple[str, Text]],
    *,
    err: bool = False,
    indent: int = 0,
    label_style: str = "dim",
) -> None:
    """Label/value rows whose values wrap under themselves, not under the label."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=label_style, no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(label, value)
    show(grid, err=err, indent=indent, max_width=PROSE_WIDTH)
