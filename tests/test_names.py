"""Author name parsing and comparison."""

from __future__ import annotations

import pytest

from bibcheck.names import authors_match, parse_author_field, parse_name, split_authors


@pytest.mark.parametrize(
    ("raw", "family", "given"),
    [
        ("Smith, John", "Smith", "John"),
        ("John Smith", "Smith", "John"),
        ("J. Smith", "Smith", "J."),
        ("Smith, John A.", "Smith", "John A."),
        ("Kaiming He", "He", "Kaiming"),
        # Lowercase particles belong to the surname.
        ("Ludwig van Beethoven", "van Beethoven", "Ludwig"),
        ("van der Berg, Jan", "van der Berg", "Jan"),
        ("Jan van der Berg", "van der Berg", "Jan"),
        # Suffixes, in both the strict and the common spelling.
        ("Doe, Jr., John", "Doe", "John"),
        ("Martin Luther King, Jr.", "King", "Martin Luther"),
        # A single token is a surname, not a given name.
        ("Madonna", "Madonna", ""),
        # Accents survive into the display form.
        (r"G\"{o}kce, {\c{C}}etin", "Gökce", "Çetin"),
    ],
)
def test_parse_name(raw: str, family: str, given: str) -> None:
    name = parse_name(raw)
    assert name.family == family
    assert name.given == given


def test_braced_corporate_author_is_not_split() -> None:
    name = parse_name("{Google Research}")
    assert name.family == "Google Research"
    assert name.given == ""


def test_and_inside_braces_is_not_a_separator() -> None:
    """``{Smith and Wesson Ltd.}`` is one author, not two."""
    assert split_authors("{Smith and Wesson Ltd.} and Doe, Jane") == [
        "{Smith and Wesson Ltd.}",
        "Doe, Jane",
    ]


def test_family_key_folds_accents_and_case() -> None:
    assert parse_name(r"Sch{\"o}lkopf, Bernhard").family_key == "scholkopf"
    assert parse_name("SCHOLKOPF, B.").family_key == "scholkopf"


def test_parse_author_field_drops_others() -> None:
    names = parse_author_field("Vaswani, Ashish and Shazeer, Noam and others")
    assert [name.family for name in names] == ["Vaswani", "Shazeer"]


def test_parse_author_field_of_none_is_empty() -> None:
    assert parse_author_field(None) == ()


def test_initials_match_full_given_names() -> None:
    assert authors_match(
        parse_author_field("J. Smith and J. Doe"),
        parse_author_field("John Smith and Jane Doe"),
    )


def test_a_different_initial_is_a_mismatch() -> None:
    assert not authors_match(
        parse_author_field("A. Smith"), parse_author_field("John Smith")
    )


def test_a_missing_given_name_is_not_evidence_of_a_mismatch() -> None:
    assert authors_match(parse_author_field("Smith"), parse_author_field("John Smith"))


def test_a_different_surname_is_a_mismatch() -> None:
    assert not authors_match(
        parse_author_field("John Schmidt"), parse_author_field("John Smith")
    )


def test_a_truncated_list_matches_a_prefix_of_the_full_list() -> None:
    """The ``et al.`` case: fewer local authors is normal, not an error."""
    assert authors_match(
        parse_author_field("Vaswani, Ashish and Shazeer, Noam"),
        parse_author_field("Ashish Vaswani and Noam Shazeer and Niki Parmar"),
    )


def test_extra_local_authors_are_a_mismatch() -> None:
    assert not authors_match(
        parse_author_field("A. Vaswani and N. Shazeer and N. Parmar"),
        parse_author_field("Ashish Vaswani and Noam Shazeer"),
    )


def test_author_order_matters_by_default() -> None:
    reversed_list = parse_author_field("Jane Doe and John Smith")
    ordered = parse_author_field("John Smith and Jane Doe")
    assert not authors_match(reversed_list, ordered)
    assert authors_match(reversed_list, ordered, require_order=False)


def test_empty_lists_never_match() -> None:
    assert not authors_match((), parse_author_field("John Smith"))
    assert not authors_match(parse_author_field("John Smith"), ())
