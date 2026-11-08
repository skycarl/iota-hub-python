"""Terminal styling for human output (conventions section 13).

Styling is a layer over the plain lines, never a different set of facts. A
stream is *styled* only when it is a terminal, ``NO_COLOR`` is unset and the
run is not ``--json``; everything else -- a pipe, a file, an agent, the tests --
gets the plain text. Glyphs beyond ASCII additionally need the stream's
encoding to be UTF-8, so a legacy Windows console with a cp1252 code page still
gets color, with ASCII stand-ins, and nothing it cannot print.

Whatever the look, the text shown is :func:`clean`: much of what the CLI prints
is server data that other users wrote (an observer's name, an asteroid's, a
finding's message), and a terminal *executes* the control characters in it --
an ESC sequence rewrites the line, sets the window title or the clipboard. The
plain lines and every styled renderable pass through the one sanitizer at the
point they are written, so no caller has to remember to.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from functools import cache

from rich.console import Console, ConsoleOptions, RenderableType
from rich.padding import Padding
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

# --------------------------------------------------------------------------
# Sanitizing
# --------------------------------------------------------------------------

#: ECMA-48 escape sequences, 7-bit (ESC-introduced) and 8-bit (C1) forms:
#: a CSI with its parameter, intermediate and final bytes; a string command
#: (OSC, DCS, SOS, PM, APC) up to its BEL or ST terminator; any other escape
#: with its intermediate and final bytes. An unterminated string command does
#: not match the middle alternative: the last one then drops its introducer,
#: and the body is left as the visible text it would otherwise have swallowed.
_ESCAPE_SEQUENCES = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|(?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|\x1b[ -/]*[0-~]?"
)

#: Unicode categories a terminal must not be handed: controls (C0, DEL, C1),
#: format characters (bidirectional overrides, zero-width joiners, the BOM),
#: lone surrogates, and the line and paragraph separators.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

#: The two controls that are layout, not commands, and stay.
_KEPT = "\n\t"

#: An ASCII string with nothing to do: the common case, answered without
#: looking at every character's category.
_ASCII_NEEDS_WORK = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    """``text`` with everything a terminal would act on removed.

    Escape sequences go first, whole, so ``ESC ] 0 ; title BEL`` does not
    leave ``0;title`` behind; then every remaining control and format
    character, apart from a newline and a tab. Printable text in any script
    is untouched: an observer's name is shown as written.
    """
    if text.isascii() and not _ASCII_NEEDS_WORK.search(text):
        return text
    text = _ESCAPE_SEQUENCES.sub("", text)
    return "".join(
        character
        for character in text
        if character in _KEPT
        or unicodedata.category(character) not in _DROPPED_CATEGORIES
    )


class _Cleaned:
    """A renderable whose every text segment has been through :func:`clean`.

    Applied as the renderable is written, after rich has laid it out, so one
    wrapper covers a :class:`Text`, a table, a padded block -- whatever the
    callers build. Rich's own control segments (its cursor movements) are its,
    and pass.
    """

    def __init__(self, inner: RenderableType) -> None:
        self._inner = inner

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> Iterable[Segment]:
        for segment in console.render(self._inner, options):
            if segment.control is None:
                yield Segment(clean(segment.text), segment.style)
            else:
                yield segment


# --------------------------------------------------------------------------
# Styling
# --------------------------------------------------------------------------

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
    Whatever is printed is :func:`clean`: this is the styled stream's one exit.
    """
    stream = console(err)
    assert stream is not None
    if indent and soft_wrap and isinstance(renderable, Text):
        renderable = Text(" " * indent) + renderable
    elif indent:
        renderable = Padding(renderable, (0, 0, 0, indent), expand=False)
    width = min(stream.width, max_width) if max_width else None
    stream.print(_Cleaned(renderable), width=width, soft_wrap=soft_wrap)


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
