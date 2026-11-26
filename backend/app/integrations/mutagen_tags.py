"""Mutagen text-tag adapter (PRODUCT_SPEC §14, ARCHITECTURE §14).

Thin synchronous boundary: it edits tags in place on one candidate file the
caller already created, opened read/write in binary mode and still owns. It
never opens, copies, renames, deletes or publishes a path and never closes the
handle. After any raised error the candidate may be partially written and must
not be published.

Only supplied fields are updated. Absent managed fields, unrelated metadata,
artwork and already known track/disc totals stay; totals are never derived.

Not wired into any runner yet.
"""

from __future__ import annotations

import errno
import re
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, BinaryIO

from mutagen.flac import FLAC, Padding
from mutagen.id3 import (
    ID3,
    TALB,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    Encoding,
    Frames,
    ID3Tags,
    ParseID3v1,
    TextFrame,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

# Mutagen has no public API that reveals non-audio MP4 tracks or fragments, and
# this project writes no parser of its own; hence the version cap in pyproject.
from mutagen.mp4._atom import Atoms
from mutagen.ogg import OggPage
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from app.services.text_tags import TextTags


class TagOutcome(StrEnum):
    WRITTEN = "written"
    # Candidate untouched: format outside the approved audio-tag scope.
    UNSUPPORTED = "unsupported"
    # Candidate untouched: no intended tags.
    NOTHING_TO_WRITE = "nothing_to_write"


class TagErrorKind(StrEnum):
    PARSE = "parse"  # candidate could not be parsed; nothing was written
    WRITE = "write"  # tags could not be represented or saved
    VERIFY = "verify"  # re-read after saving failed or did not match


class TagError(Exception):
    """Tag-format failure. Genuine file I/O errors stay ``OSError``."""

    def __init__(self, kind: TagErrorKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}: {detail}")


class _Handle:
    """Pass-through that remembers the first genuine I/O error.

    Mutagen wraps or swallows ``OSError`` from the file and also raises bare
    ``OSError`` for short data, so the exception type alone cannot tell the two
    apart. Only errors raised by the real handle are recorded. The one exception
    is Mutagen's end-relative probe that really lands before the file start.
    Mutagen ignores write counts, so a short write is raised here.
    """

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self.io_error: OSError | None = None

    def _record(self, exc: OSError) -> None:
        if self.io_error is None:
            self.io_error = exc

    def read(self, size: int = -1) -> bytes:
        try:
            return self._raw.read(size)
        except OSError as exc:
            self._record(exc)
            raise

    def write(self, data: bytes) -> int:
        try:
            written = self._raw.write(data)
            if written != len(data):
                raise OSError(errno.EIO, f"short write: {written} of {len(data)} bytes")
        except OSError as exc:
            self._record(exc)
            raise
        return written

    def _lands_before_start(self, offset: int, whence: int) -> bool:
        if whence != 2 or offset >= 0:
            return False
        try:
            position = self._raw.tell()
            end = self._raw.seek(0, 2)
            self._raw.seek(position)
        except OSError:
            return False
        return end + offset < 0

    def seek(self, offset: int, whence: int = 0) -> int:
        try:
            return self._raw.seek(offset, whence)
        except OSError as exc:
            if exc.errno != errno.EINVAL or not self._lands_before_start(offset, whence):
                self._record(exc)
            raise

    def tell(self) -> int:
        try:
            return self._raw.tell()
        except OSError as exc:
            self._record(exc)
            raise

    def truncate(self, size: int | None = None) -> int:
        try:
            return self._raw.truncate(size)
        except OSError as exc:
            self._record(exc)
            raise

    def flush(self) -> None:
        try:
            self._raw.flush()
        except OSError as exc:
            self._record(exc)
            raise


@contextmanager
def _translated(handle: _Handle, kind: TagErrorKind) -> Iterator[None]:
    """Recorded I/O errors win; everything else becomes ``TagError(kind)``."""
    try:
        yield
    except Exception as exc:
        if handle.io_error is not None:
            raise handle.io_error from None
        if isinstance(exc, TagError):
            raise
        # Mutagen leaks struct/index/value errors on malformed data.
        raise TagError(kind, f"{type(exc).__name__}: {exc}") from exc
    if handle.io_error is not None:
        raise handle.io_error


# --- format detection -------------------------------------------------------

_FAMILY_BY_SUFFIX = {
    ".mp3": "mpeg", ".m4a": "mp4", ".flac": "flac", ".opus": "ogg", ".ogg": "ogg",
}
_FOREIGN_MAGIC = (
    b"RIFF", b"RF64", b"FORM", b"FLV", b"\x1aE\xdf\xa3", b"\x30\x26\xb2\x75",
)
# Exact codec strings as Mutagen reports them (``mp4a.<OTI hex>[.<AOT>]``),
# matching the AAC labels of the analysis normalizer, plus ALAC. A prefix is
# not enough: ``mp4a.40.34`` is MP3 and bare ``mp4a.40`` names no object type.
_MP4_AUDIO_CODECS = frozenset({
    "mp4a.40.1", "mp4a.40.2", "mp4a.40.4", "mp4a.40.5", "mp4a.40.29",  # MPEG-4 AAC
    "mp4a.66", "mp4a.67", "mp4a.68",  # MPEG-2 AAC
    "alac",
})


def _content_family(handle: _Handle) -> str | None:
    """Family named by the leading magic after ID3v2 tags; ``None`` if unknown."""
    offset = 0
    while True:
        handle.seek(offset)
        head = handle.read(12)
        if not head.startswith(b"ID3"):
            break
        handle.seek(offset)
        offset += ID3(handle, translate=False, load_v1=False).size
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "ogg"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(_FOREIGN_MAGIC):
        return "foreign"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "mpeg" if head[1] & 0x06 else "adts"
    return None


def _open_mp3(handle: _Handle) -> Any | None:
    # ID3v1 is never merged into ID3v2; see ``_has_id3v1``.
    audio: Any = MP3(handle, translate=False, load_v1=False)
    if audio.info.layer != 3:
        return None
    if audio.info.sketchy:
        raise TagError(TagErrorKind.PARSE, "no reliable MPEG audio frames")
    return audio


def _open_mp4(handle: _Handle) -> Any | None:
    atoms: Any = Atoms(handle)
    handlers: list[bytes] = []
    for track in atoms[b"moov"].findall(b"trak"):
        complete, data = track[b"mdia", b"hdlr"].read(handle)
        if not complete:
            raise TagError(TagErrorKind.PARSE, "truncated MP4 track handler")
        handlers.append(data[8:12])
    if handlers != [b"soun"]:
        return None
    handle.seek(0)
    audio: Any = MP4(handle)
    if audio.info.codec not in _MP4_AUDIO_CODECS:
        return None
    # Supported audio only after video/codec checks: an error, not a silent
    # skip, because Mutagen does not relocate every fragment offset.
    if any(atom.name == b"moof" for atom in atoms.atoms) or b"moov.mvex" in atoms:
        raise TagError(TagErrorKind.WRITE, "fragmented M4A cannot be tagged safely")
    return audio


def _open_ogg(handle: _Handle, suffix: str) -> Any | None:
    handle.seek(0)
    first = OggPage(handle)
    if not first.first or not first.packets:
        raise TagError(TagErrorKind.PARSE, "Ogg stream has no identification page")
    # Every page must belong to the first logical stream. A later
    # beginning-of-stream page or serial (multiplexed, or chained after the
    # first stream ended) may be video. Malformed or truncated trailing data
    # raises Mutagen's Ogg error (PARSE); I/O errors are recorded by the handle.
    while True:
        try:
            page = OggPage(handle)
        except EOFError:
            break
        if page.first or page.serial != first.serial:
            return None
    handle.seek(0)
    if first.packets[0].startswith(b"OpusHead"):
        return OggOpus(handle)
    if first.packets[0].startswith(b"\x01vorbis") and suffix == ".ogg":
        return OggVorbis(handle)
    return None


def _open(handle: _Handle, suffix: str) -> Any | None:
    """Parsed audio, or ``None`` when the real format is unsupported."""
    expected = _FAMILY_BY_SUFFIX[suffix]
    actual = _content_family(handle)
    if actual is not None and actual != expected:
        return None
    handle.seek(0)
    if expected == "mpeg":
        return _open_mp3(handle)
    if expected == "mp4":
        return _open_mp4(handle)
    if expected == "ogg":
        return _open_ogg(handle, suffix)
    return FLAC(handle)


# --- tag updates ------------------------------------------------------------

_NUMBER_WITH_TOTAL = re.compile(r"\s*[0-9]*\s*/\s*([0-9]+)\s*")
# ``update_to_v24`` discards these without a replacement.
_ID3_DISCARDED = ("RVAD", "EQUA", "TRDA", "TSIZ")


def _with_known_total(number: int, current: str) -> str:
    """``number``, keeping an already stored ``n/total`` total."""
    match = _NUMBER_WITH_TOTAL.fullmatch(current)
    return f"{number}/{match[1]}" if match else str(number)


def _has_id3v1(handle: _Handle) -> bool:
    """ID3v1 at the end, including the slightly short form Mutagen rewrites."""
    size = handle.seek(0, 2)
    handle.seek(max(0, size - 131))
    tail = handle.read(131)
    return tail[-128:].startswith(b"TAG") or ParseID3v1(tail) is not None


def _id3_containers(id3: Any) -> list[Any]:
    """The tag itself and every nested chapter/contents tag."""
    found = [id3]
    for frame in (*id3.getall("CHAP"), *id3.getall("CTOC")):
        found.extend(_id3_containers(frame.sub_frames))
    return found


def _legacy_date(id3: Any) -> dict[str, str]:
    return {key: str(id3[key]) for key in ("TYER", "TDAT", "TIME") if key in id3}


def _date_survives(before: dict[str, str], id3: Any) -> bool:
    if not before:
        return True
    if "TDRC" not in id3:
        return False
    date = str(id3["TDRC"])
    if before == {"TYER": date}:
        return True
    # Mutagen's own reverse conversion must give the old frames back.
    reverse: Any = ID3Tags()
    reverse.add(TDRC(encoding=Encoding.UTF8, text=[date]))
    reverse.update_to_v23()
    return before.items() <= _legacy_date(reverse).items()


_UNSET_CHAPTER_OFFSET = 0xFFFFFFFF


def _replaced_id3_frames(tags: TextTags) -> set[str]:
    """Top-level frames ``_apply_id3`` overwrites with supplied values."""
    replaced: set[str] = set()
    if tags.title is not None:
        replaced.add("TIT2")
    if tags.artists:
        replaced.add("TPE1")
    if tags.album is not None:
        replaced.add("TALB")
    if tags.album_artists:
        replaced.add("TPE2")
    if tags.track_number is not None:
        replaced.add("TRCK")
    if tags.disc_number is not None:
        replaced.add("TPOS")
    if tags.date is not None:
        replaced |= {"TDRC", "TYER", "TDAT", "TIME"}
    return replaced


_RAW_TXXX_FRAMES = {name: kind for name, kind in Frames.items() if name != "TXXX"}


def _parse_dropped_txxx(handle: _Handle, id3: Any) -> bool:
    """Whether Mutagen's parse silently dropped a ``TXXX`` frame.

    Mutagen discards a ``TXXX`` without a value as junk (in ID3v2.3 trailing
    zeros count as padding, so even ``desc\\0\\0`` has none) and keeps only one
    of several with the same description. A second, inspection-only Mutagen
    parse that does not know ``TXXX`` returns every serialised one, nested
    ones included, as raw bytes; fewer parsed frames means a loss.
    """
    if not id3.size:
        return False  # no ID3v2 tag in the file
    handle.seek(0)
    raw: Any = ID3(handle, translate=False, load_v1=False, known_frames=_RAW_TXXX_FRAMES)
    serialised = sum(
        frame[:4] == b"TXXX" for container in _id3_containers(raw)
        for frame in container.unknown_frames
    )
    parsed = sum(len(container.getall("TXXX")) for container in _id3_containers(id3))
    return serialised != parsed


def _refuse_unpreservable_id3(id3: Any, tags: TextTags) -> None:
    """Refuse before any write what saving would silently break or drop.

    Chapter byte offsets point into the file and go stale once the tag size
    changes; Mutagen does not rewrite them. Empty text frames are never saved,
    so an existing one (e.g. a ``TXXX`` whose only content is its description)
    would vanish, unless it is a top-level frame replaced by a supplied value.
    """
    replaced = _replaced_id3_frames(tags)
    for container in _id3_containers(id3):
        for key, frame in container.items():
            if frame.FrameID == "CHAP" and (
                frame.start_offset != _UNSET_CHAPTER_OFFSET
                or frame.end_offset != _UNSET_CHAPTER_OFFSET
            ):
                raise TagError(TagErrorKind.WRITE, f"chapter {key} has byte offsets")
            if isinstance(frame, TextFrame) and not str(frame) and not (
                container is id3 and key in replaced
            ):
                raise TagError(TagErrorKind.WRITE, f"empty frame {key} cannot be preserved")


def _upgrade_id3(id3: Any, tags: TextTags) -> None:
    """Convert to ID3v2.4, refusing when other tags could not be carried over."""
    lost: list[str] = []
    if id3.version[:2] == (2, 2):
        # Frames without an ID3v2.3 equivalent are already dropped by the parse.
        lost.append("ID3v2.2")
    legacy = []
    for container in _id3_containers(id3):
        lost += [key for key in _ID3_DISCARDED if key in container]
        if container.unknown_frames and container._unknown_v2_version != 4:
            lost.append("unknown frames")
        legacy.append((
            container,
            _legacy_date(container),
            str(container["TORY"]) if "TORY" in container else None,
            list(container["IPLS"].people) if "IPLS" in container else None,
        ))

    id3.update_to_v24()  # in memory, nested tags included

    for container, date, original_year, people in legacy:
        superseded = container is id3 and tags.date is not None
        if not superseded and not _date_survives(date, container):
            lost.append("date")
        if original_year is not None and (
            "TDOR" not in container or str(container["TDOR"]) != original_year
        ):
            lost.append("TORY")
        if people is not None and container["TIPL"].people != people:
            lost.append("IPLS")
    if lost:
        raise TagError(TagErrorKind.WRITE, f"ID3v2.4 cannot preserve: {', '.join(lost)}")


def _apply_id3(id3: Any, tags: TextTags) -> None:
    def put(frame_type: Any, text: list[str]) -> None:
        name = frame_type.__name__
        id3.setall(name, [frame_type(encoding=Encoding.UTF8, text=text)])

    def put_number(frame_type: Any, number: int) -> None:
        frames = id3.getall(frame_type.__name__)
        current = str(frames[0].text[0]) if frames and frames[0].text else ""
        put(frame_type, [_with_known_total(number, current)])

    if tags.title is not None:
        put(TIT2, [tags.title])
    if tags.artists:
        put(TPE1, list(tags.artists))
    if tags.album is not None:
        put(TALB, [tags.album])
    if tags.album_artists:
        put(TPE2, list(tags.album_artists))
    if tags.track_number is not None:
        put_number(TRCK, tags.track_number)
    if tags.disc_number is not None:
        put_number(TPOS, tags.disc_number)
    if tags.date is not None:
        put(TDRC, [tags.date])
        if str(id3["TDRC"].text[0]) != tags.date:
            raise TagError(TagErrorKind.WRITE, "date is not a valid ID3 timestamp")


def _apply_mp4(atoms: Any, tags: TextTags) -> None:
    def put_number(key: str, number: int) -> None:
        current = atoms.get(key)
        atoms[key] = [(number, current[0][1] if current else 0)]

    if tags.title is not None:
        atoms["\xa9nam"] = [tags.title]
    if tags.artists:
        atoms["\xa9ART"] = list(tags.artists)
    if tags.album is not None:
        atoms["\xa9alb"] = [tags.album]
    if tags.album_artists:
        atoms["aART"] = list(tags.album_artists)
    if tags.track_number is not None:
        put_number("trkn", tags.track_number)
    if tags.disc_number is not None:
        put_number("disk", tags.disc_number)
    if tags.date is not None:
        atoms["\xa9day"] = [tags.date]


def _apply_vorbis(comments: Any, tags: TextTags) -> None:
    def put_number(key: str, number: int) -> None:
        current = comments.get(key) or [""]
        comments[key] = [_with_known_total(number, current[0])]

    # Undecodable bytes were replaced while parsing and would be lost on save.
    if any("�" in value for _, value in comments):
        raise TagError(TagErrorKind.WRITE, "existing comments are not valid UTF-8")
    if tags.title is not None:
        comments["TITLE"] = [tags.title]
    if tags.artists:
        comments["ARTIST"] = list(tags.artists)
    if tags.album is not None:
        comments["ALBUM"] = [tags.album]
    if tags.album_artists:
        comments["ALBUMARTIST"] = list(tags.album_artists)
    if tags.track_number is not None:
        put_number("TRACKNUMBER", tags.track_number)
    if tags.disc_number is not None:
        put_number("DISCNUMBER", tags.disc_number)
    if tags.date is not None:
        comments["DATE"] = [tags.date]


def _apply(handle: _Handle, audio: Any, tags: TextTags) -> None:
    if audio.tags is None:
        audio.add_tags()
    if isinstance(audio, MP3):
        # Mutagen can only rebuild or remove ID3v1, never keep it as it is.
        if _has_id3v1(handle):
            raise TagError(TagErrorKind.WRITE, "existing ID3v1 tag cannot be preserved")
        if _parse_dropped_txxx(handle, audio.tags):
            raise TagError(TagErrorKind.WRITE, "a TXXX frame was lost while parsing")
        _refuse_unpreservable_id3(audio.tags, tags)
        _upgrade_id3(audio.tags, tags)
        _apply_id3(audio.tags, tags)
    elif isinstance(audio, MP4):
        _apply_mp4(audio.tags, tags)
    else:
        _apply_vorbis(audio.tags, tags)


# --- verification -----------------------------------------------------------

_INVARIANTS = (
    "length", "channels", "sample_rate", "bits_per_sample", "codec", "version", "layer",
    "total_samples", "md5_signature",
)


def _audio_invariants(audio: Any) -> dict[str, object]:
    return {name: getattr(audio.info, name, None) for name in _INVARIANTS}


def _id3_snapshot(id3: Any) -> object:
    frames: dict[str, object] = {}
    # No filter for empty text frames: any left would be dropped and must fail.
    for key, frame in id3.items():
        if frame.FrameID in ("CHAP", "CTOC"):
            # Nested frame order changes on save, so compare nested tags by key.
            fields = {k: repr(v) for k, v in vars(frame).items() if k != "sub_frames"}
            frames[key] = fields, _id3_snapshot(frame.sub_frames)
        else:
            frames[key] = repr(frame)
    return frames, sorted(bytes(frame) for frame in id3.unknown_frames if len(frame) > 10)


def _snapshot(audio: Any) -> object:
    """Every stored tag, managed or not, in a comparable form."""
    if isinstance(audio, MP3):
        return _id3_snapshot(audio.tags)
    if isinstance(audio, MP4):
        return dict((audio.tags or {}).items())
    if isinstance(audio, FLAC):
        return [
            (block.code, bytes(block.write()))
            for block in audio.metadata_blocks if not isinstance(block, Padding)
        ]
    return [(key.lower(), value) for key, value in audio.tags]


def _reopen(handle: _Handle, suffix: str) -> Any | None:
    if suffix == ".mp3":
        # Only the ID3v2.4 tag that was just written counts, not ID3v1.
        handle.seek(0)
        audio: Any = MP3(handle, load_v1=False)
        if audio.tags is None or audio.tags.version[:2] != (2, 4):
            return None
        return audio
    return _open(handle, suffix)


# --- public entry -----------------------------------------------------------


def write_text_tags(candidate: BinaryIO, *, suffix: str, tags: TextTags) -> TagOutcome:
    """Write ``tags`` into the caller-owned ``candidate`` and verify a re-read.

    ``candidate`` is a seekable ``r+b`` handle and ``suffix`` the intended
    extension (``".mp3"``); the real content must agree with it. Raises
    ``TagError`` for tag-format problems and the original ``OSError`` for
    genuine I/O failures. Handle position afterwards is unspecified.
    """
    suffix = suffix.lower()
    if suffix not in _FAMILY_BY_SUFFIX:
        return TagOutcome.UNSUPPORTED
    if tags == TextTags():
        return TagOutcome.NOTHING_TO_WRITE

    handle = _Handle(candidate)
    with _translated(handle, TagErrorKind.PARSE):
        audio = _open(handle, suffix)
        if audio is not None:
            invariants = _audio_invariants(audio)
    if audio is None:
        return TagOutcome.UNSUPPORTED

    with _translated(handle, TagErrorKind.WRITE):
        _apply(handle, audio, tags)
        expected = _snapshot(audio)
        handle.seek(0)
        audio.save(handle)
        handle.flush()

    with _translated(handle, TagErrorKind.VERIFY):
        written = _reopen(handle, suffix)
        if written is None or type(written) is not type(audio):
            raise TagError(TagErrorKind.VERIFY, "re-read format differs")
        if _snapshot(written) != expected:
            raise TagError(TagErrorKind.VERIFY, "re-read tags differ")
        if _audio_invariants(written) != invariants:
            raise TagError(TagErrorKind.VERIFY, "re-read audio properties differ")
    return TagOutcome.WRITTEN
