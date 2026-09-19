"""Pull a reference list straight out of a paper PDF.

Often there is no ``.bib`` to hand -- a co-author sent a PDF, or the submission
is the only artefact left. This module recovers the bibliography from the
document itself and hands it to exactly the same checks a ``.bib`` gets, by
building synthetic :class:`BibEntry` objects. Nothing downstream needs to know
where the references came from.

Extraction from PDF is *inherently lossy*: text is stored as positioned glyphs,
not sentences, so column order, line breaks and hyphenation all have to be
undone by guesswork. The module is built around that rather than pretending
otherwise:

* Every reference keeps its **raw extracted text**, so the report can show what
  was actually read off the page.
* A reference that cannot be parsed is **reported, never dropped** -- a missing
  reference would be a silent lie about how many were checked.
* Each parse carries a confidence, so a reference assembled from guesswork is
  visibly weaker than one carrying a DOI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Sequence

from .identifiers import normalize_arxiv_id, normalize_doi
from .models import BibEntry, Code, CrossCheck, Problem, Severity
from .parsing import ParsedBib

__all__ = [
    "extract_text",
    "find_reference_section",
    "split_references",
    "parse_reference",
    "references_to_bib",
    "cited_markers",
    "crosscheck_pdf",
    "PdfExtractionError",
    "ParsedReference",
]


class PdfExtractionError(RuntimeError):
    """The file could not be read as a PDF at all."""


#: Headings that begin a reference list. Matched case-insensitively on a line
#: of its own, optionally numbered ("V. REFERENCES", "6 Bibliography").
_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:(?:[IVXLC]+|\d+)[.)]?[ \t]+)?"
    r"(references?|bibliography|works\s+cited|literature\s+cited)"
    r"[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Headings that end one. An appendix or a biography follows the references in
#: most templates, and sweeping them in produces garbage entries.
_TERMINATOR_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:(?:[IVXLC]+|\d+)[.)]?[ \t]+)?"
    r"(appendix|appendices|acknowledg(?:e)?ments?|author\s+biograph|"
    r"biograph(?:y|ies)|supplementary|supporting\s+information|about\s+the\s+authors)"
    r"\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

#: ``[12]`` at the start of a reference.
_BRACKET_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"(?m)^[ \t]*\[(\d{1,3})\][ \t]*")
#: ``12.`` or ``12)`` at the start of a reference.
_NUMBER_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"(?m)^[ \t]*(\d{1,3})[.)][ \t]+")
#: ``[1]``, ``[1], [2]`` and ``[1]-[3]`` in the body text.
_CITATION_RE: Final[re.Pattern[str]] = re.compile(r"\[(\d{1,3}(?:\s*[-–,]\s*\d{1,3})*)\]")

_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_VOLUME_RE: Final[re.Pattern[str]] = re.compile(r"\bvol(?:ume)?\.?\s*(\d+)", re.IGNORECASE)
_ISSUE_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:no|iss(?:ue)?)\.?\s*(\d+)", re.IGNORECASE)
_PAGES_RE: Final[re.Pattern[str]] = re.compile(
    r"\bp{1,2}\.?\s*(\d+)\s*[-–—]{1,2}\s*(\d+)", re.IGNORECASE
)
_DOI_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:doi:?\s*|https?://(?:dx\.)?doi\.org/)\s*(10\.\d{4,9}(?:\.\d+)*/[^\s,;]+)",
    re.IGNORECASE,
)
_BARE_DOI_RE: Final[re.Pattern[str]] = re.compile(r"\b(10\.\d{4,9}(?:\.\d+)*/[^\s,;]+)")
_ARXIV_RE: Final[re.Pattern[str]] = re.compile(
    r"arxiv[:\s]*((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)", re.IGNORECASE
)
#: An IEEE-style quoted title: ``"Title," in ...`` or ``"Title."``.
_QUOTED_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"[\"“]([^\"”]{3,400})[\"”]")

#: Cue words that mark where a venue begins, used when there is no quoted title.
_VENUE_CUE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:in\s+Proc|Proc\.|Trans\.|Conf\.|J\.|"
    r"Proceedings\b|in\s+Advances\b|Journal\b|Transactions\b|"
    r"Conference\b|IEEE\b|ACM\b|arXiv\b)"
)


@dataclass(frozen=True, slots=True)
class ParsedReference:
    """One reference recovered from a PDF."""

    marker: str
    raw: str
    title: str | None = None
    authors: str | None = None
    year: str | None = None
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    confidence: float = 0.0

    @property
    def key(self) -> str:
        """A citation key that maps back to the PDF.

        ``ref4`` is deliberately the reference's own number: the reader can
        find ``[4]`` on the page immediately, which a generated
        ``wakefield1998`` would not give them.
        """
        return f"ref{self.marker}" if self.marker.isdigit() else f"ref_{self.marker}"

    @property
    def usable(self) -> bool:
        """Is there enough here to look the work up at all?"""
        return bool(self.doi or self.arxiv_id or self.title)


def extract_text(source: Path | bytes) -> str:
    """Read every page of a PDF as text, in reading order.

    Raises :class:`PdfExtractionError` when the file is not a PDF or is
    encrypted; a page that fails individually is skipped, since one bad page
    should not cost the whole bibliography.
    """
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - depends on the install
        raise PdfExtractionError(
            "reading PDFs needs pypdf; install bibcheck with: pip install 'bibcheck[pdf]'"
        ) from error

    import io

    try:
        handle = io.BytesIO(source) if isinstance(source, bytes) else source
        reader = PdfReader(handle)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as error:
                raise PdfExtractionError("the PDF is encrypted") from error
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")  # One unreadable page, not a failed run.
        text = "\n".join(pages)
    except PdfExtractionError:
        raise
    except Exception as error:
        raise PdfExtractionError(f"could not read the PDF: {error}") from error

    if not text.strip():
        raise PdfExtractionError(
            "no text could be extracted; the PDF may be a scan, which needs OCR first"
        )
    return text


def find_reference_section(text: str) -> tuple[str, str]:
    """Split the document into (body, references).

    The *last* references heading wins: papers cite the word "references" in
    prose, and a per-section bibliography in a multi-part document should not
    shadow the real one at the end.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return text, ""

    heading = matches[-1]
    body = text[: heading.start()]
    tail = text[heading.end() :]

    terminator = _TERMINATOR_RE.search(tail)
    if terminator is not None and terminator.start() > 200:
        tail = tail[: terminator.start()]
    return body, tail.strip()


