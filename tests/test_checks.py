"""The six checks. Every rule here is pure, so no HTTP is involved at all."""

from __future__ import annotations

import pytest

from bibcheck.checks import (
    check_drift,
    check_existence,
    check_lifecycle,
    check_preprint_superseded,
    crosscheck_manuscript,
    extract_citation_keys,
    find_duplicates,
)
from bibcheck.models import Code, EntryKind, Metadata, Severity, WorkRecord
from bibcheck.names import parse_author_field
from bibcheck.normalize import parse_pages


def meta(**overrides: object) -> Metadata:
    base: dict[str, object] = {
        "title": "Deep Residual Learning for Image Recognition",
        "authors": parse_author_field("He, Kaiming and Zhang, Xiangyu"),
        "year": 2016,
        "venue": "Conference on Computer Vision and Pattern Recognition",
        "volume": "1",
        "pages": parse_pages("770--778"),
        "doi": "10.1109/cvpr.2016.90",
    }
    base.update(overrides)
    return Metadata(**base)  # type: ignore[arg-type]


def record(**overrides: object) -> WorkRecord:
    metadata_overrides = {
        k: v for k, v in overrides.items() if k in Metadata.__dataclass_fields__
    }
    record_overrides = {
        k: v for k, v in overrides.items() if k not in Metadata.__dataclass_fields__
    }
    return WorkRecord(metadata=meta(**metadata_overrides), **record_overrides)  # type: ignore[arg-type]


def codes(problems: tuple[object, ...]) -> list[Code]:
    return [problem.code for problem in problems]  # type: ignore[attr-defined]


class TestExistence:
    def test_a_resolving_doi_produces_nothing(self) -> None:
        assert check_existence(doi="10.1/x", found=True) == ()

    def test_a_fabricated_doi_is_critical(self) -> None:
        problems = check_existence(doi="10.9999/fake", found=False)
        assert codes(problems) == [Code.DOI_NOT_FOUND]
        assert problems[0].severity is Severity.CRITICAL
        assert problems[0].local == "10.9999/fake"
        assert problems[0].url == "https://doi.org/10.9999/fake"

    def test_an_unmatched_entry_without_a_doi_is_only_a_warning(self) -> None:
        """Not in Crossref is not the same claim as "this DOI is fake".

        Plenty of real work is absent from Crossref. Reporting it as CRITICAL
        would cry wolf on every thesis and technical report.
        """
        problems = check_existence(doi=None, found=False)
        assert codes(problems) == [Code.NOT_FOUND_BY_TITLE]
        assert problems[0].severity is Severity.WARN

    def test_a_title_match_is_flagged_as_lower_confidence(self) -> None:
        problems = check_existence(doi=None, found=True, confidence=0.88)
        assert codes(problems) == [Code.LOW_CONFIDENCE_MATCH]
        assert problems[0].confidence == 0.88


class TestDrift:
    def test_an_identical_record_produces_nothing(self) -> None:
        assert check_drift(meta(), record()) == ()

    def test_a_wrong_year_is_reported_with_both_values(self) -> None:
        problems = check_drift(meta(year=2021), record(year=2016))
        assert codes(problems) == [Code.YEAR_MISMATCH]
        assert problems[0].local == "2021"
        assert problems[0].authoritative == "2016"

    def test_a_wrong_title_is_reported(self) -> None:
        problems = check_drift(meta(title="An Entirely Different Paper"), record())
        assert Code.TITLE_MISMATCH in codes(problems)

    def test_a_wrong_author_is_reported(self) -> None:
        problems = check_drift(
            meta(authors=parse_author_field("Jones, Bob")), record()
        )
        assert Code.AUTHOR_MISMATCH in codes(problems)

    def test_wrong_volume_and_pages_are_reported(self) -> None:
        problems = check_drift(
            meta(volume="9", pages=parse_pages("1--2")), record()
        )
        assert set(codes(problems)) == {Code.VOLUME_MISMATCH, Code.PAGES_MISMATCH}

    @pytest.mark.parametrize(
        "local_title",
        [
            r"Deep Residual Learning for Image Recognition",
            "deep residual learning for image recognition",
            "Deep Residual Learning for Image Recognition.",
            "{Deep} {Residual} Learning for Image Recognition",
        ],
    )
    def test_cosmetic_title_differences_are_never_reported(self, local_title: str) -> None:
        assert check_drift(meta(title=local_title), record()) == ()

    def test_latex_accents_do_not_produce_drift(self) -> None:
        """The case the whole normalization layer exists for."""
        local = meta(title=r"Caf\'{e} Culture and Na{\"\i}ve Bayes")
        authoritative = record(title="Café Culture and Naïve Bayes")
        assert check_drift(local, authoritative) == ()

    def test_initials_versus_full_names_are_not_drift(self) -> None:
        local = meta(authors=parse_author_field("K. He and X. Zhang"))
        assert check_drift(local, record()) == ()

    def test_a_truncated_author_list_is_not_drift(self) -> None:
        local = meta(authors=parse_author_field("He, Kaiming"))
        assert check_drift(local, record()) == ()

    def test_a_field_missing_on_either_side_is_not_drift(self) -> None:
        """Absence must never be reported as disagreement."""
        assert check_drift(meta(year=None), record()) == ()
        assert check_drift(meta(), record(year=None)) == ()
        assert check_drift(meta(volume=None, pages=None), record()) == ()

    def test_every_difference_is_reported_not_just_the_first(self) -> None:
        problems = check_drift(
            meta(year=1999, volume="9", title="Something Else Completely"), record()
        )
        assert len(problems) >= 3


