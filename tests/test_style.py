"""Style and hygiene checks. All local — no network, no fixtures."""

from __future__ import annotations

import pytest

from bibcheck.models import Code, Severity
from bibcheck.parsing import parse_bibtex
from bibcheck.style import (
    STYLES,
    check_case_protection,
    check_pages_sanity,
    check_style_fields,
    check_url_entry,
    find_inconsistent_authors,
    find_inconsistent_venues,
    venue_signature,
)

IEEE = STYLES["ieee"]


def entry(source: str):  # type: ignore[no-untyped-def]
    return parse_bibtex(source).entries[0]


def one(source: str):  # type: ignore[no-untyped-def]
    return check_case_protection(entry(source))


class TestCaseProtection:
    """The check that exists because nothing else in the pipeline can find it.

    A title with a bare acronym is correct, resolves fine and drifts from
    nothing — and a style file silently lower-cases it at proof stage.
    """

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("BERT: Pre-training of Transformers", "BERT"),
            ("A Study of CNNs", "CNNs"),
            ("Evaluating GPT-4 on Reasoning", "GPT-4"),
            ("The IEEE 802.11 Standard", "IEEE"),
            ("Lessons from COVID-19", "COVID-19"),
            ("Introducing ResNet Architectures", "ResNet"),
            ("On the iPhone Ecosystem", "iPhone"),
        ],
    )
    def test_unprotected_capitals_are_flagged(self, title: str, expected: str) -> None:
        problems = one("@article{a, title = {%s}, year={2020}}" % title)
        assert problems
        assert expected in problems[0].message
        assert problems[0].severity is Severity.WARN
        assert problems[0].code is Code.CASE_PROTECTION

    @pytest.mark.parametrize(
        "title",
        [
            "{BERT}: Pre-training of Transformers",
            "A Study of {CNNs}",
            "{IEEE} 802.11",
        ],
    )
    def test_already_protected_capitals_are_left_alone(self, title: str) -> None:
        assert one("@article{a, title = {%s}, year={2020}}" % title) == ()

    @pytest.mark.parametrize(
        "title",
        [
            "A perfectly ordinary title",
            "The Quick Brown Fox Jumps",
            "An Analysis of Things and Stuff",
        ],
    )
    def test_ordinary_titles_are_not_flagged(self, title: str) -> None:
        """A check that fires on everything is ignored on everything."""
        assert one("@article{a, title = {%s}, year={2020}}" % title) == ()

    def test_the_suggestion_protects_only_the_exposed_words(self) -> None:
        problems = one(
            "@article{a, title = {Deep Learning with {ResNet} and CNNs}, year={2020}}"
        )
        assert problems[0].authoritative == "Deep Learning with {ResNet} and {CNNs}"

    def test_several_acronyms_are_all_named(self) -> None:
        problems = one("@article{a, title = {BERT and GPT for NLP}, year={2020}}")
        message = problems[0].message
        assert "BERT" in message and "GPT" in message and "NLP" in message

    def test_an_entry_with_no_title_is_skipped(self) -> None:
        assert check_case_protection(entry("@article{a, year={2020}}")) == ()


class TestStyleFields:
    def test_a_complete_ieee_article_passes(self) -> None:
        source = (
            "@article{a, title={T}, author={A}, journal={J}, year={2020},"
            " volume={1}, number={2}, pages={1--2}}"
        )
        assert check_style_fields(entry(source), IEEE) == ()

    def test_a_missing_required_field_is_a_warning(self) -> None:
        problems = check_style_fields(
            entry("@article{a, title={T}, author={A}, year={2020}}"), IEEE
        )
        required = [p for p in problems if p.severity is Severity.WARN]
        assert required and "journal" in required[0].message

    def test_a_missing_recommended_field_is_only_informational(self) -> None:
        problems = check_style_fields(
            entry("@article{a, title={T}, author={A}, journal={J}, year={2020}}"), IEEE
        )
        assert all(p.severity is Severity.INFO for p in problems)

    def test_an_editor_satisfies_the_author_requirement(self) -> None:
        """An edited volume legitimately has no author."""
        source = "@inproceedings{a, title={T}, editor={E}, booktitle={B}, year={2020}}"
        warnings = [
            p for p in check_style_fields(entry(source), IEEE) if p.severity is Severity.WARN
        ]
        assert warnings == []

    def test_an_unknown_entry_type_is_not_policed(self) -> None:
        assert check_style_fields(entry("@weirdtype{a, title={T}}"), IEEE) == ()


