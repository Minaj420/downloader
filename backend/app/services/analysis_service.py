"""Analysis orchestration: validate → probe → normalize → persist."""

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.actors import LOCAL_ACTOR_ID
from app.domain.analysis import AnalysisError, AnalysisErrorCode, NormalizedAnalysis
from app.integrations.ytdlp import YtDlpError, YtDlpFailureKind
from app.models.analysis import AnalysisSession
from app.repositories import analyses
from app.services.analysis_normalizer import build_normalized_analysis
from app.utils.urls import validate_media_url

ANALYSIS_SESSION_TTL = timedelta(hours=1)

_YT_DLP_KIND_TO_ANALYSIS_CODE: dict[YtDlpFailureKind, AnalysisErrorCode] = {
    YtDlpFailureKind.BINARY_MISSING: AnalysisErrorCode.INTERNAL_ERROR,
    YtDlpFailureKind.UNSUPPORTED_URL: AnalysisErrorCode.UNSUPPORTED_URL,
    YtDlpFailureKind.AUTHENTICATION_REQUIRED: AnalysisErrorCode.AUTHENTICATION_REQUIRED,
    YtDlpFailureKind.GEO_RESTRICTED: AnalysisErrorCode.GEO_RESTRICTED,
    YtDlpFailureKind.MEDIA_UNAVAILABLE: AnalysisErrorCode.MEDIA_UNAVAILABLE,
    YtDlpFailureKind.NETWORK_ERROR: AnalysisErrorCode.NETWORK_ERROR,
    YtDlpFailureKind.TIMEOUT: AnalysisErrorCode.TIMEOUT,
    YtDlpFailureKind.MALFORMED_OUTPUT: AnalysisErrorCode.MALFORMED_EXTRACTOR_OUTPUT,
    YtDlpFailureKind.EXTRACTOR_FAILED: AnalysisErrorCode.EXTRACTOR_FAILED,
}


class URLAnalyzer(Protocol):
    """Boundary implemented by the yt-dlp integration adapter (and test fakes)."""

    async def analyze(
        self, url: str, platform_hint: str | None = None
    ) -> dict[str, Any]: ...


class AnalysisService:
    def __init__(
        self, analyzer: URLAnalyzer, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._analyzer = analyzer
        self._session_factory = session_factory

    async def analyze(
        self,
        url: str,
        platform_hint: str | None,
        *,
        owner_id: str = LOCAL_ACTOR_ID,
    ) -> NormalizedAnalysis:
        validated_url = validate_media_url(url)
        analysis_id = uuid4().hex
        try:
            raw_info = await self._analyzer.analyze(validated_url, platform_hint)
            normalized = build_normalized_analysis(
                raw_info,
                analysis_id=analysis_id,
                original_url=validated_url,
                platform_hint=platform_hint,
            )
            if normalized.content_type == "single" and not normalized.formats:
                raise AnalysisError(
                    AnalysisErrorCode.MEDIA_UNAVAILABLE,
                    detail="Source returned metadata without usable formats",
                )
        except AnalysisError as exc:
            exc.analysis_id = analysis_id
            await self._persist_failure(
                analysis_id=analysis_id,
                owner_id=owner_id,
                source_url=validated_url,
                platform_hint=platform_hint,
                error=exc,
            )
            raise
        except YtDlpError as exc:
            error = AnalysisError(
                _YT_DLP_KIND_TO_ANALYSIS_CODE[exc.kind], detail=exc.detail
            )
            error.analysis_id = analysis_id
            await self._persist_failure(
                analysis_id=analysis_id,
                owner_id=owner_id,
                source_url=validated_url,
                platform_hint=platform_hint,
                error=error,
            )
            raise error from exc
        except Exception as exc:
            error = AnalysisError(
                AnalysisErrorCode.INTERNAL_ERROR, detail=repr(exc)
            )
            error.analysis_id = analysis_id
            await self._persist_failure(
                analysis_id=analysis_id,
                owner_id=owner_id,
                source_url=validated_url,
                platform_hint=platform_hint,
                error=error,
            )
            raise error from exc

        await self._persist_success(
            analysis_id=analysis_id,
            owner_id=owner_id,
            source_url=validated_url,
            platform_hint=platform_hint,
            normalized=normalized,
        )
        return normalized

    async def get_session_record(
        self, analysis_id: str, owner_id: str
    ) -> AnalysisSession | None:
        async with self._session_factory() as session:
            return await analyses.get_analysis_session(session, analysis_id, owner_id)

    async def _persist_success(
        self,
        *,
        analysis_id: str,
        owner_id: str,
        source_url: str,
        platform_hint: str | None,
        normalized: NormalizedAnalysis,
    ) -> None:
        now = datetime.now(UTC)
        record = AnalysisSession(
            id=analysis_id,
            owner_id=owner_id,
            source_url=source_url,
            platform_hint=platform_hint,
            detected_platform=normalized.detected_platform,
            extractor=normalized.extractor,
            media_type=normalized.content_type,
            title=normalized.title,
            status="completed",
            normalized_result_json=normalized.model_dump_json(),
            created_at=now,
            expires_at=now + ANALYSIS_SESSION_TTL,
        )
        async with self._session_factory() as session:
            await analyses.add_analysis_session(session, record)

    async def _persist_failure(
        self,
        *,
        analysis_id: str,
        owner_id: str,
        source_url: str,
        platform_hint: str | None,
        error: AnalysisError,
    ) -> None:
        now = datetime.now(UTC)
        record = AnalysisSession(
            id=analysis_id,
            owner_id=owner_id,
            source_url=source_url,
            platform_hint=platform_hint,
            status="failed",
            error_code=error.code.value,
            error_message=error.message,
            created_at=now,
            expires_at=now + ANALYSIS_SESSION_TTL,
        )
        async with self._session_factory() as session:
            await analyses.add_analysis_session(session, record)
