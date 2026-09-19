"""``--fix``: write corrected metadata to a *new* file.

Three promises, and the implementation is shaped by them:

1. **The input is never modified.** The fixer refuses to write over its own
   source, and returns new text rather than mutating anything.
2. **Keys and field order are preserved.** Rather than re-emitting entries from
   the parsed model -- which would reformat the whole file -- corrections are
   applied as *surgical replacements* on the original text. Every byte the
   fixer does not deliberately change comes through untouched: comments,
   indentation, alignment, delimiter style, trailing commas, `@preamble`.
3. **Anything uncertain is left alone.** Only objective fields are corrected,
   and only for entries resolved by DOI, where the record is definitely the
   right work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Sequence

from .models import Code, EntryReport, Report, Severity, Status
from .parsing import ParsedBib

__all__ = ["Fix", "plan_fixes", "apply_fixes", "FIXABLE_FIELDS", "DOI_ADD_THRESHOLD"]

#: Fields safe to correct automatically. These are objective and low-risk: a
#: year or a page range is either right or wrong.
#:
#: Title, author and venue are deliberately absent. A flagged title or author
#: mismatch means the entry may be the *wrong work entirely*, which is a
#: judgement call for the author; and rewriting an abbreviated venue would
#: undo a deliberate house style. Those are reported, never rewritten.
FIXABLE_FIELDS: Final[dict[Code, str]] = {
    Code.YEAR_MISMATCH: "year",
    Code.VOLUME_MISMATCH: "volume",
    Code.PAGES_MISMATCH: "pages",
}

#: A DOI is only *added* to an entry that has none when the title match is
#: near-certain. Inserting an identifier on a shaky match would be worse than
#: leaving the entry alone.
DOI_ADD_THRESHOLD: Final[float] = 0.95

#: Purely local corrections, applied to any entry regardless of whether it
#: resolved. These change *presentation* and never meaning -- brace protection
#: adds braces around text already there, and a backwards page range has one
#: correct reading -- so none of them prejudges a decision the author still has
#: to make about a CRITICAL entry.
LOCAL_FIXES: Final[dict[Code, str]] = {
    Code.CASE_PROTECTION: "title",
    Code.SUSPECT_PAGES: "pages",
}


@dataclass(frozen=True, slots=True)
class Fix:
    """One correction to apply."""

    key: str
    field: str
    old: str | None
    new: str
    added: bool = False

    def describe(self) -> str:
        if self.added:
            return f"{self.key}: added {self.field} = {self.new}"
        return f"{self.key}: {self.field} {self.old} → {self.new}"


def plan_fixes(report: Report) -> list[Fix]:
    """Decide which corrections are safe to make.

    An entry is skipped entirely if anything about it is CRITICAL -- a
    retracted paper or an unresolvable DOI needs a human decision about whether
    to cite it at all, and silently correcting its page numbers would be absurd.
    """
    fixes: list[Fix] = []
    for entry in report.entries:
        fixes.extend(_fixes_for(entry))
    return fixes


def _fixes_for(entry: EntryReport) -> list[Fix]:
    """Plan this entry's corrections, at most one per field.

    Two rules can legitimately target the same field -- a backwards page range
    is both locally wrong and different from the record. The registry wins,
    because it knows the true value rather than merely a self-consistent one.
    """
    by_field: dict[str, Fix] = {}

    # Local formatting fixes first: they need no lookup and apply even to an
    # entry the author may end up deleting.
    for problem in entry.problems:
        field = LOCAL_FIXES.get(problem.code)
        if (
            field is not None
            and problem.severity is not Severity.INFO
            and problem.authoritative
        ):
            by_field[field] = Fix(
                key=entry.key,
                field=field,
                old=problem.local,
                new=_normalise(field, problem.authoritative),
            )

    record = entry.record
    if record is None or not entry.resolved:
        return list(by_field.values())
    if entry.status is Status.CRITICAL:
        return list(by_field.values())
    if any(problem.severity is Severity.CRITICAL for problem in entry.problems):
        return list(by_field.values())

    if record.match_confidence >= 1.0:
        for problem in entry.problems:
            field = FIXABLE_FIELDS.get(problem.code)
            if field is None or problem.authoritative is None:
                continue
            # Overwrites any local fix for the same field, by design.
            by_field[field] = Fix(
                key=entry.key,
                field=field,
                old=problem.local,
                new=_normalise(field, problem.authoritative),
            )

    # Adding a DOI the entry lacks is the one insertion worth making, and only
    # when the title match leaves essentially no doubt.
    local = entry.metadata
    if (
        local is not None
        and not local.doi
        and record.metadata.doi
        and record.match_confidence >= DOI_ADD_THRESHOLD
    ):
        by_field["doi"] = Fix(
            key=entry.key, field="doi", old=None, new=record.metadata.doi, added=True
        )
    return list(by_field.values())


def _normalise(field: str, value: str) -> str:
    """Write a value the way BibTeX expects it.

    Page ranges are the only case that matters: a report shows ``770-778``
    because that reads naturally, but BibTeX wants an en-dash written ``--``,
    and writing a single hyphen into the file would be a silent downgrade.
    """
    if field == "pages":
        return re.sub(r"\s*-{1,3}\s*", "--", value.strip())
    return value


def apply_fixes(source: str, parsed: ParsedBib, fixes: Sequence[Fix]) -> tuple[str, list[Fix]]:
    """Apply corrections to the source text.

    Returns the new text and the fixes that were actually applied -- a fix
    whose field could not be located is reported as skipped rather than
    silently dropped or forced in.
    """
    if not fixes:
        return source, []

    spans = _entry_spans(source, parsed)
    by_key: dict[str, list[Fix]] = {}
    for fix in fixes:
        by_key.setdefault(fix.key, []).append(fix)

    applied: list[Fix] = []
    # Work back to front so earlier offsets stay valid as the text changes.
    pieces = source
    for key in sorted(by_key, key=lambda k: spans.get(k, (0, 0))[0], reverse=True):
        span = spans.get(key)
        if span is None:
            continue
        start, end = span
        segment = pieces[start:end]
        for fix in by_key[key]:
            updated = _apply_one(segment, fix)
            if updated is not None:
                segment = updated
                applied.append(fix)
        pieces = pieces[:start] + segment + pieces[end:]

    return pieces, applied


def _entry_spans(source: str, parsed: ParsedBib) -> dict[str, tuple[int, int]]:
    """Character span of each entry in the source text.

    Derived from the one-based start lines the parser recorded, with each entry
    running up to the line before the next one begins.
    """
    line_offsets = [0]
    for line in source.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))

    starts: list[tuple[str, int]] = []
    for entry in parsed.entries:
        if entry.start_line is None:
            continue
        index = min(entry.start_line - 1, len(line_offsets) - 1)
        starts.append((entry.key, line_offsets[index]))
    starts.sort(key=lambda item: item[1])

    spans: dict[str, tuple[int, int]] = {}
    for position, (key, start) in enumerate(starts):
        end = starts[position + 1][1] if position + 1 < len(starts) else len(source)
        spans[key] = (start, end)
    return spans


def _apply_one(segment: str, fix: Fix) -> str | None:
    """Replace or insert one field inside a single entry's text."""
    match = re.search(
        r"(?im)^([ \t]*)(" + re.escape(fix.field) + r")([ \t]*=[ \t]*)", segment
    )
    if match is None:
        return _insert_field(segment, fix) if fix.added else None

    value_start = match.end()
    value_end, delimiter = _read_value(segment, value_start)
    if value_end is None:
        return None

    replacement = _wrap(fix.new, delimiter)
    return segment[:value_start] + replacement + segment[value_end:]


