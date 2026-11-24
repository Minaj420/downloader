"""Pure intended text-tag projection (PRODUCT_SPEC §14 M6.5 text tags)."""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.analysis import SourceMetadata
from app.services.text_tags import TextTags, intended_text_tags


def _full() -> SourceMetadata:
    return SourceMetadata(
        track="Song",
        artists=["A & B", "C"],
        album="Album",
        album_artists=["Various Artists"],
        track_number=3,
        disc_number=2,
        release_year=2019,
        release_date="20200131",
    )


def test_full_mapping_and_track_precedence_over_title() -> None:
    tags = intended_text_tags(_full(), source_title="Video Title (Official)")
    assert tags == TextTags(
        title="Song",
        artists=("A & B", "C"),
        album="Album",
        album_artists=("Various Artists",),
        track_number=3,
        disc_number=2,
        date="2020-01-31",
    )


def test_title_falls_back_to_trimmed_source_title() -> None:
    meta = SourceMetadata(artists=["A"])
    tags = intended_text_tags(meta, source_title="  Some Title \n")
    assert tags is not None
    assert tags.title == "Some Title"
    assert tags.artists == ("A",)


def test_title_is_not_truncated_or_path_sanitized() -> None:
    long_title = "a/b:c*" + "x" * 500
    tags = intended_text_tags(None, source_title=long_title)
    assert tags == TextTags(title=long_title)


def test_multivalues_immutable_unsplit_and_not_deduplicated() -> None:
    meta = SourceMetadata(
        artists=["A, B", "A, B", "C"], album_artists=["X", "X"], track="T"
    )
    snapshot = meta.model_copy(deep=True)
    tags = intended_text_tags(meta, source_title=None)
    assert tags is not None
    assert tags.artists == ("A, B", "A, B", "C")
    assert tags.album_artists == ("X", "X")
    assert isinstance(tags.artists, tuple)
    assert meta == snapshot
    meta.artists.append("late")
    assert tags.artists == ("A, B", "A, B", "C")
    with pytest.raises(dataclasses.FrozenInstanceError):
        tags.title = "other"  # type: ignore[misc]


def test_year_only_renders_as_text() -> None:
    tags = intended_text_tags(SourceMetadata(release_year=2018), source_title=None)
    assert tags == TextTags(date="2018")


def test_full_date_preferred_over_year() -> None:
    meta = SourceMetadata(release_year=1999, release_date="20240229")
    tags = intended_text_tags(meta, source_title=None)
    assert tags is not None
    assert tags.date == "2024-02-29"


def test_unusable_date_falls_back_to_year() -> None:
    meta = SourceMetadata(release_year=2001, release_date="2001-05")
    tags = intended_text_tags(meta, source_title=None)
    assert tags is not None
    assert tags.date == "2001"


def test_numbers_only_from_metadata() -> None:
    tags = intended_text_tags(SourceMetadata(track_number=7), source_title="T")
    assert tags == TextTags(title="T", track_number=7)


@pytest.mark.parametrize("metadata", [None, SourceMetadata()])
def test_legacy_or_empty_metadata_uses_title_only(
    metadata: SourceMetadata | None,
) -> None:
    assert intended_text_tags(metadata, source_title="T") == TextTags(title="T")


@pytest.mark.parametrize("title", [None, "", "   \t\n"])
@pytest.mark.parametrize("metadata", [None, SourceMetadata()])
def test_nothing_to_write_returns_none(
    metadata: SourceMetadata | None, title: str | None
) -> None:
    assert intended_text_tags(metadata, source_title=title) is None


def test_invalid_text_isolated_field_by_field() -> None:
    meta = SourceMetadata(
        track="bad\x00track",
        artists=["ok", "n\x00ul", "\ud800", "fine"],
        album="\udfff",
        album_artists=["al\x00"],
        track_number=4,
        release_year=2010,
    )
    tags = intended_text_tags(meta, source_title="Good Title")
    assert tags == TextTags(
        title="Good Title",
        artists=("ok", "fine"),
        album=None,
        album_artists=(),
        track_number=4,
        date="2010",
    )


def test_unusable_track_and_title_keep_other_fields() -> None:
    meta = SourceMetadata(track="\x00", album="Album")
    tags = intended_text_tags(meta, source_title="bad\x00")
    assert tags == TextTags(album="Album")


def test_unencodable_only_text_returns_none() -> None:
    meta = SourceMetadata(track="\ud800", artists=["\x00"])
    assert intended_text_tags(meta, source_title="\udc00") is None


def test_non_ascii_text_kept_verbatim() -> None:
    meta = SourceMetadata(track="Şarkı ✓", artists=["Éluard"])
    tags = intended_text_tags(meta, source_title="x")
    assert tags == TextTags(title="Şarkı ✓", artists=("Éluard",))
