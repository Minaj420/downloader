"""Filename sanitization for media written into the library.

Rules follow ARCHITECTURE.md §33: preserve useful Unicode, remove path
separators, NUL and control characters, prevent ``..`` traversal, trim
invalid trailing characters, cap length, and fall back to a deterministic
name when nothing usable remains.

``sanitize_media_filename`` additionally preserves the final extension
and caps the combined name by UTF-8 byte length (filesystem NAME_MAX is a
byte limit), so a long title can neither eat the extension nor produce a
name that exceeds the filesystem limit for multi-byte titles.

``sanitize_path_segment`` cleans one directory segment with the same
rules and the same UTF-8 byte bound, without any extension handling.

``conflict_media_filenames`` builds the ordered no-clobber candidates for
one published media file (PRODUCT_SPEC.md §15–16): ``[NN - ]Title[ (720p)]
[ (n)].ext``. The title is cleaned and cut exactly once, after reserving
room for the prefix, the qualifier, the widest counter and the extension,
so every candidate shares the same title body.
"""

import unicodedata

_FORBIDDEN_CHARACTERS = set('/\\:*?"<>|')
_MAX_FILENAME_LENGTH = 150
_MAX_MEDIA_FILENAME_BYTES = 150
_MAX_PATH_SEGMENT_BYTES = 150
_FALLBACK_FILENAME = "download"
# Inclusive upper bound of the collision counter: "Title (999).ext".
MAX_CONFLICT_COUNTER = 999


def _clean_characters(name: str) -> str:
    """Apply the shared cleaning rules without any length capping."""
    normalized = unicodedata.normalize("NFC", name.strip())
    cleaned_characters: list[str] = []
    for character in normalized:
        if character in _FORBIDDEN_CHARACTERS:
            cleaned_characters.append("_")
        elif unicodedata.category(character).startswith("C"):
            continue
        else:
            cleaned_characters.append(character)
    return "".join(cleaned_characters).strip().rstrip(" .")


def sanitize_filename(name: str, *, max_length: int = _MAX_FILENAME_LENGTH) -> str:
    cleaned = _clean_characters(name)
    if cleaned in {"", ".", ".."}:
        return _FALLBACK_FILENAME
    return cleaned[:max_length].rstrip(" .") or _FALLBACK_FILENAME


def sanitize_media_filename(
    filename: str, *, max_bytes: int = _MAX_MEDIA_FILENAME_BYTES
) -> str:
    """Sanitize a media filename while preserving its final extension.

    The combined ``body.ext`` name never exceeds ``max_bytes`` UTF-8
    bytes. The body is cut at a character boundary so the result is
    always valid UTF-8, and the deterministic fallback name is used when
    nothing usable remains within the body budget.

    The final dot-separated part is the extension regardless of its
    length. A degenerate extension that cannot fit the budget at all is
    cut at a character boundary so that it, the dot, and at least one
    body byte still fit within ``max_bytes``; realistic media extensions
    never reach that branch.
    """
    stem, suffix = _split_extension(filename)
    body = _clean_characters(stem)
    extension = _clean_characters(suffix)
    if not extension:
        fitted = _truncate_to_utf8_bytes(body, max_bytes).rstrip(" .")
        return fitted or _FALLBACK_FILENAME
    # Degenerate case: leave room for the dot and one body byte.
    extension = _truncate_to_utf8_bytes(extension, max(0, max_bytes - 2))
    if not extension:
        fitted = _truncate_to_utf8_bytes(body, max_bytes).rstrip(" .")
        return fitted or _FALLBACK_FILENAME
    body_budget = max_bytes - len(f".{extension}".encode())
    fitted = _truncate_to_utf8_bytes(body, body_budget).rstrip(" .")
    if not fitted:
        fitted = _truncate_to_utf8_bytes(_FALLBACK_FILENAME, body_budget)
    if not fitted:
        # Degenerate budgets smaller than the fallback name itself: keep
        # a non-empty name instead of returning ""/"."/"..".
        return _FALLBACK_FILENAME
    return f"{fitted}.{extension}"


