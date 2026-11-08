"""The display sanitizer: server strings reach the terminal as text, never as
control sequences (conventions section 13)."""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.text import Text

from iota_hub import _term
from iota_hub._term import clean


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # CSI: erase the line, move the cursor, colour.
        ("a\x1b[2Kb", "ab"),
        ("a\x1b[1;1Hb", "ab"),
        ("a\x1b[31;1mb\x1b[0m", "ab"),
        # OSC: window title (BEL), clipboard (ST).
        ("a\x1b]0;pwned\x07b", "ab"),
        ("a\x1b]52;c;cHduZWQ=\x1b\\b", "ab"),
        # DCS and APC strings.
        ("a\x1bPq#0;2;0;0;0#0~~\x1b\\b", "ab"),
        ("a\x1b_payload\x1b\\b", "ab"),
        # An unterminated string command loses its introducer; what it would
        # have swallowed stays visible as text.
        ("a\x1b]0;pwned", "a0;pwned"),
        # Two-character escapes: RIS (full reset), and a lone trailing ESC.
        ("a\x1bcb", "ab"),
        ("a\x1b", "a"),
        # 8-bit C1 forms: CSI, OSC with ST, NEL on its own.
        ("a\x9b2Kb", "ab"),
        ("a\x9d0;pwned\x9cb", "ab"),
        ("a\x85b", "ab"),
        # The other C0 controls and DEL, carriage return included.
        ("a\x00b\x07c\x08d\x7fe\rf", "abcdef"),
        # Format characters: bidirectional overrides, zero-width, BOM.
        ("a\u202eb\u2066c\u2069d", "abcd"),
        ("a\u200bb\u200dc\ufeffd", "abcd"),
        # Line and paragraph separators.
        ("a\u2028b\u2029c", "abc"),
    ],
)
def test_clean_removes_what_a_terminal_would_act_on(raw: str, expected: str) -> None:
    assert clean(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "plain ASCII text",
        "two\nlines\twith a tab",
        "Zoë Čapek (33777) 1999 RM158 — café ✓ › ✗",  # noqa: RUF001
        "UCAC4 326-152286 · 2026-09-26T04:12:00Z",
        "名前 Имя اسم",
    ],
)
def test_clean_leaves_text_alone(text: str) -> None:
    assert clean(text) == text


def test_show_cleans_whatever_rich_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    """The styled stream's one exit cleans a Text, a grid and a padded block."""
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, width=80)
    monkeypatch.setattr(_term, "console", lambda err=False: console)

    _term.show(Text("name\x1b]0;pwned\x1b\\ here", "bold"), indent=2)
    _term.show(_term.text(("label ", "dim"), "v\x1b[2Kalue"), soft_wrap=True)
    _term.pairs([("Fix", Text("re-export\x9b2K the curve"))], indent=4)
    # Rich strips a BEL itself when the Text is built, so this OSC reaches the
    # sanitizer unterminated: the introducer goes, the body stays as text.
    _term.show(Text("title\x1b]0;x\x07 set"))

    out = stream.getvalue()
    assert "\x1b" not in out and "\x9b" not in out and "\x07" not in out
    assert "pwned" not in out
    assert "name here" in out
    assert "value" in out
    assert "re-export the curve" in out
    assert "title0;x set" in out
