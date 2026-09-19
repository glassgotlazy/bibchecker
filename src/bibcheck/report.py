"""Rendering a :class:`Report` for humans and for machines.

The layout rule, which the web page follows too: **colour carries meaning and
nothing else does.** Status is the only thing that is ever coloured, so a glance
down the left edge tells you what needs attention. No boxes, no nesting, no
progress chrome left on screen after the run.

Colour is dropped automatically when stdout is not a terminal, so piping to a
file or a CI log yields clean text without anyone passing a flag.
"""

from __future__ import annotations

import json
import os
from typing import Final, Iterable, TextIO

from rich.console import Console
from rich.text import Text

from .models import Code, Problem, Report, Severity, Status
from .stats import OLD_REFERENCE_YEARS, Stats

__all__ = [
    "render_human",
    "render_json",
    "render_stats",
    "make_console",
    "STATUS_GLYPH",
    "STATUS_STYLE",
]

#: One character per status. Deliberately ASCII-adjacent so they survive a
#: terminal without good Unicode coverage and a CI log viewer.
STATUS_GLYPH: Final[dict[Status, str]] = {
    Status.OK: "✓",
    Status.WARN: "⚠",
    Status.CRITICAL: "✕",
    Status.UNRESOLVED: "?",
    Status.NETWORK_ERROR: "~",
}

STATUS_STYLE: Final[dict[Status, str]] = {
    Status.OK: "green",
    Status.WARN: "yellow",
    Status.CRITICAL: "red",
    Status.UNRESOLVED: "blue",
    Status.NETWORK_ERROR: "dim",
}

#: Order the summary tally is printed in: most serious first.
_TALLY_ORDER: Final[tuple[Status, ...]] = (
    Status.CRITICAL,
    Status.WARN,
    Status.UNRESOLVED,
    Status.NETWORK_ERROR,
    Status.OK,
)

_LABEL: Final[dict[Status, str]] = {
    Status.OK: "ok",
    Status.WARN: "warn",
    Status.CRITICAL: "critical",
    Status.UNRESOLVED: "unresolved",
    Status.NETWORK_ERROR: "error",
}


def make_console(
    *, file: TextIO | None = None, color: bool | None = None, width: int | None = None
) -> Console:
    """Build a console.

    ``color=None`` means "decide from the environment", which is what almost
    every caller wants: rich then honours ``NO_COLOR``, ``TERM=dumb`` and
    whether the stream is a TTY.
    """
    if color is None:
        return Console(file=file, width=width, highlight=False, soft_wrap=False)
    return Console(
        file=file,
        width=width,
        highlight=False,
        soft_wrap=False,
        force_terminal=color or None,
        no_color=not color,
    )


#: Findings where the second value genuinely comes from the registry. Only
#: these may be labelled "crossref" -- a local suggestion shown under that
#: label would be a straight-up lie about where it came from.
_DRIFT_CODES: Final[frozenset[Code]] = frozenset(
    {
        Code.TITLE_MISMATCH,
        Code.AUTHOR_MISMATCH,
        Code.YEAR_MISMATCH,
        Code.VENUE_MISMATCH,
        Code.VOLUME_MISMATCH,
        Code.PAGES_MISMATCH,
    }
)


def _problem_text(problem: Problem) -> Text:
    """One problem, rendered as a single line.

    A drift finding shows both values side by side -- the whole point is that
    the author can see what differs without opening a browser. A *local*
    finding shows its suggestion under "fix", because that value is bibcheck's
    own proposal rather than something a registry said.
    """
    text = Text()
    if (
        problem.code in _DRIFT_CODES
        and problem.field
        and problem.local is not None
        and problem.authoritative is not None
    ):
        text.append(f"{problem.field} ", style="default")
        text.append("bib ", style="dim")
        text.append(problem.local)
        text.append("  crossref ", style="dim")
        text.append(problem.authoritative)
        return text

    text.append(problem.message)
    if problem.authoritative and problem.code not in _DRIFT_CODES:
        text.append("  fix ", style="dim")
        text.append(problem.authoritative, style="dim")
    elif problem.local:
        text.append(f"  {problem.local}", style="dim")
    if problem.confidence is not None:
        text.append(f"  (confidence {problem.confidence:.2f})", style="dim")
    return text


def _display_path(path: str) -> str:
    """Show the shortest unambiguous spelling of the path.

    An absolute path from a temp dir or a deep tree wraps across two lines and
    tells the reader nothing they did not already know.
    """
    try:
        relative = os.path.relpath(path)
    except (ValueError, OSError):
        return path
    return relative if len(relative) < len(path) else path