def sanitize_path_segment(
    name: str, *, max_bytes: int = _MAX_PATH_SEGMENT_BYTES
) -> str:
    """Sanitize a single directory segment within ``max_bytes`` UTF-8 bytes.

    The whole name is one segment: dots are never treated as an
    extension separator, and ``/``/``\\`` become ``_`` so a path-shaped
    name can never add a directory level. Trailing dots and spaces are
    trimmed, so ``.``/``..`` and dot-only names use the deterministic
    fallback, which is itself cut to fit the budget. ``max_bytes`` must
    be a positive ``int``.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError(f"max_bytes must be an int: {max_bytes!r}")
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive: {max_bytes!r}")
    # Re-normalize: removing a control/format character can leave a base
    # character next to a combining mark that the first NFC pass kept apart.
    cleaned = unicodedata.normalize("NFC", _clean_characters(name))
    fitted = _truncate_to_utf8_bytes(cleaned, max_bytes).rstrip(" .")
    return fitted or _truncate_to_utf8_bytes(_FALLBACK_FILENAME, max_bytes)


def conflict_media_filenames(
    title: str,
    extension: str,
    *,
    prefix: str = "",
    qualifier: str = "",
    max_bytes: int = _MAX_MEDIA_FILENAME_BYTES,
    max_counter: int = MAX_CONFLICT_COUNTER,
) -> list[str]:
    """Ordered collision candidates ``prefix + title + qualifier [ (n)] .ext``.

    ``prefix`` (e.g. ``"07 - "``) and ``qualifier`` (e.g. ``" (720p)"``)
    are backend-formatted from integers and are used verbatim. The
    untrusted title is cleaned with the shared rules (deterministic
    fallback when nothing usable remains) and cut once at a character
    boundary to the budget left after reserving the prefix, qualifier,
    ``.ext`` and the widest counter suffix ``" (max_counter)"``. Candidate
    1 carries no counter; candidates 2..``max_counter`` add ``" (n)"``.
    Every candidate fits ``max_bytes`` UTF-8 bytes and is a single
    non-traversal path segment.
    """
    if isinstance(max_counter, bool) or not isinstance(max_counter, int) or max_counter < 1:
        raise ValueError(f"max_counter must be a positive int: {max_counter!r}")
    cleaned_extension = _clean_characters(extension)
    fixed = len(f"{prefix}{qualifier} ({max_counter})".encode())
    if cleaned_extension:
        # Degenerate extensions are cut so that at least one title byte fits.
        cleaned_extension = _truncate_to_utf8_bytes(
            cleaned_extension, max_bytes - fixed - 2
        )
    suffix = f".{cleaned_extension}" if cleaned_extension else ""
    title_budget = max_bytes - fixed - len(suffix.encode())
    if title_budget < 1:
        raise ValueError("filename prefix/qualifier leave no room for the title")
    body = _clean_characters(title)
    if body in {"", ".", ".."}:
        body = _FALLBACK_FILENAME
    fitted = _truncate_to_utf8_bytes(body, title_budget).rstrip(" .")
    if not fitted:
        fitted = _truncate_to_utf8_bytes(_FALLBACK_FILENAME, title_budget)
    head = f"{prefix}{fitted}{qualifier}"
    names = [f"{head}{suffix}"]
    names.extend(f"{head} ({counter}){suffix}" for counter in range(2, max_counter + 1))
    for name in names:
        if name in {".", ".."} or "/" in name or "\x00" in name:
            raise ValueError(f"unsafe media filename candidate: {name!r}")
    return names


def _split_extension(filename: str) -> tuple[str, str]:
    stem, separator, suffix = filename.rpartition(".")
    if not separator or not _usable_extension(suffix):
        return filename, ""
    return stem, suffix


def _usable_extension(suffix: str) -> bool:
    """Accept any non-empty suffix regardless of length.

    Length heuristics are intentionally absent: the final dot-separated
    part is the extension no matter how long it is. A suffix containing
    whitespace, forbidden or control characters cannot survive cleaning
    intact, so it stays part of the body instead.
    """
    if not suffix:
        return False
    return not any(
        character.isspace()
        or character in _FORBIDDEN_CHARACTERS
        or unicodedata.category(character).startswith("C")
        for character in suffix
    )


def _truncate_to_utf8_bytes(text: str, byte_budget: int) -> str:
    """Cut ``text`` at a character boundary so it fits ``byte_budget``."""
    if byte_budget <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_budget:
        return text
    return encoded[:byte_budget].decode("utf-8", "ignore")
