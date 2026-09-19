"""Citation key suggestion."""

from __future__ import annotations

from bibcheck.keys import suggest_key, unique_key
from bibcheck.models import Metadata
from bibcheck.names import parse_author_field


def meta(**overrides: object) -> Metadata:
    base: dict[str, object] = {
        "title": "Deep Residual Learning for Image Recognition",
        "authors": parse_author_field("He, Kaiming and Zhang, Xiangyu"),
        "year": 2016,
    }
    base.update(overrides)
    return Metadata(**base)  # type: ignore[arg-type]


def test_the_conventional_shape() -> None:
    assert suggest_key(meta()) == "he2016residual"


def test_a_distinctive_word_is_chosen_over_the_first_one() -> None:
    """"deep" and "learning" identify nothing; half a bibliography collides."""
    assert "residual" in suggest_key(meta())


def test_accents_are_folded() -> None:
    key = suggest_key(meta(authors=parse_author_field(r"Sch{\"o}lkopf, Bernhard")))
    assert key.startswith("scholkopf")


def test_missing_pieces_degrade_gracefully() -> None:
    assert suggest_key(meta(authors=())) == "2016residual"
    assert suggest_key(meta(year=None)) == "heresidual"
    assert suggest_key(Metadata()) == "reference"


def test_a_title_of_only_stopwords_still_yields_a_key() -> None:
    assert suggest_key(meta(title="A New Approach")) == "he2016"


def test_unique_key_disambiguates_alphabetically() -> None:
    assert unique_key("he2016", []) == "he2016"
    assert unique_key("he2016", ["he2016"]) == "he2016a"
    assert unique_key("he2016", ["he2016", "he2016a"]) == "he2016b"


def test_unique_key_falls_back_to_numbers_when_exhausted() -> None:
    taken = ["he2016"] + [f"he2016{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"]
    assert unique_key("he2016", taken) == "he20162"
