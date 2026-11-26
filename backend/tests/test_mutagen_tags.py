"""Mutagen text-tag adapter on real, locally synthesised audio.

Media comes from the FFmpeg smoke helpers (argv lists, timeouts, no network).
Tests skip only when the tools or a required encoder are missing.
"""

from __future__ import annotations

import base64
import errno
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple, cast

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    CHAP,
    COMM,
    CTOC,
    ID3,
    IPLS,
    RVAD,
    TALB,
    TDAT,
    TIME,
    TIT2,
    TORY,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TRDA,
    TSIZ,
    TXXX,
    TYER,
    ID3NoHeaderError,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4Info
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from app.integrations import mutagen_tags
from app.integrations.mutagen_tags import TagError, TagErrorKind, TagOutcome, write_text_tags
from app.services.text_tags import TextTags
from tests.test_ffmpeg_real_smoke import (
    _EVEN_SIZE,
    _cover_jpeg,
    _encode,
    _file_digest,
    _packet_hash,
    _probe,
    _raw_video_input,
    _require,
    _stream_types,
    _write_raw_frames,
    _write_tone_wav,
    requires_ffmpeg,
)

FULL = TextTags(
    title="Şarkı ✓",
    artists=("A & B", "C, D", "C, D"),
    album="Albüm",
    album_artists=("Various", "Other"),
    track_number=3,
    disc_number=2,
    date="2020-01-31",
)


class _Format(NamedTuple):
    name: str
    suffix: str
    encoder: str
    arguments: list[str]
    opener: Callable[..., Any]


_FORMATS = [
    _Format("mp3", ".mp3", "libmp3lame",
            ["-c:a", "libmp3lame", "-id3v2_version", "3", "-f", "mp3"], MP3),
    _Format("m4a", ".m4a", "aac",
            ["-c:a", "aac", "-movflags", "+faststart", "-f", "ipod"], MP4),
    _Format("m4a-mdat-first", ".m4a", "aac", ["-c:a", "aac", "-f", "ipod"], MP4),
    _Format("m4a-alac", ".m4a", "alac", ["-c:a", "alac", "-f", "ipod"], MP4),
    _Format("flac", ".flac", "flac", ["-c:a", "flac", "-f", "flac"], FLAC),
    _Format("vorbis", ".ogg", "libvorbis", ["-c:a", "libvorbis", "-f", "ogg"], OggVorbis),
    _Format("opus", ".opus", "libopus", ["-c:a", "libopus", "-f", "opus"], OggOpus),
    _Format("opus-in-ogg", ".ogg", "libopus", ["-c:a", "libopus", "-f", "ogg"], OggOpus),
]
_BY_NAME = {fmt.name: fmt for fmt in _FORMATS}
_CORE = [_BY_NAME[name] for name in ("mp3", "m4a", "flac", "vorbis", "opus")]

all_formats = pytest.mark.parametrize("fmt", _FORMATS, ids=[f.name for f in _FORMATS])
core_formats = pytest.mark.parametrize("fmt", _CORE, ids=[f.name for f in _CORE])


def _make(tmp_path: Path, fmt: _Format) -> Path:
    _require((fmt.encoder,))
    tone = _write_tone_wav(tmp_path / "tone.wav")
    return _encode(["-i", str(tone), *fmt.arguments], tmp_path / f"candidate{fmt.suffix}")


def _write(path: Path, tags: TextTags, suffix: str | None = None) -> TagOutcome:
    with path.open("r+b") as handle:
        outcome = write_text_tags(
            handle, suffix=path.suffix if suffix is None else suffix, tags=tags
        )
        assert not handle.closed
    return outcome


def _native(audio: Any) -> dict[str, Any]:
    """Managed fields as the format natively stores them."""
    tags = audio.tags
    if isinstance(audio, MP3):
        names = ("TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TPOS", "TDRC")
        return {
            n: [str(t) for t in tags[n].text] for n in names if tags is not None and n in tags
        }
    if isinstance(audio, MP4):
        keys = ("\xa9nam", "\xa9ART", "\xa9alb", "aART", "trkn", "disk", "\xa9day")
        return {k: list(tags[k]) for k in keys if tags is not None and k in tags}
    keys = ("title", "artist", "album", "albumartist", "tracknumber", "discnumber", "date")
    return {k: tags[k] for k in keys if tags is not None and k in tags}


def _expected_full(audio: Any) -> dict[str, Any]:
    texts = [["Şarkı ✓"], ["A & B", "C, D", "C, D"], ["Albüm"], ["Various", "Other"]]
    date = ["2020-01-31"]
    if isinstance(audio, MP3):
        keys = ("TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TPOS", "TDRC")
        return dict(zip(keys, [*texts, ["3"], ["2"], date], strict=True))
    if isinstance(audio, MP4):
        keys = ("\xa9nam", "\xa9ART", "\xa9alb", "aART", "trkn", "disk", "\xa9day")
        return dict(zip(keys, [*texts, [(3, 0)], [(2, 0)], date], strict=True))
    keys = ("title", "artist", "album", "albumartist", "tracknumber", "discnumber", "date")
    return dict(zip(keys, [*texts, ["3"], ["2"], date], strict=True))


# --- writing ----------------------------------------------------------------


@requires_ffmpeg
@all_formats
def test_full_tags_are_native_and_audio_is_preserved(tmp_path: Path, fmt: _Format) -> None:
    path = _make(tmp_path, fmt)
    packets = _packet_hash(path, "0:a:0")
    streams = _stream_types(_probe(path))

    assert _write(path, FULL) is TagOutcome.WRITTEN

    audio = fmt.opener(path)
    assert _native(audio) == _expected_full(audio)
    if fmt.name == "mp3":
        assert audio.tags.version == (2, 4, 0)
    assert _packet_hash(path, "0:a:0") == packets
    assert _stream_types(_probe(path)) == streams


