"""URL validation for media analysis requests.

v0.1 boundary (ARCHITECTURE.md §50): accept only expected http(s) URLs and
reject obviously unsafe schemes and local filesystem paths. Heavier SSRF
policy is intentionally out of scope for the trusted private deployment.
"""

from urllib.parse import urlparse

from app.domain.analysis import AnalysisError, AnalysisErrorCode

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MAX_URL_LENGTH = 2048


def validate_media_url(raw_url: str) -> str:
    """Validate a user-submitted media URL and return the cleaned value."""
    candidate = raw_url.strip()
    if not candidate:
        raise AnalysisError(
            AnalysisErrorCode.INVALID_URL, detail="URL is empty"
        )
    if len(candidate) > _MAX_URL_LENGTH:
        raise AnalysisError(
            AnalysisErrorCode.INVALID_URL, detail="URL exceeds maximum length"
        )

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise AnalysisError(
            AnalysisErrorCode.INVALID_URL,
            detail=f"Unsupported URL scheme: {parsed.scheme or '(none)'}",
        )
    if not parsed.hostname:
        raise AnalysisError(
            AnalysisErrorCode.INVALID_URL, detail="URL has no hostname"
        )
    return candidate
