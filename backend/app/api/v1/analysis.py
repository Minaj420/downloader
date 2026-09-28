from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.dependencies import get_actor
from app.domain.actors import Actor
from app.domain.analysis import (
    AnalysisError,
    AnalysisErrorBody,
    NormalizedAnalysis,
    error_retryable,
)
from app.domain.platforms import Platform
from app.services.analysis_service import AnalysisService

router = APIRouter(tags=["analysis"])


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    platform_hint: Platform | None = None


class AnalysisSessionResponse(BaseModel):
    analysis_id: str
    status: str
    result: NormalizedAnalysis | None = None
    error: AnalysisErrorBody | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


def _error_response(exc: AnalysisError) -> JSONResponse:
    body = AnalysisErrorBody(
        code=exc.code.value,
        message=exc.message,
        retryable=exc.retryable,
        analysis_id=exc.analysis_id,
    )
    return JSONResponse(
        status_code=exc.http_status, content={"error": body.model_dump()}
    )


def _not_found_response() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": AnalysisErrorBody(
                code="analysis_not_found",
                message="This analysis session does not exist.",
            ).model_dump()
        },
    )


@router.post("/analysis")
async def create_analysis(
    request: AnalyzeRequest, http_request: Request, actor: Actor = Depends(get_actor)
) -> Any:
    service: AnalysisService = http_request.app.state.analysis_service
    hint = request.platform_hint.value if request.platform_hint else None
    try:
        return await service.analyze(request.url, hint, owner_id=actor.id)
    except AnalysisError as exc:
        return _error_response(exc)


@router.get("/analysis/{analysis_id}")
async def get_analysis(
    analysis_id: str, http_request: Request, actor: Actor = Depends(get_actor)
) -> Any:
    service: AnalysisService = http_request.app.state.analysis_service
    record = await service.get_session_record(analysis_id, owner_id=actor.id)
    if record is None:
        return _not_found_response()

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        return JSONResponse(
            status_code=410,
            content={
                "error": AnalysisErrorBody(
                    code="analysis_expired",
                    message="This analysis session has expired. Analyze the URL again.",
                    retryable=True,
                    analysis_id=record.id,
                ).model_dump()
            },
        )

    result = (
        NormalizedAnalysis.model_validate_json(record.normalized_result_json)
        if record.normalized_result_json
        else None
    )
    error = None
    if record.status == "failed":
        error = AnalysisErrorBody(
            code=record.error_code or "internal_error",
            message=record.error_message or "The analysis failed.",
            retryable=error_retryable(record.error_code or ""),
            analysis_id=record.id,
        )
    return AnalysisSessionResponse(
        analysis_id=record.id,
        status=record.status,
        result=result,
        error=error,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )
