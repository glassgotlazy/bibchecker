"""Extraction and normalisation of DOIs and arXiv identifiers."""

from __future__ import annotations

import re
from typing import Final, Mapping

__all__ = [
    "normalize_doi",
    "is_plausible_doi",
    "extract_doi",
    "normalize_arxiv_id",
    "extract_arxiv_id",
    "doi_url",
    "arxiv_url",
]

# A DOI is ``10.<registrant>/<suffix>``. The registrant is 4+ digits (possibly
# dotted); the suffix is anything non-space. Case is insignificant, so keys are
# lowercased -- but the *displayed* value keeps whatever the author wrote.
_DOI_CORE: Final[str] = r"10\.\d{4,9}(?:\.\d+)*/[^\s\"<>{}]+"
_DOI_RE: Final[re.Pattern[str]] = re.compile(_DOI_CORE, re.IGNORECASE)
_DOI_FULL_RE: Final[re.Pattern[str]] = re.compile(rf"^{_DOI_CORE}$", re.IGNORECASE)
_DOI_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:(?:https?://)?(?:dx\.)?doi\.org/|doi:\s*|info:doi/|urn:doi:)",
    re.IGNORECASE,
)
# Trailing punctuation picked up from prose or a LaTeX brace group.
_DOI_TRAILING: Final[str] = ".,;:)}]>'\"\\"

# Post-2007 style ``2103.14030`` (optionally ``v3``) and pre-2007 style
# ``math.GT/0309136`` / ``cs/9901002``.
_ARXIV_NEW: Final[str] = r"\d{4}\.\d{4,5}(?:v\d+)?"
_ARXIV_OLD: Final[str] = r"[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
_ARXIV_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:arxiv[:\s/]*)?({_ARXIV_NEW}|{_ARXIV_OLD})", re.IGNORECASE
)
_ARXIV_DOI_RE: Final[re.Pattern[str]] = re.compile(
    rf"^10\.48550/arxiv\.({_ARXIV_NEW}|{_ARXIV_OLD})$", re.IGNORECASE
)
# The bare form, anchored: only a value that is *entirely* an arXiv id counts.
_ARXIV_BARE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:arxiv[:\s]*)?({_ARXIV_NEW}|{_ARXIV_OLD})$", re.IGNORECASE
)
# The embedded form: an id is only believed inside prose when an explicit arXiv
# marker sits next to it. Without this, the DOI ``10.5555/3295222.3295349``
# contains something shaped exactly like ``3295.3295349`` and gets misread as a
# preprint id.
_ARXIV_MARKED_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:arxiv\s*[:.]?\s*|arxiv\.org/(?:abs|pdf)/)({_ARXIV_NEW}|{_ARXIV_OLD})",
    re.IGNORECASE,
)

#: Bib fields searched for an identifier, in order of trustworthiness.
_DOI_FIELDS: Final[tuple[str, ...]] = ("doi", "ee", "url", "note", "howpublished")
#: Fields whose entire value is expected to be the identifier.
_ARXIV_BARE_FIELDS: Final[tuple[str, ...]] = ("eprint", "arxivid", "arxiv")
#: Fields where an identifier may be embedded in prose or a URL.
_ARXIV_PROSE_FIELDS: Final[tuple[str, ...]] = (
    "url", "journal", "journaltitle", "booktitle", "note", "howpublished", "series",
)


def normalize_doi(value: str | None) -> str | None:
    """Reduce any DOI spelling to the bare lowercase ``10.x/y`` form.

    ``https://doi.org/10.1109/ABC.2020``, ``doi:10.1109/abc.2020`` and
    ``{10.1109/ABC.2020}`` all key to ``10.1109/abc.2020``.
    """
    if value is None:
        return None
    text = value.strip().strip("{}").strip()
    text = _DOI_PREFIX_RE.sub("", text).strip()
    text = text.rstrip(_DOI_TRAILING)
    if not text:
        return None
    return text.lower() if _DOI_FULL_RE.match(text) else None


def is_plausible_doi(value: str | None) -> bool:
    """Structurally valid DOI? Says nothing about whether it *resolves*."""
    return normalize_doi(value) is not None


def extract_doi(fields: Mapping[str, str]) -> str | None:
    """Find a DOI anywhere in an entry's fields, preferring the ``doi`` field."""
    for name in _DOI_FIELDS:
        raw = fields.get(name)
        if raw is None:
            continue
        direct = normalize_doi(raw)
        if direct is not None:
            return direct
        match = _DOI_RE.search(raw)
        if match:
            found = normalize_doi(match.group(0))
            if found is not None:
                return found
    return None


def normalize_arxiv_id(value: str | None) -> str | None:
    """Reduce a value that *is* an arXiv reference to its bare id, version stripped.

    Accepts ``arXiv:2103.14030v2``, ``2103.14030``, ``cs/9901002``, an
    ``arxiv.org`` URL and the DataCite DOI ``10.48550/arXiv.2103.14030``.
    Returns ``None`` for anything that merely *contains* a lookalike substring --
    see :func:`extract_arxiv_id` for the field-aware search.
    """
    if value is None:
        return None
    text = value.strip().strip("{}").strip()
    if not text:
        return None

    doi_match = _ARXIV_DOI_RE.match(_DOI_PREFIX_RE.sub("", text).strip())
    if doi_match:
        return _strip_version(doi_match.group(1))

    bare_match = _ARXIV_BARE_RE.match(text)
    if bare_match:
        return _strip_version(bare_match.group(1))

    marked_match = _ARXIV_MARKED_RE.search(text)
    if marked_match:
        return _strip_version(marked_match.group(1))
    return None


def _strip_version(identifier: str) -> str:
    return re.sub(r"v\d+$", "", identifier)


def extract_arxiv_id(fields: Mapping[str, str]) -> str | None:
    """Find an arXiv identifier in an entry's fields.

    Each field is searched with the strictness it deserves: ``eprint`` must be
    the id itself, ``doi`` must be the registered ``10.48550`` form, and free
    text must carry an explicit ``arXiv`` marker. ``archivePrefix`` naming a
    different server suppresses the search entirely.
    """
    prefix = (fields.get("archiveprefix") or fields.get("archivePrefix") or "").strip().lower()
    if prefix and "arxiv" not in prefix:
        return None

    for name in _ARXIV_BARE_FIELDS:
        raw = fields.get(name)
        if raw is None:
            continue
        match = _ARXIV_BARE_RE.match(raw.strip().strip("{}").strip())
        if match:
            return _strip_version(match.group(1))

    doi = fields.get("doi")
    if doi is not None:
        doi_match = _ARXIV_DOI_RE.match(_DOI_PREFIX_RE.sub("", doi.strip().strip("{}")).strip())
        if doi_match:
            return _strip_version(doi_match.group(1))

    for name in _ARXIV_PROSE_FIELDS:
        raw = fields.get(name)
        if raw is None:
            continue
        marked = _ARXIV_MARKED_RE.search(raw)
        if marked:
            return _strip_version(marked.group(1))
    return None


def doi_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


def arxiv_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"
