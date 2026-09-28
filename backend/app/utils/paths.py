"""Filesystem path containment helpers (ARCHITECTURE.md §33, §49)."""

from pathlib import Path


def ensure_within_root(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and verify it stays strictly inside ``root``.

    The root itself is rejected as well: callers guard destructive
    operations (e.g. ``rmtree``) that must never target the configured
    root directory (ARCHITECTURE.md §33, §49).
    """
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate == resolved_root:
        raise ValueError(f"path equals root: {resolved_candidate}")
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f"path escapes root: {resolved_candidate}")
    return resolved_candidate