class TestVenueDrift:
    def test_an_identical_venue_produces_nothing(self) -> None:
        assert check_drift(meta(), record()) == ()

    def test_an_abbreviated_venue_is_informational_not_a_warning(self) -> None:
        """House style is not an error, and warning about it trains authors
        to ignore warnings."""
        problems = check_drift(
            meta(venue="IEEE Trans. Pattern Anal. Mach. Intell."),
            record(venue="Nature Reviews Genetics"),
        )
        assert codes(problems) == [Code.VENUE_MISMATCH]
        assert problems[0].severity is Severity.INFO

    def test_a_genuinely_wrong_venue_is_a_warning(self) -> None:
        problems = check_drift(
            meta(venue="Journal of Marine Biology"),
            record(venue="Nature Reviews Genetics"),
        )
        assert codes(problems) == [Code.VENUE_MISMATCH]
        assert problems[0].severity is Severity.WARN

    def test_one_venue_containing_the_other_is_not_drift(self) -> None:
        local = meta(venue="Computer Vision and Pattern Recognition")
        authoritative = record(
            venue="2016 IEEE Conference on Computer Vision and Pattern Recognition"
        )
        assert check_drift(local, authoritative) == ()


class TestLifecycle:
    def test_a_clean_record_produces_nothing(self) -> None:
        assert check_lifecycle(record()) == ()

    def test_a_retraction_is_critical(self) -> None:
        problems = check_lifecycle(record(retracted=True))
        assert codes(problems) == [Code.RETRACTED]
        assert problems[0].severity is Severity.CRITICAL

    def test_a_withdrawal_is_critical(self) -> None:
        assert check_lifecycle(record(withdrawn=True))[0].severity is Severity.CRITICAL

    def test_an_expression_of_concern_is_critical(self) -> None:
        problems = check_lifecycle(record(expression_of_concern=True))
        assert codes(problems) == [Code.EXPRESSION_OF_CONCERN]
        assert problems[0].severity is Severity.CRITICAL

    def test_several_flags_are_all_reported(self) -> None:
        problems = check_lifecycle(record(retracted=True, expression_of_concern=True))
        assert set(codes(problems)) == {Code.RETRACTED, Code.EXPRESSION_OF_CONCERN}


class TestPreprintSuperseded:
    def test_a_preprint_with_a_published_version_is_reported(self) -> None:
        local = meta(kind=EntryKind.PREPRINT, arxiv_id="2004.05150", doi=None)
        problems = check_preprint_superseded(
            local, record(published_version_doi="10.1000/published.2021")
        )
        assert codes(problems) == [Code.PREPRINT_SUPERSEDED]
        assert problems[0].authoritative == "10.1000/published.2021"

    def test_the_published_venue_is_named_when_known(self) -> None:
        local = meta(kind=EntryKind.PREPRINT, arxiv_id="2004.05150", doi=None)
        published = WorkRecord(
            metadata=Metadata(
                title="Longformer", venue="Proceedings of ACL", year=2021,
                doi="10.1000/published.2021",
            )
        )
        problems = check_preprint_superseded(
            local,
            record(published_version_doi="10.1000/published.2021"),
            published=published,
        )
        assert "Proceedings of ACL" in problems[0].message
        assert "2021" in problems[0].message

    def test_a_non_preprint_is_never_reported(self) -> None:
        assert check_preprint_superseded(
            meta(), record(published_version_doi="10.1000/other")
        ) == ()

    def test_a_preprint_with_no_published_version_is_not_reported(self) -> None:
        local = meta(kind=EntryKind.PREPRINT, arxiv_id="2004.05150", doi=None)
        assert check_preprint_superseded(local, record(published_version_doi=None)) == ()

    def test_an_entry_already_citing_the_published_version_is_left_alone(self) -> None:
        """Crossref records the relation on both sides; only one is actionable."""
        local = meta(kind=EntryKind.PREPRINT, doi="10.1000/published.2021")
        assert check_preprint_superseded(
            local, record(published_version_doi="10.1000/published.2021")
        ) == ()