class TestPagesSanity:
    def test_a_backwards_range_is_flagged_with_the_fix(self) -> None:
        problems = check_pages_sanity(entry("@article{a, pages={778--770}}"))
        assert problems[0].code is Code.SUSPECT_PAGES
        assert problems[0].authoritative == "770--778"

    def test_a_normal_range_passes(self) -> None:
        assert check_pages_sanity(entry("@article{a, pages={770--778}}")) == ()

    def test_a_single_page_passes(self) -> None:
        assert check_pages_sanity(entry("@article{a, pages={42}}")) == ()

    def test_an_absurd_span_is_only_informational(self) -> None:
        problems = check_pages_sanity(entry("@article{a, pages={1--5000}}"))
        assert problems[0].severity is Severity.INFO

    def test_non_numeric_pages_are_left_alone(self) -> None:
        assert check_pages_sanity(entry("@article{a, pages={e1234}}")) == ()
        assert check_pages_sanity(entry("@article{a, pages={to appear}}")) == ()


class TestUrlEntries:
    def test_a_url_without_an_access_date_is_flagged(self) -> None:
        problems = check_url_entry(entry("@misc{a, url={https://example.org}}"))
        assert problems[0].code is Code.STALE_URL_ENTRY

    def test_a_urldate_satisfies_it(self) -> None:
        source = "@misc{a, url={https://example.org}, urldate={2024-01-01}}"
        assert check_url_entry(entry(source)) == ()

    def test_a_doi_makes_the_url_incidental(self) -> None:
        source = "@article{a, url={https://example.org}, doi={10.1000/x}}"
        assert check_url_entry(entry(source)) == ()

    def test_an_entry_with_no_url_is_skipped(self) -> None:
        assert check_url_entry(entry("@article{a, title={T}}")) == ()


class TestVenueSignature:
    def test_an_abbreviation_and_its_full_name_share_a_signature(self) -> None:
        """The trick that makes inconsistent venues findable at all."""
        assert venue_signature(
            "IEEE Transactions on Pattern Analysis and Machine Intelligence"
        ) == venue_signature("IEEE Trans. Pattern Anal. Mach. Intell.")

    def test_different_venues_differ(self) -> None:
        assert venue_signature("Nature Machine Intelligence") != venue_signature(
            "Journal of Marine Biology"
        )


class TestInconsistentVenues:
    def test_two_spellings_of_one_venue_are_reported(self) -> None:
        source = (
            "@article{a, title={A}, journal={IEEE Transactions on Pattern Analysis"
            " and Machine Intelligence}, year={2020}}\n"
            "@article{b, title={B}, journal={IEEE Trans. Pattern Anal. Mach."
            " Intell.}, year={2021}}\n"
        )
        found = find_inconsistent_venues(parse_bibtex(source).entries)
        # Only the minority (abbreviated) spelling is asked to change.
        assert "b" in found
        assert found["b"][0].code is Code.INCONSISTENT_VENUE
        assert "Transactions" in (found["b"][0].authoritative or "")

    def test_a_consistent_bibliography_reports_nothing(self) -> None:
        source = (
            "@article{a, title={A}, journal={Nature}, year={2020}}\n"
            "@article{b, title={B}, journal={Nature}, year={2021}}\n"
        )
        assert find_inconsistent_venues(parse_bibtex(source).entries) == {}

    def test_genuinely_different_venues_are_not_conflated(self) -> None:
        source = (
            "@article{a, title={A}, journal={Nature Machine Intelligence}, year={2020}}\n"
            "@article{b, title={B}, journal={Journal of Marine Biology}, year={2021}}\n"
        )
        assert find_inconsistent_venues(parse_bibtex(source).entries) == {}


class TestInconsistentAuthors:
    def test_initials_and_a_full_name_for_one_person_are_reported(self) -> None:
        source = (
            "@article{a, title={A}, author={K. He}, journal={J}, year={2020}}\n"
            "@article{b, title={B}, author={Kaiming He}, journal={J}, year={2021}}\n"
            "@article{c, title={C}, author={Kaiming He}, journal={J}, year={2022}}\n"
        )
        found = find_inconsistent_authors(parse_bibtex(source).entries)
        assert "a" in found  # the minority spelling
        assert found["a"][0].code is Code.INCONSISTENT_AUTHOR

    def test_a_consistent_bibliography_reports_nothing(self) -> None:
        source = (
            "@article{a, title={A}, author={Kaiming He}, journal={J}, year={2020}}\n"
            "@article{b, title={B}, author={Kaiming He}, journal={J}, year={2021}}\n"
        )
        assert find_inconsistent_authors(parse_bibtex(source).entries) == {}

    def test_different_people_are_not_conflated(self) -> None:
        source = (
            "@article{a, title={A}, author={Kaiming He}, journal={J}, year={2020}}\n"
            "@article{b, title={B}, author={Jane Doe}, journal={J}, year={2021}}\n"
        )
        assert find_inconsistent_authors(parse_bibtex(source).entries) == {}
