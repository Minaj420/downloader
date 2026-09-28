"""Filename sanitization for media written into the library.

Rules follow ARCHITECTURE.md §33: preserve useful Unicode, remove path
separators, NUL and control characters, prevent ``..`` traversal, trim
invalid trailing characters, cap length, and fall back to a deterministic
name when nothing usable remains.

``sanitize_media_filename`` additionally preserves the final extension
and caps the combined name by UTF-8 byte length (filesystem NAME_MAX is a
byte limit), so a long title can neither eat the extension nor produce a
name that exceeds the filesystem limit for multi-byte titles.
"""

import unicodedata

_FORBIDDEN_CHARACTERS = set('/\\:*?"<>|')
_MAX_FILENAME_LENGTH = 150
_MAX_MEDIA_FILENAME_BYTES = 150
_FALLBACK_FILENAME = "download"


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
