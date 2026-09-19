"""Map a Crossref ``message`` object onto bibcheck's own types.

Kept free of HTTP on purpose. Everything here is a pure function over parsed
JSON, so the mapping can be tested exhaustively with no network and no mocking,
and so there is exactly one file to correct if Crossref changes a field.

Every accessor is defensive. Crossref records are heterogeneous -- a field that
is a list on one record is absent on the next, and a publisher can deposit very
nearly anything -- so a surprising shape yields ``None`` rather than an
exception. A missing field means "this source does not say", which the checks
treat as "cannot compare", never as a mismatch.
"""

from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

from .identifiers import normalize_arxiv_id, normalize_doi
from .models import EntryKind, Metadata, WorkRecord
from .names import PersonName
from .normalize import normalize_text, parse_pages, parse_volume

__all__ = [
    "parse_work",
    "work_kind",
    "RETRACTION_TYPES",
    "WITHDRAWAL_TYPES",
    "CONCERN_TYPES",
]

#: Crossref ``update-to``/``updated-by`` types, grouped by what they mean for a
#: citation. ``partial_retraction`` is treated as a retraction: the citing
#: author still needs to look at it before submitting.
RETRACTION_TYPES: Final[frozenset[str]] = frozenset({"retraction", "partial_retraction"})
WITHDRAWAL_TYPES: Final[frozenset[str]] = frozenset({"withdrawal", "removal"})
CONCERN_TYPES: Final[frozenset[str]] = frozenset({"expression_of_concern"})

_KIND_BY_TYPE: Final[dict[str, EntryKind]] = {
    "journal-article": EntryKind.JOURNAL_ARTICLE,
    "proceedings-article": EntryKind.CONFERENCE_PAPER,
    "proceedings": EntryKind.CONFERENCE_PAPER,
    "book": EntryKind.BOOK,
    "monograph": EntryKind.BOOK,
    "reference-book": EntryKind.BOOK,
    "edited-book": EntryKind.BOOK,
    "book-chapter": EntryKind.CHAPTER,
    "book-part": EntryKind.CHAPTER,
    "book-section": EntryKind.CHAPTER,
    "dissertation": EntryKind.THESIS,
    "report": EntryKind.REPORT,
    "report-component": EntryKind.REPORT,
    "posted-content": EntryKind.PREPRINT,
}

#: Date fields consulted in order; the first that yields a year wins.
_DATE_FIELDS: Final[tuple[str, ...]] = (
    "issued", "published", "published-print", "published-online", "created",
)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_string(value: Any) -> str | None:
    """Crossref returns most text fields as a list. Take the first non-empty."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Sequence):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _string(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _year_from(value: Any) -> int | None:
    """Read ``{"date-parts": [[2016, 6, 27]]}``, tolerating partial dates."""
    parts = _as_mapping(value).get("date-parts")
    if not isinstance(parts, Sequence) or isinstance(parts, str):
        return None
    for group in parts:
        if not isinstance(group, Sequence) or isinstance(group, str) or not group:
            continue
        candidate = group[0]
        if isinstance(candidate, int) and 1600 <= candidate <= 2200:
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            year = int(candidate)
            if 1600 <= year <= 2200:
                return year
    return None


def _parse_year(message: Mapping[str, Any]) -> int | None:
    for field in _DATE_FIELDS:
        year = _year_from(message.get(field))
        if year is not None:
            return year
    return None


def _parse_authors(value: Any) -> tuple[PersonName, ...]:
    """Build :class:`PersonName` objects from Crossref's author list.

    Crossref gives ``{"given": ..., "family": ...}`` for people and ``{"name":
    ...}`` for organisations; both appear in the same list.
    """
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    names: list[PersonName] = []
    for item in value:
        entry = _as_mapping(item)
        family = _string(entry.get("family"))
        given = _string(entry.get("given"))
        if family:
            display = f"{family}, {given}" if given else family
            names.append(PersonName(display=display, family=family, given=given or ""))
            continue
        organisation = _string(entry.get("name"))
        if organisation:
            names.append(PersonName(display=organisation, family=organisation, given=""))
    return tuple(names)


def _parse_venue(message: Mapping[str, Any]) -> str | None:
    """Prefer the full container title; fall back through the other holders."""
    for field in ("container-title", "short-container-title", "institution", "publisher"):
        value = message.get(field)
        if field == "institution":
            # ``institution`` is a list of objects, not of strings.
            if isinstance(value, Sequence) and not isinstance(value, str):
                for item in value:
                    name = _string(_as_mapping(item).get("name"))
                    if name:
                        return normalize_text(name)
            continue
        found = _first_string(value)
        if found:
            return normalize_text(found)
    return None


def work_kind(message: Mapping[str, Any]) -> EntryKind:
    """Map Crossref's ``type``/``subtype`` onto our coarse work kind."""
    crossref_type = (_string(message.get("type")) or "").lower()
    kind = _KIND_BY_TYPE.get(crossref_type, EntryKind.OTHER)
    if crossref_type == "posted-content":
        subtype = (_string(message.get("subtype")) or "").lower()
        # A posted-content record that is not a preprint is something else
        # entirely (a report, a comment); do not call it a preprint.
        return EntryKind.PREPRINT if subtype in {"", "preprint"} else EntryKind.OTHER
    return kind