def _join_wrapped_lines(block: str) -> str:
    """Undo the line breaks a PDF's fixed column width introduced.

    A word split across lines with a hyphen is rejoined; every other break
    becomes a space. Done *after* splitting into references so a break never
    merges two entries.
    """
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", block)
    return re.sub(r"\s+", " ", text).strip()


def split_references(block: str) -> list[tuple[str, str]]:
    """Split a reference block into ``(marker, raw_text)`` pairs.

    Three strategies, tried in order of how reliable they are: bracketed
    numbers, plain numbers, then blank-line separation for author-year styles.
    """
    if not block.strip():
        return []

    for pattern in (_BRACKET_MARKER_RE, _NUMBER_MARKER_RE):
        found = _split_on_markers(block, pattern)
        if len(found) >= 2:
            return found

    # Author-year styles have no marker at all; fall back to blank lines, then
    # to one-reference-per-line.
    chunks = [chunk for chunk in re.split(r"\n\s*\n", block) if chunk.strip()]
    if len(chunks) < 2:
        chunks = [line for line in block.splitlines() if len(line.strip()) > 40]
    return [(str(index), _join_wrapped_lines(chunk)) for index, chunk in enumerate(chunks, 1)]


def _split_on_markers(
    block: str, pattern: re.Pattern[str]
) -> list[tuple[str, str]]:
    matches = list(pattern.finditer(block))
    if not matches:
        return []

    # Markers must run 1, 2, 3...: a stray "[15]" inside a reference's own text
    # would otherwise slice it in half.
    expected = 1
    kept: list[re.Match[str]] = []
    for match in matches:
        if int(match.group(1)) == expected:
            kept.append(match)
            expected += 1
    if len(kept) < 2:
        return []

    found: list[tuple[str, str]] = []
    for position, match in enumerate(kept):
        end = kept[position + 1].start() if position + 1 < len(kept) else len(block)
        raw = _join_wrapped_lines(block[match.end() : end])
        if raw:
            found.append((match.group(1), raw))
    return found