def render_human(
    report: Report,
    console: Console,
    *,
    elapsed: float | None = None,
    show_ok: bool = True,
) -> None:
    """Print the report.

    Laid out by hand rather than with a table: a table pads every cell and
    right-fills every row, which leaves trailing whitespace all over a CI log
    and puts stray gaps between the glyph and the key.
    """
    console.print()
    console.print(_header(report, elapsed))
    console.print()

    visible = [
        entry
        for entry in report.entries
        if show_ok or entry.status is not Status.OK
    ]
    key_width = min(max((len(entry.key) for entry in visible), default=0), 32)

    for entry in visible:
        status = entry.status
        line = Text("  ")
        line.append(STATUS_GLYPH[status], style=STATUS_STYLE[status])
        line.append("  ")
        line.append(entry.key.ljust(key_width))
        problems = entry.problems
        if problems:
            line.append("  ")
            line.append_text(_problem_text(problems[0]))
        console.print(line)

        # Continuation lines sit under the problems column, not the key.
        indent = " " * (5 + key_width + 2)
        for problem in problems[1:]:
            console.print(Text(indent) + _problem_text(problem))

    if not visible:
        console.print(Text("  every entry checked out", style=STATUS_STYLE[Status.OK]))

    for problem in report.parse_problems:
        console.print(
            Text("  ! ", style=STATUS_STYLE[Status.WARN]) + Text(problem.message, style="dim")
        )

    _render_crosscheck(report, console)
    console.print()
    console.print(Text("  ") + _tally(report))
    console.print()


def _header(report: Report, elapsed: float | None) -> Text:
    text = Text("  bibcheck  ", style="dim")
    text.append(_display_path(report.bib_path), style="bold")
    parts = [f"{len(report.entries)} entries"]
    parts.append("cache only" if report.offline else "crossref")
    if elapsed is not None:
        parts.append(f"{elapsed:.1f}s")
    text.append("  ·  " + " · ".join(parts), style="dim")
    return text


def _render_crosscheck(report: Report, console: Console) -> None:
    """Report undefined keys.

    Uncited entries are *not* repeated here: each already carries its own
    finding on its own line, and listing them twice is noise.
    """
    crosscheck = report.crosscheck
    if crosscheck is None or not crosscheck.undefined:
        return
    console.print()
    console.print(
        Text("  ", style="default")
        + Text(STATUS_GLYPH[Status.CRITICAL], style=STATUS_STYLE[Status.CRITICAL])
        + Text(f"  {len(crosscheck.undefined)} cited but missing from the .bib: ")
        + Text(", ".join(crosscheck.undefined), style="dim")
    )


def _tally(report: Report) -> Text:
    """The summary line: most serious first, so the eye lands on what matters."""
    counts = report.counts
    text = Text()
    written = False
    for status in _TALLY_ORDER:
        count = counts[status]
        if not count:
            continue
        if written:
            text.append("   ")
        text.append(f"{count} {_LABEL[status]}", style=STATUS_STYLE[status])
        written = True
    if not written:
        text.append("nothing to report", style="dim")
    return text


def render_stats(stats: Stats, console: Console) -> None:
    """Print bibliography health metrics.

    Deliberately plain: these are observations, not findings, and colouring
    them would compete with the status colours that do mean something.
    """
    if not stats.total:
        return

    console.print(Text("  bibliography", style="dim"))
    console.print()

    rows: list[tuple[str, str]] = [
        ("references", str(stats.total)),
        (
            "with a DOI",
            f"{stats.with_doi}  ({stats.share(stats.with_doi):.0%})",
        ),
    ]
    if stats.median_year is not None:
        rows.append(
            (
                "years",
                f"{stats.oldest}–{stats.newest}   median {stats.median_year}",
            )
        )
        rows.append(
            (
                f"older than {OLD_REFERENCE_YEARS}y",
                f"{stats.old_references}  ({stats.share(stats.old_references):.0%})",
            )
        )
    if stats.preprints:
        rows.append(
            ("preprints", f"{stats.preprints}  ({stats.share(stats.preprints):.0%})")
        )
    if stats.self_citations:
        rows.append(
            (
                "self-citations",
                f"{stats.self_citations}  ({stats.share(stats.self_citations):.0%})",
            )
        )

    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        console.print(Text("  ") + Text(label.rjust(width), style="dim") + Text("  " + value))

    if stats.venues:
        console.print()
        console.print(Text("  " + "top venues".rjust(width), style="dim"))
        for name, count in stats.venues[:3]:
            trimmed = name if len(name) <= 52 else name[:49] + "…"
            console.print(
                Text("  " + " " * width + "  ")
                + Text(f"{count:>3}  ", style="dim")
                + Text(trimmed)
            )

    for note in stats.notes:
        console.print()
        console.print(Text("  · ", style="dim") + Text(note, style="dim"))
    console.print()


def render_json(report: Report, *, indent: int | None = 2) -> str:
    """Serialise the report. Stable enough to diff between runs."""
    return json.dumps(report.to_json(), indent=indent, ensure_ascii=False, sort_keys=False)