def _update_types(value: Any) -> tuple[str, ...]:
    """Collect the ``type`` of each update record."""
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    found: list[str] = []
    for item in value:
        update_type = _string(_as_mapping(item).get("type"))
        if update_type:
            found.append(update_type.lower().replace("-", "_").replace(" ", "_"))
    return tuple(found)


def _relation_present(message: Mapping[str, Any], name: str) -> bool:
    """Is ``relation.<name>`` present and non-empty?

    Used for lifecycle signals, where the *existence* of the relation is the
    finding. A malformed DOI inside it must not make a retraction invisible --
    the author still needs to be told the work was retracted.
    """
    relation = _as_mapping(message.get("relation")).get(name)
    if not isinstance(relation, Sequence) or isinstance(relation, str):
        return False
    return any(isinstance(item, Mapping) for item in relation)


def _relation_dois(message: Mapping[str, Any], name: str) -> tuple[str, ...]:
    """Pull DOIs out of ``relation.<name>``."""
    relation = _as_mapping(message.get("relation")).get(name)
    if not isinstance(relation, Sequence) or isinstance(relation, str):
        return ()
    found: list[str] = []
    for item in relation:
        entry = _as_mapping(item)
        identifier = _string(entry.get("id"))
        id_type = (_string(entry.get("id-type")) or "doi").lower()
        if identifier and id_type == "doi":
            normalized = normalize_doi(identifier)
            if normalized:
                found.append(normalized)
    return tuple(found)


def _arxiv_id(message: Mapping[str, Any], doi: str | None) -> str | None:
    """Recover an arXiv id from the DOI or from an explicit alternative id."""
    if doi:
        from_doi = normalize_arxiv_id(doi)
        if from_doi is not None:
            return from_doi
    alternatives = message.get("alternative-id")
    if isinstance(alternatives, Sequence) and not isinstance(alternatives, str):
        for item in alternatives:
            if isinstance(item, str):
                found = normalize_arxiv_id(item)
                if found is not None:
                    return found
    return None


def parse_work(
    message: Mapping[str, Any],
    *,
    source: str = "crossref",
    from_cache: bool = False,
    confidence: float = 1.0,
) -> WorkRecord:
    """Build a :class:`WorkRecord` from one Crossref work object.

    Lifecycle is read from both directions, because Crossref records it on
    whichever side deposited it: ``updated-by`` sits on the retracted article
    ("I was retracted by this notice"), while ``update-to`` sits on the notice
    itself ("I retract that article"). A citation can point at either, and a
    reader needs the warning in both cases.
    """
    doi = normalize_doi(_string(message.get("DOI")))
    title = _first_string(message.get("title"))
    subtitle = _first_string(message.get("subtitle"))

    metadata = Metadata(
        title=normalize_text(title) if title else None,
        subtitle=normalize_text(subtitle) if subtitle else None,
        authors=_parse_authors(message.get("author")),
        year=_parse_year(message),
        venue=_parse_venue(message),
        volume=parse_volume(_string(message.get("volume"))),
        issue=parse_volume(_string(message.get("issue"))),
        pages=parse_pages(_string(message.get("page"))),
        publisher=normalize_text(_string(message.get("publisher")) or "") or None,
        doi=doi,
        arxiv_id=_arxiv_id(message, doi),
        kind=work_kind(message),
    )

    incoming = _update_types(message.get("updated-by"))
    outgoing = _update_types(message.get("update-to"))
    notices = tuple(dict.fromkeys(incoming + outgoing))
    retracted_by = _relation_present(message, "is-retracted-by")

    published = _relation_dois(message, "is-preprint-of")

    return WorkRecord(
        metadata=metadata,
        source=source,
        retracted=bool(set(notices) & RETRACTION_TYPES) or retracted_by,
        withdrawn=bool(set(notices) & WITHDRAWAL_TYPES),
        expression_of_concern=bool(set(notices) & CONCERN_TYPES),
        update_notices=notices,
        published_version_doi=published[0] if published else None,
        retrieved_from_cache=from_cache,
        match_confidence=confidence,
    )