def _read_value(text: str, start: int) -> tuple[int | None, str]:
    """Find where a field's value ends, and which delimiter style it uses.

    Braces nest, so they are counted rather than searched for -- a title like
    ``{A {Study}}`` must not terminate at the first closing brace.
    """
    if start >= len(text):
        return None, "{"

    if text[start] == "{":
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index + 1, "{"
        return None, "{"

    if text[start] == '"':
        index = start + 1
        while index < len(text):
            if text[index] == '"' and text[index - 1] != "\\":
                return index + 1, '"'
            index += 1
        return None, '"'

    # A bare value: a number, or a string macro. Runs to the comma or brace.
    match = re.compile(r"[^,\n}]*").match(text, start)
    if match is None:
        return None, ""
    return match.end(), ""


def _wrap(value: str, delimiter: str) -> str:
    """Re-wrap a value in the delimiter style the entry already used."""
    if delimiter == "{":
        return "{" + value + "}"
    if delimiter == '"':
        return '"' + value + '"'
    return value


def _insert_field(segment: str, fix: Fix) -> str | None:
    """Insert a field the entry does not have, matching its local style.

    The indentation and the position of ``=`` are copied from an existing
    field, so an inserted line looks like it was always there.
    """
    sample = re.search(r"(?im)^([ \t]*)([A-Za-z][\w-]*)([ \t]*)=([ \t]*)", segment)
    if sample is None:
        return None
    indent, name, before_eq, after_eq = sample.groups()

    # Line the new ``=`` up with the existing one where the file aligns them.
    column = len(name) + len(before_eq)
    pad = " " * max(1, column - len(fix.field))
    line = f"{indent}{fix.field}{pad}={after_eq}{{{fix.new}}}"

    closing = segment.rfind("}")
    if closing == -1:
        return None

    head = segment[:closing].rstrip()
    tail = segment[closing:]
    separator = "" if head.endswith(",") else ","
    return f"{head}{separator}\n{line}\n{tail.lstrip()}" if tail.startswith("}") else None


def write_fixed(
    source_path: Path, output_path: Path, text: str, *, force: bool = False
) -> None:
    """Write the corrected file, refusing to clobber the input.

    ``--fix`` exists to produce a file the author can diff against the original.
    Writing over the original would destroy exactly that.
    """
    source = source_path.resolve()
    output = output_path.resolve()
    if source == output:
        raise ValueError(
            "--fix must write to a different file than the input; "
            "bibcheck never edits a bibliography in place"
        )
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists (pass --force to overwrite)")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
