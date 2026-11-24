"""Pure intended audio text-tag projection (PRODUCT_SPEC §14, ARCHITECTURE §14).

Maps normalized ``SourceMetadata`` plus the source title to a frozen,
format-independent value. No tag-format code, no file access and no
persistence live here; a later writer decides how each field is stored.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.analysis import SourceMetadata


@dataclass(frozen=True, slots=True)
class TextTags:
    """Intended text tags; multi-values keep source order, unchanged."""

    title: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    album_artists: tuple[str, ...] = ()
    track_number: int | None = None
    disc_number: int | None = None
    date: str | None = None


def _usable(value: object) -> str | None:
    """The text itself when it has no NUL and encodes as UTF-8, else ``None``.

    Never truncated or otherwise rewritten.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _usable_names(values: list[str]) -> tuple[str, ...]:
    """Usable names in order; no splitting, joining or deduplication."""
    names: list[str] = []
    for value in values:
        name = _usable(value)
        if name is not None:
            names.append(name)
    return tuple(names)


def _number(value: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _date_text(metadata: SourceMetadata) -> str | None:
    """Full release date as ``YYYY-MM-DD``, else the year as text."""
    raw = metadata.release_date
    if raw is not None and len(raw) == 8 and raw.isascii() and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    year = metadata.release_year
    if isinstance(year, int) and not isinstance(year, bool):
        return str(year)
    return None


def intended_text_tags(
    metadata: SourceMetadata | None, *, source_title: str | None
) -> TextTags | None:
    """Project normalized source data to intended text tags.

    The track name wins over the title; the trimmed non-blank source title is
    the fallback, also when the track text is unusable. Each field is
    validated on its own, so one bad field never drops the others. Returns
    ``None`` when there is nothing to write. The input is never mutated.
    """
    title: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    album_artists: tuple[str, ...] = ()
    track_number: int | None = None
    disc_number: int | None = None
    date: str | None = None

    if metadata is not None:
        title = _usable(metadata.track)
        artists = _usable_names(metadata.artists)
        album = _usable(metadata.album)
        album_artists = _usable_names(metadata.album_artists)
        track_number = _number(metadata.track_number)
        disc_number = _number(metadata.disc_number)
        date = _date_text(metadata)

    if title is None and isinstance(source_title, str):
        title = _usable(source_title.strip())

    tags = TextTags(
        title=title,
        artists=artists,
        album=album,
        album_artists=album_artists,
        track_number=track_number,
        disc_number=disc_number,
        date=date,
    )
    if tags == TextTags():
        return None
    return tags