def parse_reference(marker: str, raw: str) -> ParsedReference:
    """Pull structured fields out of one reference string.

    Best-effort and openly heuristic. A DOI or arXiv id found here makes
    everything else redundant -- the registry lookup supplies authoritative
    metadata -- so those are searched for first and weigh most in the
    confidence score.
    """
    text = _join_wrapped_lines(raw)

    doi = None
    match = _DOI_RE.search(text) or _BARE_DOI_RE.search(text)
    if match:
        doi = normalize_doi(match.group(1).rstrip(".,;)"))

    arxiv_id = None
    arxiv_match = _ARXIV_RE.search(text)
    if arxiv_match:
        arxiv_id = normalize_arxiv_id(arxiv_match.group(1))

    title, authors, remainder = _split_title(text)
    years = _YEAR_RE.findall(text)
    volume = _VOLUME_RE.search(text)
    issue = _ISSUE_RE.search(text)
    pages = _PAGES_RE.search(text)

    reference = ParsedReference(
        marker=marker,
        raw=text,
        title=title,
        authors=authors,
        year=years[-1] if years else None,
        venue=_guess_venue(remainder),
        volume=volume.group(1) if volume else None,
        issue=issue.group(1) if issue else None,
        pages=f"{pages.group(1)}--{pages.group(2)}" if pages else None,
        doi=doi,
        arxiv_id=arxiv_id,
    )
    return _with_confidence(reference)


def _split_title(text: str) -> tuple[str | None, str | None, str]:
    """Separate authors, title and the rest.

    IEEE and most numbered styles quote the title, which is by far the most
    reliable signal available. Without quotes, the title is taken as the span
    between the author list and the first venue cue.
    """
    quoted = _QUOTED_TITLE_RE.search(text)
    if quoted:
        title = quoted.group(1).strip().rstrip(",.;")
        authors = text[: quoted.start()].strip().rstrip(",")
        return (title or None), (authors or None), text[quoted.end() :]

    # Author-year: "Doe, J. (2020). The title. Venue."
    paren_year = re.search(r"\((1[89]\d{2}|20\d{2})\)\.?\s*", text)
    if paren_year:
        authors = text[: paren_year.start()].strip().rstrip(".,")
        rest = text[paren_year.end() :]
        sentence = re.split(r"(?<=[a-z0-9])\.\s+", rest, maxsplit=1)
        title = sentence[0].strip().rstrip(".") if sentence else None
        return (title or None), (authors or None), (sentence[1] if len(sentence) > 1 else "")

    cue = _VENUE_CUE_RE.search(text)
    if cue and cue.start() > 20:
        head = text[: cue.start()]
        parts = re.split(r",\s+(?=[A-Z])", head)
        if len(parts) >= 2:
            return parts[-1].strip().rstrip(",."), ", ".join(parts[:-1]), text[cue.start() :]
    return None, None, text


