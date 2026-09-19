"""Mapping Crossref records onto bibcheck's types. Pure functions, no HTTP."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from bibcheck.crossref_parse import parse_work, work_kind
from bibcheck.models import EntryKind
from bibcheck.normalize import PageRange

Loader = Callable[[str], dict[str, Any]]


def test_parses_a_conference_paper(crossref_fixture: Loader) -> None:
    record = parse_work(crossref_fixture("work_resnet")["message"])
    metadata = record.metadata
    assert metadata.title == "Deep Residual Learning for Image Recognition"
    assert metadata.doi == "10.1109/cvpr.2016.90"
    assert metadata.year == 2016
    assert metadata.pages == PageRange("770", "778")
    assert metadata.kind is EntryKind.CONFERENCE_PAPER
    assert [name.family for name in metadata.authors] == ["He", "Zhang", "Ren", "Sun"]
    assert metadata.venue is not None and "Pattern Recognition" in metadata.venue
    assert not record.has_lifecycle_flag


def test_parses_a_journal_article(crossref_fixture: Loader) -> None:
    metadata = parse_work(crossref_fixture("work_year_drift")["message"]).metadata
    assert metadata.year == 2019
    assert metadata.volume == "12"
    assert metadata.issue == "3"
    assert metadata.pages == PageRange("45", "67")
    assert metadata.venue == "Journal of Reproducible Findings"
    assert metadata.kind is EntryKind.JOURNAL_ARTICLE


class TestLifecycle:
    def test_a_retracted_article_is_flagged(self, crossref_fixture: Loader) -> None:
        record = parse_work(crossref_fixture("work_retracted")["message"])
        assert record.retracted
        assert record.has_lifecycle_flag
        assert "retraction" in record.update_notices

    def test_the_notice_itself_is_also_flagged(self, crossref_fixture: Loader) -> None:
        """Crossref records the relation on whichever side deposited it.

        ``updated-by`` sits on the retracted article, ``update-to`` on the
        notice. A citation can point at either, so both must warn.
        """
        record = parse_work(crossref_fixture("work_retraction_notice")["message"])
        assert record.retracted

    def test_an_expression_of_concern_is_distinct_from_a_retraction(
        self, crossref_fixture: Loader
    ) -> None:
        record = parse_work(crossref_fixture("work_concern")["message"])
        assert record.expression_of_concern
        assert not record.retracted
        assert record.has_lifecycle_flag

    def test_a_clean_record_carries_no_flags(self, crossref_fixture: Loader) -> None:
        record = parse_work(crossref_fixture("work_resnet")["message"])
        assert not record.retracted
        assert not record.withdrawn
        assert not record.expression_of_concern
        assert record.update_notices == ()

    @pytest.mark.parametrize(
        ("update_type", "attribute"),
        [
            ("retraction", "retracted"),
            ("partial_retraction", "retracted"),
            ("withdrawal", "withdrawn"),
            ("removal", "withdrawn"),
            ("expression_of_concern", "expression_of_concern"),
        ],
    )
    def test_every_update_type_maps_to_a_flag(
        self, update_type: str, attribute: str
    ) -> None:
        record = parse_work(
            {"DOI": "10.1/x", "updated-by": [{"type": update_type, "DOI": "10.1/n"}]}
        )
        assert getattr(record, attribute) is True

    def test_a_correction_is_not_a_retraction(self) -> None:
        """An erratum is normal scholarly hygiene, not a reason to panic."""
        record = parse_work(
            {"DOI": "10.1/x", "updated-by": [{"type": "correction", "DOI": "10.1/n"}]}
        )
        assert not record.has_lifecycle_flag
        assert record.update_notices == ("correction",)

    def test_is_retracted_by_relation_is_honoured(self) -> None:
        record = parse_work(
            {
                "DOI": "10.1000/x",
                "relation": {"is-retracted-by": [{"id": "10.1000/notice", "id-type": "doi"}]},
            }
        )
        assert record.retracted

    def test_a_retraction_survives_a_malformed_doi_inside_the_relation(self) -> None:
        """The relation existing is the finding; its DOI parsing is not.

        A publisher depositing a malformed DOI must not make a retraction
        invisible to the one person who needs to see it.
        """
        record = parse_work(
            {"DOI": "10.1000/x", "relation": {"is-retracted-by": [{"id": "not-a-doi"}]}}
        )
        assert record.retracted


class TestPreprints:
    def test_a_preprint_is_recognised_and_links_to_its_published_version(
        self, crossref_fixture: Loader
    ) -> None:
        record = parse_work(crossref_fixture("work_preprint")["message"])
        assert record.metadata.kind is EntryKind.PREPRINT
        assert record.metadata.arxiv_id == "2004.05150"
        assert record.published_version_doi == "10.1000/published.2021"

    def test_the_published_version_is_not_itself_a_preprint(
        self, crossref_fixture: Loader
    ) -> None:
        record = parse_work(crossref_fixture("work_published_version")["message"])
        assert record.metadata.kind is EntryKind.CONFERENCE_PAPER
        assert record.published_version_doi is None

    def test_posted_content_that_is_not_a_preprint_is_not_called_one(self) -> None:
        record = parse_work({"DOI": "10.1/x", "type": "posted-content", "subtype": "comment"})
        assert record.metadata.kind is EntryKind.OTHER


class TestDefensiveParsing:
    """Crossref records are heterogeneous; a surprise must never raise."""

    def test_an_empty_message_parses_to_all_none(self) -> None:
        metadata = parse_work({}).metadata
        assert metadata.title is None
        assert metadata.year is None
        assert metadata.authors == ()
        assert metadata.doi is None

    @pytest.mark.parametrize(
        "message",
        [
            {"title": "a bare string, not a list"},
            {"title": []},
            {"title": [None, "Real Title"]},
            {"author": "not a list"},
            {"author": [{"no": "names"}]},
            {"issued": {"date-parts": [[]]}},
            {"issued": {"date-parts": "nonsense"}},
            {"issued": {}},
            {"relation": "not a mapping"},
            {"updated-by": "not a list"},
            {"page": "not a page range"},
            {"volume": None},
        ],
    )
    def test_odd_shapes_do_not_raise(self, message: dict[str, Any]) -> None:
        parse_work(message)

    def test_a_bare_string_title_is_still_read(self) -> None:
        assert parse_work({"title": "A Bare String"}).metadata.title == "A Bare String"

    def test_a_null_in_a_title_list_is_skipped(self) -> None:
        assert parse_work({"title": [None, "Real Title"]}).metadata.title == "Real Title"

    def test_an_organisation_author_is_kept_whole(self) -> None:
        metadata = parse_work({"author": [{"name": "The Editors of The Lancet"}]}).metadata
        assert [name.display for name in metadata.authors] == ["The Editors of The Lancet"]

    def test_a_partial_date_still_yields_a_year(self) -> None:
        assert parse_work({"issued": {"date-parts": [[2016]]}}).metadata.year == 2016

    def test_an_implausible_year_is_rejected(self) -> None:
        assert parse_work({"issued": {"date-parts": [[99]]}}).metadata.year is None

    def test_the_date_fields_are_tried_in_order(self) -> None:
        message = {"issued": {}, "published-print": {"date-parts": [[2015, 3]]}}
        assert parse_work(message).metadata.year == 2015

    def test_an_unparseable_page_range_is_none_not_a_guess(self) -> None:
        """"Cannot compare" must stay distinguishable from a real value."""
        assert parse_work({"page": "to appear"}).metadata.pages is None


@pytest.mark.parametrize(
    ("crossref_type", "expected"),
    [
        ("journal-article", EntryKind.JOURNAL_ARTICLE),
        ("proceedings-article", EntryKind.CONFERENCE_PAPER),
        ("book", EntryKind.BOOK),
        ("book-chapter", EntryKind.CHAPTER),
        ("dissertation", EntryKind.THESIS),
        ("report", EntryKind.REPORT),
        ("posted-content", EntryKind.PREPRINT),
        ("something-new-crossref-invented", EntryKind.OTHER),
        ("", EntryKind.OTHER),
    ],
)
def test_work_kind_mapping(crossref_type: str, expected: EntryKind) -> None:
    assert work_kind({"type": crossref_type}) is expected