@requires_ffmpeg
@all_formats
def test_large_values_grow_the_file_without_touching_audio(
    tmp_path: Path, fmt: _Format
) -> None:
    path = _make(tmp_path, fmt)
    packets = _packet_hash(path, "0:a:0")
    title = "x" * 90_000

    assert _write(path, TextTags(title=title)) is TagOutcome.WRITTEN
    assert next(iter(_native(fmt.opener(path)).values())) == [title]
    assert _packet_hash(path, "0:a:0") == packets
    assert _write(path, TextTags(title="short")) is TagOutcome.WRITTEN
    assert next(iter(_native(fmt.opener(path)).values())) == ["short"]
    assert _packet_hash(path, "0:a:0") == packets


@requires_ffmpeg
@core_formats
def test_year_only_date_and_in_memory_candidate(tmp_path: Path, fmt: _Format) -> None:
    buffer = io.BytesIO(_make(tmp_path, fmt).read_bytes())

    outcome = write_text_tags(buffer, suffix=fmt.suffix.upper(), tags=TextTags(date="2018"))

    assert outcome is TagOutcome.WRITTEN
    buffer.seek(0)
    assert ["2018"] in _native(fmt.opener(buffer)).values()


# --- preservation -----------------------------------------------------------


def _seed(path: Path, fmt: _Format, cover: bytes) -> None:
    audio = fmt.opener(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if isinstance(audio, MP3):
        for frame in (
            TPE1(encoding=3, text=["Old Artist"]), TALB(encoding=3, text=["Old Album"]),
            TPE2(encoding=3, text=["Old AA"]), TRCK(encoding=3, text=["5/12"]),
            TPOS(encoding=3, text=["1/4"]), TYER(encoding=3, text=["1999"]),
            COMM(encoding=3, lang="eng", desc="", text=["keep me"]),
            TXXX(encoding=3, desc="custom", text=["value"]),
            APIC(encoding=3, mime="image/jpeg", type=3, desc="front", data=cover),
        ):
            tags.add(frame)
        tags.update_to_v23()
        audio.save(v2_version=3)
        assert ID3(path, translate=False).version == (2, 3, 0)
    elif isinstance(audio, MP4):
        tags["\xa9ART"], tags["\xa9alb"], tags["aART"] = ["Old Artist"], ["Old Album"], ["Old AA"]
        tags["trkn"], tags["disk"], tags["\xa9day"] = [(5, 12)], [(1, 4)], ["1999"]
        tags["\xa9cmt"] = ["keep me"]
        tags["----:com.apple.iTunes:custom"] = [b"value"]
        tags["covr"] = [MP4Cover(cover, MP4Cover.FORMAT_JPEG)]
        audio.save()
    else:
        tags["artist"], tags["album"], tags["albumartist"] = ["Old Artist"], ["Old Album"], "Old AA"
        tags["tracknumber"], tags["discnumber"], tags["date"] = "5/12", "1/4", "1999"
        tags["tracktotal"], tags["comment"], tags["custom"] = "12", "keep me", "value"
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/jpeg", cover
        if isinstance(audio, FLAC):
            audio.add_picture(picture)
        else:
            tags["metadata_block_picture"] = base64.b64encode(picture.write()).decode()
        audio.save()


def _unmanaged(audio: Any) -> list[Any]:
    tags = audio.tags
    if isinstance(audio, MP3):
        return [tags["COMM::eng"].text, tags["TXXX:custom"].text,
                [(p.mime, p.type, p.desc, p.data) for p in tags.getall("APIC")]]
    if isinstance(audio, MP4):
        return [tags["\xa9cmt"], tags["----:com.apple.iTunes:custom"], tags["covr"]]
    extra = [p.write() for p in audio.pictures] if isinstance(audio, FLAC) else (
        tags["metadata_block_picture"])
    return [tags["comment"], tags["custom"], tags["tracktotal"], extra]


@requires_ffmpeg
@core_formats
def test_only_supplied_fields_change(tmp_path: Path, fmt: _Format) -> None:
    path = _make(tmp_path, fmt)
    _seed(path, fmt, _cover_jpeg(tmp_path).read_bytes())
    before = fmt.opener(path)
    native, unmanaged = _native(before), _unmanaged(before)
    packets = _packet_hash(path, "0:a:0")

    assert _write(path, TextTags(title="New")) is TagOutcome.WRITTEN

    after = fmt.opener(path)
    result = _native(after)
    title_key = {MP3: "TIT2", MP4: "\xa9nam"}.get(type(after), "title")
    assert result.pop(title_key) == ["New"]
    native.pop(title_key, None)
    assert len(native) == 6 and result == native
    assert _unmanaged(after) == unmanaged
    assert _packet_hash(path, "0:a:0") == packets


@requires_ffmpeg
@core_formats
def test_known_totals_stay_when_numbers_change(tmp_path: Path, fmt: _Format) -> None:
    path = _make(tmp_path, fmt)
    _seed(path, fmt, _cover_jpeg(tmp_path).read_bytes())

    assert _write(path, FULL) is TagOutcome.WRITTEN

    audio = fmt.opener(path)
    expected = _expected_full(audio)
    if isinstance(audio, MP4):
        expected |= {"trkn": [(3, 12)], "disk": [(2, 4)]}
    elif isinstance(audio, MP3):
        expected |= {"TRCK": ["3/12"], "TPOS": ["2/4"]}
    else:
        expected |= {"tracknumber": ["3/12"], "discnumber": ["2/4"]}
    assert _native(audio) == expected
    assert _unmanaged(audio)[-1]  # artwork


def _assert_refused(path: Path, tags: TextTags) -> None:
    before = _file_digest(path)
    with pytest.raises(TagError) as caught:
        _write(path, tags)
    assert caught.value.kind is TagErrorKind.WRITE
    assert _file_digest(path) == before


@requires_ffmpeg
@pytest.mark.parametrize("year", [b"1999", b"9", b""], ids=["full", "short-year", "no-year"])
@pytest.mark.parametrize("with_v2", [True, False], ids=["v1-and-v2", "v1-only"])
def test_mp3_with_id3v1_is_refused_and_stays_byte_identical(
    tmp_path: Path, year: bytes, with_v2: bool
) -> None:
    # v1 and v2 disagree on the artist and the comment exists only in v1.
    path = _make(tmp_path, _BY_NAME["mp3"])
    if with_v2:
        audio: Any = MP3(path)
        audio.tags.add(TPE1(encoding=3, text=["V2 Artist"]))
        audio.save()
    else:
        ID3(path).delete(path)
    v1 = (b"TAG" + b"Old".ljust(30, b"\0") + b"V1 Artist".ljust(30, b"\0")
          + b"Old Album".ljust(30, b"\0") + year + b"v1 only".ljust(30, b"\0") + b"\x11")
    path.write_bytes(path.read_bytes() + v1)

    _assert_refused(path, TextTags(title="New"))
    _assert_refused(path, FULL)
    assert path.read_bytes().endswith(v1)


def _save_v23(path: Path, frames: list[Any], unknown: list[bytes] | None = None) -> None:
    """Store frames verbatim in ID3v2.3, without Mutagen's own conversion."""
    audio: Any = MP3(path)
    audio.tags.clear()
    for frame in frames:
        audio.tags.add(frame)
    if unknown:
        audio.tags.unknown_frames = unknown
        audio.tags._unknown_v2_version = 3
    audio.save(v2_version=3)


def _chapter(*sub_frames: Any, element_id: str = "c1") -> CHAP:
    return CHAP(element_id=element_id, start_time=0, end_time=500, sub_frames=list(sub_frames))


def _contents(*sub_frames: Any) -> CTOC:
    return CTOC(element_id="toc", flags=3, child_element_ids=["c1"], sub_frames=list(sub_frames))


_WRAPPERS: dict[str, Callable[[Any], Any]] = {
    "direct": lambda frame: frame,
    "chap": lambda frame: _chapter(frame),
    "ctoc": lambda frame: _contents(frame),
    "chap-in-ctoc": lambda frame: _contents(_chapter(frame)),
}
_LOST_FRAMES: dict[str, Callable[[], Any]] = {
    "tsiz": lambda: TSIZ(encoding=0, text=["123"]),
    "trda": lambda: TRDA(encoding=0, text=["summer"]),
    "rvad": lambda: RVAD(adjustments=[1, 2]),
    "tdat-alone": lambda: TDAT(encoding=0, text=["3101"]),
    "time-alone": lambda: TIME(encoding=0, text=["1230"]),
    "bad-year": lambda: TYER(encoding=0, text=["99"]),
    "bad-original-year": lambda: TORY(encoding=0, text=["unknown"]),
}


@requires_ffmpeg
@pytest.mark.parametrize("place", _WRAPPERS)
@pytest.mark.parametrize("lost", _LOST_FRAMES)
def test_mp3_refuses_upgrade_that_would_lose_frames(
    tmp_path: Path, place: str, lost: str
) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    frame = _LOST_FRAMES[lost]()
    _save_v23(path, [_WRAPPERS[place](frame)])
    stored = ID3(path, translate=False)
    assert stored.version == (2, 3, 0)
    assert type(frame).__name__ in repr(dict(stored.items()))

    _assert_refused(path, TextTags(title="New"))


@requires_ffmpeg
@pytest.mark.parametrize("nested", [False, True])
def test_mp3_refuses_to_drop_unknown_v23_frames(tmp_path: Path, nested: bool) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    unknown = [b"XABC\x00\x00\x00\x04\x00\x00data"]
    if nested:
        chapter = _chapter(TIT2(encoding=0, text=["one"]))
        chapter.sub_frames.unknown_frames = unknown  # type: ignore[attr-defined]
        chapter.sub_frames._unknown_v2_version = 3  # type: ignore[attr-defined]
        # Written as v2.4 data would drop them, so keep the v2.3 sub-tag as is.
        chapter._get_v23_frame = lambda **kwargs: chapter  # type: ignore[method-assign]
        _save_v23(path, [chapter])
    else:
        _save_v23(path, [], unknown)
    assert b"XABC" in path.read_bytes()

    _assert_refused(path, TextTags(title="New"))


@requires_ffmpeg
def test_mp3_id3v22_is_refused(tmp_path: Path) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    ID3(path).delete(path)
    frame = b"TT2\x00\x00\x04\x00old"
    path.write_bytes(b"ID3\x02\x00\x00\x00\x00\x00" + bytes([len(frame)]) + frame
                     + path.read_bytes())
    assert ID3(path).version == (2, 2, 0)

    _assert_refused(path, TextTags(title="New"))


@requires_ffmpeg
@pytest.mark.parametrize("version", [3, 4])
def test_mp3_chapters_survive(tmp_path: Path, version: int) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    packets = _packet_hash(path, "0:a:0")
    frames: list[Any] = [
        _chapter(TPE1(encoding=3, text=["a much longer nested artist"]),
                 TIT2(encoding=3, text=["one"]), TYER(encoding=0, text=["1999"])),
        _chapter(TIT2(encoding=3, text=["two"]), element_id="c2"),
        _contents(TIT2(encoding=3, text=["all"]), TALB(encoding=3, text=["nested album"])),
        TYER(encoding=0, text=["2001"]), TDAT(encoding=0, text=["3101"]),
        TIME(encoding=0, text=["1230"]), TORY(encoding=0, text=["1980"]),
        IPLS(encoding=3, people=[["mixer", "M"]]),
    ]
    _save_v23(path, frames)
    if version == 4:
        ID3(path).save(path)
        assert ID3(path).version == (2, 4, 0)

    assert _write(path, TextTags(title="New")) is TagOutcome.WRITTEN

    tags = ID3(path, translate=False)
    assert tags.version == (2, 4, 0)
    assert tags["TIT2"].text == ["New"]
    one, two, toc = tags["CHAP:c1"], tags["CHAP:c2"], tags["CTOC:toc"]
    assert (one.start_time, one.end_time) == (0, 500)
    assert one.sub_frames["TPE1"].text == ["a much longer nested artist"]
    assert one.sub_frames["TIT2"].text == ["one"]
    assert str(one.sub_frames["TDRC"]) == "1999"
    assert two.sub_frames["TIT2"].text == ["two"]
    assert toc.child_element_ids == ["c1"]
    assert toc.sub_frames["TIT2"].text == ["all"]
    assert toc.sub_frames["TALB"].text == ["nested album"]
    assert str(tags["TDRC"]) == "2001-01-31 12:30:00"
    assert str(tags["TDOR"]) == "1980"
    assert tags["TIPL"].people == [["mixer", "M"]]
    assert _packet_hash(path, "0:a:0") == packets


_UNSET = 0xFFFFFFFF


@requires_ffmpeg
@pytest.mark.parametrize(("start", "end", "nested"), [
    (100, 900, False), (100, _UNSET, False), (_UNSET, 900, False), (100, 900, True),
], ids=["both", "start-only", "end-only", "inside-ctoc"])
def test_mp3_chapter_byte_offsets_are_refused(
    tmp_path: Path, start: int, end: int, nested: bool
) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    chapter = CHAP(element_id="c1", start_time=0, end_time=500, start_offset=start,
                   end_offset=end, sub_frames=[TIT2(encoding=3, text=["one"])])
    _save_v23(path, [_contents(chapter) if nested else chapter])
    stored = ID3(path, translate=False)
    found = stored["CTOC:toc"].sub_frames["CHAP:c1"] if nested else stored["CHAP:c1"]
    assert (found.start_offset, found.end_offset) == (start, end)

    _assert_refused(path, TextTags(title="x" * 90_000))


def _frame(name: bytes, data: bytes) -> bytes:
    # Frames stay below 128 bytes, where plain and syncsafe sizes are equal.
    assert len(data) < 128
    return name + len(data).to_bytes(4, "big") + b"\x00\x00" + data


def _with_raw_v24_tag(path: Path, frames: list[bytes]) -> None:
    """Replace the ID3 tag with hand-serialised ID3v2.4 frames Mutagen won't write.

    ID3v2.4, because Mutagen treats trailing zeros in ID3v2.3 as padding and
    would never even parse an empty ``TXXX`` value there.
    """
    _with_raw_tag(path, frames, 4)


def _with_raw_tag(path: Path, frames: list[bytes], major: int) -> None:
    ID3(path).delete(path)
    body = b"".join(frames)
    size = len(body)
    syncsafe = bytes(((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F))
    path.write_bytes(b"ID3" + bytes((major, 0, 0)) + syncsafe + body + path.read_bytes())


# Latin-1, description "note", one empty value: only the descriptor carries data.
_EMPTY_TXXX = _frame(b"TXXX", b"\x00note\x00\x00")
_EMPTY_TIT2 = _frame(b"TIT2", b"\x00\x00")
_TALB = _frame(b"TALB", b"\x00album")


def _raw_chapter(*sub_frames: bytes) -> bytes:
    times = (0).to_bytes(4, "big") + (500).to_bytes(4, "big") + b"\xff" * 8
    return _frame(b"CHAP", b"c1\x00" + times + b"".join(sub_frames))


def _raw_contents(*sub_frames: bytes) -> bytes:
    return _frame(b"CTOC", b"toc\x00\x03\x01c1\x00" + b"".join(sub_frames))


_EMPTY_CASES: dict[str, tuple[list[bytes], Callable[[Any], Any], TextTags]] = {
    "direct-txxx": ([_EMPTY_TXXX, _TALB], lambda t: t["TXXX:note"], TextTags(title="New")),
    "chap-txxx": ([_raw_chapter(_EMPTY_TXXX)],
                  lambda t: t["CHAP:c1"].sub_frames["TXXX:note"], TextTags(title="New")),
    "ctoc-txxx": ([_raw_contents(_EMPTY_TXXX)],
                  lambda t: t["CTOC:toc"].sub_frames["TXXX:note"], TextTags(title="New")),
    # A supplied title replaces only the top-level TIT2, never a nested one.
    "chap-title-replaced-on-top": ([_raw_chapter(_EMPTY_TIT2)],
                                   lambda t: t["CHAP:c1"].sub_frames["TIT2"],
                                   TextTags(title="New")),
    # Replacing the empty top-level title must not hide an unrelated empty frame.
    "title-replaced-plus-txxx": ([_EMPTY_TIT2, _EMPTY_TXXX], lambda t: t["TXXX:note"],
                                 TextTags(title="New")),
    "title-not-supplied": ([_EMPTY_TIT2, _TALB], lambda t: t["TIT2"], TextTags(album="New")),
}


@requires_ffmpeg
@pytest.mark.parametrize("case", _EMPTY_CASES)
def test_mp3_existing_empty_text_frame_is_refused(tmp_path: Path, case: str) -> None:
    frames, locate, tags = _EMPTY_CASES[case]
    path = _make(tmp_path, _BY_NAME["mp3"])
    _with_raw_v24_tag(path, frames)
    frame = locate(ID3(path, translate=False))
    assert str(frame) == ""
    if case.endswith("txxx"):
        assert frame.desc == "note"

    _assert_refused(path, tags)


@requires_ffmpeg
def test_mp3_empty_title_is_replaced_when_title_is_supplied(tmp_path: Path) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    _with_raw_v24_tag(path, [_EMPTY_TIT2, _TALB])
    assert str(ID3(path, translate=False)["TIT2"]) == ""
    packets = _packet_hash(path, "0:a:0")

    assert _write(path, TextTags(title="New")) is TagOutcome.WRITTEN

    tags = ID3(path, translate=False)
    assert tags["TIT2"].text == ["New"]
    assert tags["TALB"].text == ["album"]
    assert _packet_hash(path, "0:a:0") == packets


# ID3v2.3 descriptive TXXX without a value: with and without a value terminator.
_V23_EMPTY_TXXX = {
    "terminated": _frame(b"TXXX", b"\x00note\x00\x00"),
    "bare": _frame(b"TXXX", b"\x00note\x00"),
}
_V23_TXXX = _frame(b"TXXX", b"\x00other\x00value")
_V23_PLACES: dict[str, Callable[[bytes], list[bytes]]] = {
    "direct": lambda frame: [_TALB, frame, _V23_TXXX],
    "chap": lambda frame: [_TALB, _raw_chapter(frame, _V23_TXXX)],
    "ctoc": lambda frame: [_TALB, _raw_contents(frame)],
    "chap-in-ctoc": lambda frame: [_raw_contents(_raw_chapter(frame))],
}


@requires_ffmpeg
@pytest.mark.parametrize("place", _V23_PLACES)
@pytest.mark.parametrize("form", _V23_EMPTY_TXXX)
def test_mp3_v23_empty_descriptive_txxx_is_refused_untouched(
    tmp_path: Path, place: str, form: str
) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    frame = _V23_EMPTY_TXXX[form]
    _with_raw_tag(path, _V23_PLACES[place](frame), 3)
    original = path.read_bytes()
    # Raw evidence: the serialised frame is in the file...
    assert original.startswith(b"ID3\x03") and frame in original
    # ...but Mutagen's normal parse does not see it at all.
    assert "TXXX:note" not in repr(dict(ID3(path, translate=False).items()))

    for tags in (TextTags(title="New"), TextTags(title="x" * 90_000), FULL):
        with pytest.raises(TagError) as caught:
            _write(path, tags)
        assert caught.value.kind is TagErrorKind.WRITE
        assert path.read_bytes() == original


@requires_ffmpeg
def test_mp3_v23_duplicate_txxx_description_is_refused_untouched(tmp_path: Path) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    first, second = _frame(b"TXXX", b"\x00note\x00one"), _frame(b"TXXX", b"\x00note\x00two")
    _with_raw_tag(path, [first, second], 3)
    original = path.read_bytes()
    assert first in original and second in original
    assert len(ID3(path, translate=False).getall("TXXX")) == 1

    _assert_refused(path, TextTags(title="New"))
    assert path.read_bytes() == original


@requires_ffmpeg
@pytest.mark.parametrize("nested", [False, True])
def test_mp3_v23_txxx_with_value_is_preserved(tmp_path: Path, nested: bool) -> None:
    path = _make(tmp_path, _BY_NAME["mp3"])
    frames = [_TALB, _raw_chapter(_V23_TXXX)] if nested else [_TALB, _V23_TXXX]
    _with_raw_tag(path, frames, 3)
    assert _V23_TXXX in path.read_bytes()
    packets = _packet_hash(path, "0:a:0")

    assert _write(path, TextTags(title="New")) is TagOutcome.WRITTEN

    written = path.read_bytes()
    assert written.startswith(b"ID3\x04")
    # Raw evidence: description and value bytes are still serialised.
    assert b"\x00other\x00value" in written
    tags = ID3(path, translate=False)
    container = tags["CHAP:c1"].sub_frames if nested else tags
    assert container["TXXX:other"].text == ["value"]
    assert tags["TIT2"].text == ["New"]
    assert tags["TALB"].text == ["album"]
    assert _packet_hash(path, "0:a:0") == packets


@requires_ffmpeg
def test_vorbis_refuses_to_rewrite_undecodable_comment(tmp_path: Path) -> None:
    path = _make(tmp_path, _BY_NAME["flac"])
    audio = FLAC(path)
    audio["comment"] = ["MARKER"]
    audio.save()
    path.write_bytes(path.read_bytes().replace(b"MARKER", b"MARK\xff\xfe"))
    before = _file_digest(path)

    with pytest.raises(TagError) as caught:
        _write(path, TextTags(title="New"))

    assert caught.value.kind is TagErrorKind.WRITE
    assert _file_digest(path) == before


# --- no-op and unsupported --------------------------------------------------


@pytest.mark.parametrize("suffix", [".wav", ".webm", ".mp4", ".mkv", ".aac", "", "mp3"])
def test_unapproved_suffix_never_touches_the_handle(suffix: str) -> None:
    assert write_text_tags(
        cast(BinaryIO, object()), suffix=suffix, tags=FULL
    ) is TagOutcome.UNSUPPORTED


def test_empty_tags_never_touch_the_handle() -> None:
    assert write_text_tags(
        cast(BinaryIO, object()), suffix=".mp3", tags=TextTags()
    ) is TagOutcome.NOTHING_TO_WRITE


class _Foreign(NamedTuple):
    name: str
    encoders: tuple[str, ...]
    arguments: list[str]
    suffix: str
    video: bool = False


_FRAGMENTED = ["-movflags", "frag_keyframe+empty_moov", "-f", "mp4"]

_FOREIGN = [
    _Foreign("wav-as-mp3", (), ["-c:a", "pcm_s16le", "-f", "wav"], ".mp3"),
    _Foreign("webm-as-opus", ("libopus",), ["-c:a", "libopus", "-f", "webm"], ".opus"),
    _Foreign("flac-as-mp3", ("flac",), ["-c:a", "flac", "-f", "flac"], ".mp3"),
    _Foreign("mp3-as-flac", ("libmp3lame",), ["-c:a", "libmp3lame", "-f", "mp3"], ".flac"),
    _Foreign("m4a-as-mp3", ("aac",), ["-c:a", "aac", "-f", "ipod"], ".mp3"),
    _Foreign("adts-as-mp3", ("aac",), ["-c:a", "aac", "-f", "adts"], ".mp3"),
    _Foreign("mp2-as-mp3", ("mp2",), ["-c:a", "mp2", "-f", "mp2"], ".mp3"),
    _Foreign("vorbis-as-opus", ("libvorbis",), ["-c:a", "libvorbis", "-f", "ogg"], ".opus"),
    _Foreign("ogg-flac", ("flac",), ["-c:a", "flac", "-f", "ogg"], ".ogg"),
    _Foreign("mp3-in-mp4", ("libmp3lame",), ["-c:a", "libmp3lame", "-f", "mp4"], ".m4a"),
    _Foreign("fragmented-mp3-in-mp4", ("libmp3lame",),
             ["-c:a", "libmp3lame", *_FRAGMENTED], ".m4a"),
    _Foreign("fragmented-video-as-m4a", ("mpeg4", "aac"),
             ["-c:v", "mpeg4", "-c:a", "aac", *_FRAGMENTED], ".m4a", video=True),
    _Foreign("video-mp4-as-m4a", ("mpeg4", "aac"),
             ["-c:v", "mpeg4", "-c:a", "aac", "-f", "mp4"], ".m4a", video=True),
    _Foreign("theora-vorbis-ogg", ("libtheora", "libvorbis"),
             ["-c:v", "libtheora", "-pix_fmt", "yuv420p", "-c:a", "libvorbis", "-f", "ogg"],
             ".ogg", video=True),
]


@requires_ffmpeg
@pytest.mark.parametrize("case", _FOREIGN, ids=[c.name for c in _FOREIGN])
def test_misleading_or_unsupported_content_is_unchanged(
    tmp_path: Path, case: _Foreign
) -> None:
    _require(case.encoders)
    tone = _write_tone_wav(tmp_path / "tone.wav")
    inputs = ["-i", str(tone)]
    if case.video:
        frames = _write_raw_frames(tmp_path / "frames.rgb", _EVEN_SIZE)
        inputs = [*_raw_video_input(frames, _EVEN_SIZE), *inputs,
                  "-map", "0:v:0", "-map", "1:a:0"]
    path = _encode([*inputs, *case.arguments], tmp_path / f"case{case.suffix}")
    before = _file_digest(path)

    assert _write(path, FULL) is TagOutcome.UNSUPPORTED
    assert _file_digest(path) == before
    if "fragmented" in case.name:
        assert b"moof" in path.read_bytes()


@requires_ffmpeg
@pytest.mark.parametrize("codec", ["aac", "alac"])
def test_fragmented_supported_audio_is_a_write_error(tmp_path: Path, codec: str) -> None:
    _require((codec,))
    tone = _write_tone_wav(tmp_path / "tone.wav")
    path = _encode(["-i", str(tone), "-c:a", codec, *_FRAGMENTED], tmp_path / "case.m4a")
    assert b"moof" in path.read_bytes()
    packets = _packet_hash(path, "0:a:0")

    _assert_refused(path, FULL)
    assert _packet_hash(path, "0:a:0") == packets


@requires_ffmpeg
@pytest.mark.parametrize(("codec", "expected"), [
    ("aac", "mp4a.40.2"), ("alac", "alac"), ("libmp3lame", "mp4a.6B"),
])
def test_mutagen_reports_the_exact_mp4_codec_strings(
    tmp_path: Path, codec: str, expected: str
) -> None:
    _require((codec,))
    tone = _write_tone_wav(tmp_path / "tone.wav")
    path = _encode(["-i", str(tone), "-c:a", codec, "-f", "mp4"], tmp_path / "case.m4a")
    audio: Any = MP4(path)
    assert audio.info.codec == expected
    assert (expected in mutagen_tags._MP4_AUDIO_CODECS) is (codec != "libmp3lame")


@requires_ffmpeg
@pytest.mark.parametrize("reported", ["mp4a.40.34", "mp4a.40"])
@pytest.mark.parametrize("fragmented", [False, True])
def test_mp3_or_unspecified_mpeg4_audio_codec_is_unsupported_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported: str, fragmented: bool
) -> None:
    # FFmpeg muxes MP3 in MP4 as ``mp4a.6B``, so a real AAC candidate is used and
    # only the codec Mutagen reports for it is replaced; everything else is real.
    _require(("aac",))
    tone = _write_tone_wav(tmp_path / "tone.wav")
    container = _FRAGMENTED if fragmented else ["-movflags", "+faststart", "-f", "ipod"]
    path = _encode(["-i", str(tone), "-c:a", "aac", *container], tmp_path / "case.m4a")
    assert (b"moof" in path.read_bytes()) is fragmented
    before = _file_digest(path)
    real_load = MP4Info.load
    seen: list[str] = []

    def reporting_load(self: Any, *args: Any, **kwargs: Any) -> None:
        real_load(self, *args, **kwargs)
        seen.append(self.codec)
        self.codec = reported

    monkeypatch.setattr(MP4Info, "load", reporting_load)

    assert _write(path, FULL) is TagOutcome.UNSUPPORTED
    assert seen == ["mp4a.40.2"]  # the candidate path really parsed this file once
    assert _file_digest(path) == before


_OGG_NAMES = ["vorbis", "opus", "opus-in-ogg"]


def _theora_link(tmp_path: Path) -> bytes:
    """A separately encoded video-only Ogg stream, to be chained after audio."""
    _require(("libtheora",))
    frames = _write_raw_frames(tmp_path / "frames.rgb", _EVEN_SIZE)
    return _encode(
        [*_raw_video_input(frames, _EVEN_SIZE), "-c:v", "libtheora", "-pix_fmt", "yuv420p",
         "-f", "ogg"],
        tmp_path / "video.ogv",
    ).read_bytes()


@requires_ffmpeg
@pytest.mark.parametrize("second", ["theora", "audio"])
@pytest.mark.parametrize("name", _OGG_NAMES)
def test_chained_ogg_is_unsupported_and_unchanged(
    tmp_path: Path, name: str, second: str
) -> None:
    fmt = _BY_NAME[name]
    path = _make(tmp_path, fmt)
    audio_only = path.read_bytes()
    packets = _packet_hash(path, "0:a:0")
    if second == "theora":
        link = _theora_link(tmp_path)
    else:
        link = _encode(["-i", str(tmp_path / "tone.wav"), *fmt.arguments],
                       tmp_path / f"second{fmt.suffix}").read_bytes()
    # A chain: the first logical stream ends (EOS) before the second begins.
    chained = audio_only + link
    path.write_bytes(chained)

    assert _write(path, FULL) is TagOutcome.UNSUPPORTED
    assert path.read_bytes() == chained

    # The same audio on its own is still tagged, with its packets unchanged.
    path.write_bytes(audio_only)
    assert _write(path, FULL) is TagOutcome.WRITTEN
    assert _packet_hash(path, "0:a:0") == packets


@requires_ffmpeg
@pytest.mark.parametrize("name", _OGG_NAMES)
@pytest.mark.parametrize("trailer", [b"OggS\x00junk", b"garbage" * 10], ids=["page", "text"])
def test_ogg_with_malformed_trailing_data_is_a_parse_error(
    tmp_path: Path, name: str, trailer: bytes
) -> None:
    path = _make(tmp_path, _BY_NAME[name])
    data = path.read_bytes() + trailer
    path.write_bytes(data)

    with pytest.raises(TagError) as caught:
        _write(path, FULL)
    assert caught.value.kind is TagErrorKind.PARSE
    assert path.read_bytes() == data


@requires_ffmpeg
@pytest.mark.parametrize("name", _OGG_NAMES)
def test_ogg_scan_io_error_propagates(tmp_path: Path, name: str) -> None:
    fmt = _BY_NAME[name]
    path = _make(tmp_path, fmt)
    path.write_bytes(path.read_bytes() + _theora_link(tmp_path))
    before = _file_digest(path)
    error = OSError(errno.EIO, "injected")
    with path.open("r+b") as raw:
        # Fails a read deep inside the page scan, after the first page.
        faulty = _FaultyFile(raw, "read", error, armed=False)
        reads = 0

        def counting_read(*args: Any) -> bytes:
            nonlocal reads
            reads += 1
            if reads > 40:
                raise error
            return raw.read(*args)

        faulty.read = counting_read  # type: ignore[attr-defined]
        with pytest.raises(OSError) as caught:
            write_text_tags(cast(BinaryIO, faulty), suffix=fmt.suffix, tags=FULL)
    assert caught.value is error
    assert _file_digest(path) == before


# --- malformed --------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".mp3", ".m4a", ".flac", ".ogg", ".opus"])
@pytest.mark.parametrize("data", [
    b"", b"not audio at all " * 40, b"fLaC", b"OggS\x00\x02", b"\x00\x00\x00\x14ftypM4A ",
    # Short ID3: Mutagen raises its own bare OSError for this.
    b"ID3\x04\x00\x00\x00\x00\x7f\x7fshort",
], ids=["empty", "text", "flac-magic", "ogg-magic", "ftyp-only", "short-id3"])
def test_malformed_content_is_unchanged(suffix: str, data: bytes) -> None:
    buffer = io.BytesIO(data)
    try:
        outcome = write_text_tags(buffer, suffix=suffix, tags=FULL)
    except TagError as exc:
        assert exc.kind is TagErrorKind.PARSE
    else:
        # Magic of a different known format is a misleading suffix.
        assert outcome is TagOutcome.UNSUPPORTED
        assert data[:4] in (b"fLaC", b"OggS") or data[4:8] == b"ftyp"
    assert buffer.getvalue() == data


@pytest.mark.parametrize(("suffix", "data"), [
    (".mp3", b"not audio at all " * 40), (".mp3", b"ID3\x04\x00\x00\x00\x00\x7f\x7fshort"),
    (".flac", b"fLaC"), (".ogg", b"OggS\x00\x02"), (".m4a", b"\x00\x00\x00\x14ftypM4A "),
])
def test_matching_but_broken_content_is_a_parse_error(suffix: str, data: bytes) -> None:
    with pytest.raises(TagError) as caught:
        write_text_tags(io.BytesIO(data), suffix=suffix, tags=FULL)
    assert caught.value.kind is TagErrorKind.PARSE


# --- I/O errors -------------------------------------------------------------


class _FaultyFile:
    """Real file handle whose chosen operation fails once ``armed``."""

    def __init__(self, raw: BinaryIO, operation: str, error: OSError, armed: bool = True) -> None:
        self._raw = raw
        self._operation = operation
        self._error = error
        self.armed = armed
        self.arm_on_flush = False

    def __getattr__(self, name: str) -> Any:
        real = getattr(self._raw, name)

        def call(*args: Any) -> Any:
            if self.armed and name == self._operation and args != (b"",):
                raise self._error
            result = real(*args)
            if name == "flush" and self.arm_on_flush:
                self.armed = True
            return result

        return call


@requires_ffmpeg
@core_formats
@pytest.mark.parametrize(("operation", "code"), [
    ("read", errno.EIO), ("write", errno.ENOSPC), ("flush", errno.ENOSPC),
    ("write", errno.EACCES), ("tell", errno.EIO), ("seek", errno.EIO),
    ("seek", errno.EINVAL),  # plain seek(0), not a probe before the start
])
def test_io_error_propagates_unwrapped(
    tmp_path: Path, fmt: _Format, operation: str, code: int
) -> None:
    path = _make(tmp_path, fmt)
    error = OSError(code, "injected")
    with path.open("r+b") as raw:
        faulty = _FaultyFile(raw, operation, error)
        with pytest.raises(OSError) as caught:
            write_text_tags(cast(BinaryIO, faulty), suffix=fmt.suffix, tags=FULL)
    assert caught.value is error


@requires_ffmpeg
@core_formats
def test_io_error_while_verifying_propagates(tmp_path: Path, fmt: _Format) -> None:
    # Rewriting equal tags fits in place, so only the adapter's final flush arms.
    path = _make(tmp_path, fmt)
    assert _write(path, FULL) is TagOutcome.WRITTEN
    error = OSError(errno.EIO, "injected")
    with path.open("r+b") as raw:
        faulty = _FaultyFile(raw, "read", error, armed=False)
        faulty.arm_on_flush = True
        with pytest.raises(OSError) as caught:
            write_text_tags(cast(BinaryIO, faulty), suffix=fmt.suffix, tags=FULL)
    assert caught.value is error


@requires_ffmpeg
@core_formats
def test_real_read_only_handle_is_an_os_error(tmp_path: Path, fmt: _Format) -> None:
    path = _make(tmp_path, fmt)
    before = _file_digest(path)
    with path.open("rb") as handle, pytest.raises(io.UnsupportedOperation):
        write_text_tags(handle, suffix=fmt.suffix, tags=FULL)
    assert _file_digest(path) == before


def test_swallowed_io_error_is_still_raised() -> None:
    error = OSError(errno.EIO, "injected")
    handle = mutagen_tags._Handle(cast(BinaryIO, _FaultyFile(io.BytesIO(b"x"), "read", error)))
    with pytest.raises(OSError) as caught, mutagen_tags._translated(handle, TagErrorKind.PARSE):
        try:
            handle.read(1)
        except OSError:
            pass
    assert caught.value is error


def test_library_os_error_is_a_tag_error() -> None:
    handle = mutagen_tags._Handle(io.BytesIO(b"x"))
    with pytest.raises(TagError), mutagen_tags._translated(handle, TagErrorKind.PARSE):
        raise OSError("library short read")


def test_real_seek_before_start_is_only_a_probe(tmp_path: Path) -> None:
    path = tmp_path / "small"
    path.write_bytes(b"0123456789")
    with path.open("r+b") as raw:
        handle = mutagen_tags._Handle(raw)
        raw.seek(3)
        with pytest.raises(OSError) as caught:
            handle.seek(-131, 2)
        assert caught.value.errno == errno.EINVAL
        assert raw.tell() == 3
        # Mutagen's own ID3v1 lookup on a file shorter than the tag.
        raw.seek(0)
        with pytest.raises(ID3NoHeaderError):
            ID3(handle)
        assert handle.io_error is None


class _InvalidSeek:
    """Real handle whose end-relative negative seeks fail with EINVAL."""

    def __init__(self, raw: BinaryIO, error: OSError) -> None:
        self._raw = raw
        self._error = error

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 2 and offset < 0:
            raise self._error
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()


@pytest.mark.parametrize(("offset", "probe"), [(-5, False), (-10, False), (-11, True)])
def test_seek_einval_is_checked_against_the_real_size(offset: int, probe: bool) -> None:
    error = OSError(errno.EINVAL, "injected")
    raw = _InvalidSeek(io.BytesIO(b"0123456789"), error)
    handle = mutagen_tags._Handle(cast(BinaryIO, raw))
    expected: type[Exception] = TagError if probe else OSError
    with pytest.raises(expected) as caught, mutagen_tags._translated(handle, TagErrorKind.PARSE):
        handle.seek(offset, 2)
    assert (caught.value is error) is not probe


class _ShortRelocation:
    """Real handle that shortens the first write of moved original content."""

    def __init__(self, raw: BinaryIO, original: bytes) -> None:
        self._raw = raw
        self._original = original
        self.injected = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def write(self, data: bytes) -> int:
        if not self.injected and len(data) > 1_000 and bytes(data) in self._original:
            self.injected = True
            return self._raw.write(data[: len(data) // 2])
        return self._raw.write(data)


@requires_ffmpeg
@core_formats
def test_short_write_while_relocating_audio_is_an_io_error(
    tmp_path: Path, fmt: _Format
) -> None:
    # Core M4A is faststart; with ``mdat`` first a growing tag moves no audio.
    path = _make(tmp_path, fmt)
    with path.open("r+b") as raw:
        short = _ShortRelocation(raw, path.read_bytes())
        with pytest.raises(OSError, match="short write") as caught:
            write_text_tags(
                cast(BinaryIO, short), suffix=fmt.suffix, tags=TextTags(title="x" * 90_000)
            )
    assert short.injected
    assert caught.value.errno == errno.EIO


# --- verification -----------------------------------------------------------


@requires_ffmpeg
@core_formats
def test_reread_tag_mismatch_is_a_verify_error(
    tmp_path: Path, fmt: _Format, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _make(tmp_path, fmt)
    real_save = fmt.opener.save  # type: ignore[attr-defined]

    def dropping_save(self: Any, *args: Any, **kwargs: Any) -> None:
        for key in ("TIT2", "\xa9nam", "title"):
            if key in _native(self):
                del self.tags[key]
        real_save(self, *args, **kwargs)

    monkeypatch.setattr(fmt.opener, "save", dropping_save)
    with pytest.raises(TagError) as caught:
        _write(path, FULL)
    assert caught.value.kind is TagErrorKind.VERIFY


@requires_ffmpeg
@core_formats
@pytest.mark.parametrize("damage", ["truncate", "invariants"])
def test_damaged_result_is_a_verify_error(
    tmp_path: Path, fmt: _Format, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    path = _make(tmp_path, fmt)
    if damage == "truncate":
        def broken_save(self: Any, handle: Any, **kwargs: Any) -> None:
            handle.truncate(0)

        monkeypatch.setattr(fmt.opener, "save", broken_save)
    else:
        values = iter([{"length": 1.0}, {"length": 0.5}])
        monkeypatch.setattr(mutagen_tags, "_audio_invariants", lambda audio: next(values))
    with pytest.raises(TagError) as caught:
        _write(path, FULL)
    assert caught.value.kind is TagErrorKind.VERIFY