def _guess_venue(remainder: str) -> str | None:
    """Take the venue from the text following the title."""
    text = remainder.strip().lstrip(",.").strip()
    text = re.sub(r"^(?:in|at)\s+", "", text, flags=re.IGNORECASE)
    # Stop at whatever comes after the venue name: volume, pages, year, DOI.
    text = re.split(
        r",?\s*(?:vol(?:ume)?\.?\s*\d|p{1,2}\.?\s*\d|doi:|https?://|\b(?:1[89]|20)\d{2}\b)",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    text = text.strip().strip(",.;:").strip()
    # "arXiv preprint arXiv:2004.05150" loses its tail to the split above and
    # leaves a stutter; the server name is the only useful part.
    if re.match(r"^arxiv\b", text, re.IGNORECASE):
        return "arXiv"
    return text if 3 <= len(text) <= 200 else None


def _with_confidence(reference: ParsedReference) -> ParsedReference:
    """Score how much of this reference we believe.

    An identifier is worth more than everything else combined, because it makes
    the rest of the parse irrelevant: the registry will supply the truth.
    """
    from dataclasses import replace

    score = 0.0
    if reference.doi or reference.arxiv_id:
        score += 0.6
    if reference.title:
        score += 0.2
    if reference.authors:
        score += 0.1
    if reference.year:
        score += 0.1
    return replace(reference, confidence=round(min(score, 1.0), 2))


def _entry_type(reference: ParsedReference) -> str:
    venue = (reference.venue or "") + " " + reference.raw
    if reference.arxiv_id or re.search(r"\barxiv\b", venue, re.IGNORECASE):
        return "misc"
    # "Advances in Neural Information Processing Systems" and friends are
    # proceedings even though they name neither "proc" nor "conference".
    if re.search(
        r"\b(?:proc\.|conf\.|symp\.|proceedings\b|conference\b|workshop\b|"
        r"symposium\b|advances\s+in\b|annual\s+meeting\b|congress\b)",
        venue,
        re.IGNORECASE,
    ):
        return "inproceedings"
    if re.search(r"\b(thesis|dissertation)\b", venue, re.IGNORECASE):
        return "phdthesis"
    if re.search(r"\b(tech(nical)?\.?\s+rep|report)\b", venue, re.IGNORECASE):
        return "techreport"
    return "article"


def _to_entry(reference: ParsedReference, source: str) -> BibEntry:
    """Build a synthetic :class:`BibEntry` so the normal checks apply."""
    entry_type = _entry_type(reference)
    venue_field = "booktitle" if entry_type == "inproceedings" else "journal"

    fields: dict[str, str] = {}
    order: list[str] = []

    def put(name: str, value: str | None) -> None:
        if value:
            fields[name] = value
            order.append(name)

    put("title", reference.title)
    put("author", _bibtex_authors(reference.authors))
    put(venue_field, reference.venue)
    put("volume", reference.volume)
    put("number", reference.issue)
    put("pages", reference.pages)
    put("year", reference.year)
    put("doi", reference.doi)
    if reference.arxiv_id:
        put("eprint", reference.arxiv_id)
        put("archiveprefix", "arXiv")

    return BibEntry(
        key=reference.key,
        entry_type=entry_type,
        fields=fields,
        field_order=tuple(order),
        start_line=None,
        source=source,
        raw=reference.raw,
    )


def _bibtex_authors(authors: str | None) -> str | None:
    """Convert ``A. Vaswani, N. Shazeer, and I. Polosukhin`` to BibTeX form."""
    if not authors:
        return None
    text = re.sub(r"\bet\s+al\.?", "", authors, flags=re.IGNORECASE)
    text = re.sub(r",?\s+and\s+", ", ", text)
    names = [name.strip().rstrip(",.").strip() for name in text.split(",")]
    kept = [name for name in names if len(name) > 1 and any(c.isalpha() for c in name)]
    return " and ".join(kept) if kept else None


def references_to_bib(
    text: str, *, source: str = "<pdf>"
) -> tuple[ParsedBib, list[ParsedReference]]:
    """Turn a PDF's text into something the engine can check.

    Returns a :class:`ParsedBib` of synthetic entries plus the parsed
    references themselves, so a caller can show the raw text alongside.
    """
    _, block = find_reference_section(text)
    problems: list[Problem] = []

    if not block:
        problems.append(
            Problem(
                code=Code.MALFORMED_ENTRY,
                severity=Severity.WARN,
                message=(
                    "no references section was found; the heading may be "
                    "formatted unusually, or the PDF may be a scan"
                ),
            )
        )
        return ParsedBib(path=source, entries=(), problems=tuple(problems)), []

    references = [parse_reference(marker, raw) for marker, raw in split_references(block)]

    entries: list[BibEntry] = []
    usable: list[ParsedReference] = []
    for reference in references:
        if not reference.usable:
            # Reported rather than dropped: a vanished reference would be a
            # silent lie about how many were checked.
            problems.append(
                Problem(
                    code=Code.MALFORMED_ENTRY,
                    severity=Severity.WARN,
                    message=f"reference [{reference.marker}] could not be parsed",
                    local=reference.raw[:120],
                )
            )
            continue
        entries.append(_to_entry(reference, source))
        usable.append(reference)

    return (
        ParsedBib(path=source, entries=tuple(entries), problems=tuple(problems)),
        usable,
    )


def crosscheck_pdf(body: str, keys: Sequence[str]) -> CrossCheck:
    """Compare the reference list against the citations in the PDF's own body.

    A PDF is both the bibliography and the manuscript, so ``bibcheck
    paper.pdf`` can report an uncited reference with no ``--tex`` at all.
    """
    cited = {f"ref{marker}" for marker in cited_markers(body)}
    defined = list(dict.fromkeys(keys))
    return CrossCheck(
        manuscript="the PDF body",
        uncited=tuple(key for key in defined if key not in cited),
        undefined=tuple(sorted(cited - set(defined), key=_marker_sort)),
    )


def _marker_sort(key: str) -> tuple[int, str]:
    digits = key[3:]
    return (int(digits), "") if digits.isdigit() else (10**9, key)


def cited_markers(body: str) -> set[str]:
    """Every ``[n]`` cited in the body, expanding ranges like ``[1]-[3]``.

    Lets ``bibcheck paper.pdf`` report an uncited reference with no ``--tex``:
    the PDF is both the bibliography and the manuscript.
    """
    found: set[str] = set()
    for match in _CITATION_RE.finditer(body):
        inner = match.group(1)
        for part in re.split(r",", inner):
            part = part.strip()
            span = re.match(r"^(\d{1,3})\s*[-–]\s*(\d{1,3})$", part)
            if span:
                start, end = int(span.group(1)), int(span.group(2))
                if 0 < end - start < 200:
                    found.update(str(number) for number in range(start, end + 1))
            elif part.isdigit():
                found.add(part)
    return found