class TestDuplicates:
    def test_the_same_doi_under_two_keys_is_reported_on_both(self) -> None:
        found = find_duplicates(
            [("a", meta()), ("b", meta(title="Written Differently But Same DOI"))]
        )
        assert set(found) == {"a", "b"}
        assert found["a"][0].authoritative == "b"
        assert found["b"][0].authoritative == "a"

    def test_the_same_title_and_year_is_reported(self) -> None:
        found = find_duplicates([("a", meta(doi=None)), ("b", meta(doi="10.1/other"))])
        assert set(found) == {"a", "b"}

    def test_the_same_arxiv_id_is_reported(self) -> None:
        left = meta(doi=None, arxiv_id="2004.05150", title="One Spelling")
        right = meta(doi=None, arxiv_id="2004.05150", title="Another Spelling")
        assert set(find_duplicates([("a", left), ("b", right)])) == {"a", "b"}

    def test_distinct_works_are_not_reported(self) -> None:
        other = meta(doi="10.1/other", title="A Totally Different Paper", year=2001)
        assert find_duplicates([("a", meta()), ("b", other)]) == {}

    def test_the_same_title_in_different_years_is_not_a_duplicate(self) -> None:
        """A conference paper and its extended journal version share a title
        and are genuinely two different citations."""
        conference = meta(doi="10.1/conf", year=2016)
        journal = meta(doi="10.1/journal", year=2018)
        assert find_duplicates([("a", conference), ("b", journal)]) == {}

    def test_a_group_of_three_names_the_other_two(self) -> None:
        found = find_duplicates([("a", meta()), ("b", meta()), ("c", meta())])
        assert set(found) == {"a", "b", "c"}
        assert found["a"][0].authoritative == "b, c"

    def test_matching_on_both_doi_and_title_reports_once(self) -> None:
        assert len(find_duplicates([("a", meta()), ("b", meta())])["a"]) == 1

    def test_entries_with_no_identity_at_all_are_not_duplicates(self) -> None:
        blank = Metadata()
        assert find_duplicates([("a", blank), ("b", blank)]) == {}


class TestCitationExtraction:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (r"\cite{smith2020}", {"smith2020"}),
            (r"\cite{a,b, c}", {"a", "b", "c"}),
            (r"\citep{a} and \citet{b}", {"a", "b"}),
            (r"\citeauthor{a} \citeyear{b}", {"a", "b"}),
            (r"\cite[p.~5]{a}", {"a"}),
            (r"\cite[see][p.~5]{a}", {"a"}),
            (r"\parencite{a} \textcite{b} \autocite{c}", {"a", "b", "c"}),
            (r"\footcite{a}", {"a"}),
            (r"\nocite{a}", {"a"}),
            (r"\cite*{a}", {"a"}),
            (r"\Cite{a}", {"a"}),
            ("[@pandoc2020]", {"pandoc2020"}),
            ("[-@pandoc2020; @other]", {"pandoc2020", "other"}),
            ("no citations here", set()),
        ],
    )
    def test_citation_commands(self, source: str, expected: set[str]) -> None:
        assert extract_citation_keys(source) == expected

    def test_commented_out_citations_are_ignored(self) -> None:
        """A key surviving only in a comment is genuinely uncited -- which is
        exactly the stale entry the author is looking for."""
        assert extract_citation_keys("% \\cite{old}\n\\cite{new}") == {"new"}

    def test_an_escaped_percent_does_not_start_a_comment(self) -> None:
        assert extract_citation_keys(r"100\% of \cite{a}") == {"a"}

    def test_nocite_star_is_not_treated_as_a_key(self) -> None:
        assert extract_citation_keys(r"\nocite{*}") == set()


class TestCrossCheck:
    def test_uncited_entries_are_listed(self) -> None:
        result = crosscheck_manuscript(["a", "b", "c"], r"\cite{a}")
        assert result.uncited == ("b", "c")
        assert result.undefined == ()

    def test_undefined_keys_are_listed(self) -> None:
        result = crosscheck_manuscript(["a"], r"\cite{a,missing}")
        assert result.undefined == ("missing",)
        assert result.uncited == ()

    def test_nocite_star_suppresses_uncited_findings(self) -> None:
        """``\\nocite{*}`` means "include everything", so nothing is unused."""
        result = crosscheck_manuscript(["a", "b"], r"\nocite{*}")
        assert result.uncited == ()

    def test_a_fully_consistent_pair_reports_nothing(self) -> None:
        result = crosscheck_manuscript(["a", "b"], r"\cite{a}\cite{b}")
        assert result.uncited == ()
        assert result.undefined == ()

    def test_bibliography_order_is_preserved_in_uncited(self) -> None:
        result = crosscheck_manuscript(["z", "a", "m"], "")
        assert result.uncited == ("z", "a", "m")
