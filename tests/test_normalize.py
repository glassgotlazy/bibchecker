"""Comparison keys, similarity and field parsing."""

from __future__ import annotations

import pytest

from bibcheck.normalize import (
    PageRange,
    looks_abbreviated,
    parse_pages,
    parse_volume,
    parse_year,
    similarity,
    title_key,
    titles_match,
    venue_key,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Case, punctuation and articles are all cosmetic.
        ("Attention Is All You Need", "attention is all you need"),
        ("Deep Learning: A Review", "Deep Learning - A Review"),
        ("The Quick Brown Fox", "Quick Brown Fox"),
        # LaTeX accents versus the precomposed characters.
        (r"Caf\'{e} Culture", "Café Culture"),
        (r"Caf\'{e} Culture", "Cafe Culture"),
        # "&" and "and" are the same word.
        ("Speech & Language Processing", "Speech and Language Processing"),
        # Braces, hyphenation and whitespace.
        ("A {Study} of {BERT}", "A Study of BERT"),
        ("Multi-Task Learning", "Multi Task Learning"),
        ("Trailing space   ", "Trailing space"),
    ],
)
def test_cosmetic_differences_share_a_title_key(left: str, right: str) -> None:
    assert title_key(left) == title_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Deep Residual Learning", "Deep Recurrent Learning"),
        ("A Study of Cats", "A Study of Dogs"),
        ("Attention Is All You Need", "Attention Is Not All You Need"),
    ],
)
def test_substantive_differences_do_not_share_a_title_key(left: str, right: str) -> None:
    assert title_key(left) != title_key(right)


def test_titles_match_tolerates_a_missing_subtitle() -> None:
    assert titles_match(
        "Longformer: The Long-Document Transformer", "Longformer"
    )


def test_titles_match_rejects_a_different_paper() -> None:
    assert not titles_match(
        "Deep Residual Learning for Image Recognition",
        "Generative Adversarial Networks",
    )


def test_similarity_is_bounded_and_ordered() -> None:
    exact = similarity(title_key("Deep Learning"), title_key("deep learning"))
    close = similarity(title_key("Deep Learning Methods"), title_key("Deep Learning"))
    far = similarity(title_key("Deep Learning"), title_key("Quantum Chromodynamics"))
    assert exact == 1.0
    assert 0.0 <= far < close < exact
    assert similarity("", "anything") == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2020", 2020),
        ("{2020}", 2020),
        ("2020-06-01", 2020),
        ("June 2020", 2020),
        ("1999", 1999),
        ("in press", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_year(raw: str | None, expected: int | None) -> None:
    assert parse_year(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10--20", PageRange("10", "20")),
        ("10-20", PageRange("10", "20")),
        ("10 -- 20", PageRange("10", "20")),
        ("10 to 20", PageRange("10", "20")),
        # The elided form: "1234--56" means 1234 through 1256.
        ("1234--56", PageRange("1234", "1256")),
        ("770--778", PageRange("770", "778")),
        ("42", PageRange("42", None)),
        ("e1234", PageRange("e1234", None)),
        # Unparseable means "cannot compare", never a false mismatch.
        ("to appear", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_pages(raw: str | None, expected: PageRange | None) -> None:
    assert parse_pages(raw) == expected


def test_page_range_key_is_comparable_across_spellings() -> None:
    assert parse_pages("10--20") == parse_pages("10-20")
    assert parse_pages("770--778") != parse_pages("770--779")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", "12"), ("vol. 12", "12"), ("{7}", "7"), ("VII", "VII"), (None, None)],
)
def test_parse_volume(raw: str | None, expected: str | None) -> None:
    assert parse_volume(raw) == expected


def test_venue_key_keeps_stopwords_but_folds_the_rest() -> None:
    assert venue_key("Proceedings of the IEEE") == venue_key("proceedings of the ieee")
    assert "of" in venue_key("Proceedings of the IEEE")


@pytest.mark.parametrize(
    ("venue", "abbreviated"),
    [
        ("IEEE Trans. Pattern Anal. Mach. Intell.", True),
        ("Proc. CVPR", True),
        ("Nature Machine Intelligence", False),
        ("Journal of Machine Learning Research", False),
    ],
)
def test_looks_abbreviated(venue: str, abbreviated: bool) -> None:
    assert looks_abbreviated(venue) is abbreviated
