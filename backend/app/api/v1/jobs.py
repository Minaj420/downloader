from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.dependencies import get_actor
from app.domain.actors import Actor
from app.domain.jobs import JobError
from app.models.jobs import ActivityEvent
from app.repositories import activity
from app.services.job_mapping import job_to_dto
from app.services.job_service import APP_LIBRARY_DESTINATION, JobService
from app.utils.datetime import utc_isoformat

router = APIRouter(tags=["jobs"])


class CreateJobRequest(BaseModel):
    analysis_id: str = Field(min_length=1, max_length=32)
    variant_id: str = Field(min_length=1, max_length=128)
    destination: str = APP_LIBRARY_DESTINATION


def _error_response(exc: JobError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code.value,
                "message": exc.message,
                "retryable": exc.retryable,
                "job_id": exc.job_id,
            }
        },
    )


def _not_found_response() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "job_not_found",
                "message": "This job does not exist.",
                "retryable": False,
                "job_id": None,
            }
        },
    )


@router.post("/jobs")
async def create_job(
    request: CreateJobRequest,
    http_request: Request,
    actor: Actor = Depends(get_actor),
) -> Any:
    service: JobService = http_request.app.state.job_service
    try:
        record = await service.create_job(
            actor,
            analysis_id=request.analysis_id,
            variant_id=request.variant_id,
            destination=request.destination,
        )
    except JobError as exc:
        return _error_response(exc)
    return job_to_dto(record).model_dump(mode="json")


@router.get("/jobs")
async def list_jobs(http_request: Request, actor: Actor = Depends(get_actor)) -> Any:
    service: JobService = http_request.app.state.job_service
    records = await service.list_jobs(actor)
    return {
        "jobs": [job_to_dto(record).model_dump(mode="json") for record in records]
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str, http_request: Request, actor: Actor = Depends(get_actor)
) -> Any:
    service: JobService = http_request.app.state.job_service
    record = await service.get_job(actor, job_id)
    if record is None:
        return _not_found_response()
    return job_to_dto(record).model_dump(mode="json")


def _activity_item(event: ActivityEvent) -> dict[str, Any]:
    """Same field set as the SSE ``activity.created`` payload (events.py)."""
    return {
        "id": event.id,
        "job_id": event.job_id,
        "level": event.level,
        "event_type": event.event_type,
        "message": event.message,
        "created_at": utc_isoformat(event.created_at),
    }


@router.get("/jobs/{job_id}/activity")
async def get_job_activity(
    job_id: str, http_request: Request, actor: Actor = Depends(get_actor)
) -> Any:
    """Full activity history for one job, id ASC.

    Owner-scoped exactly like ``get_job`` (unknown or foreign jobs are
    404). This is the initial history source for the Activity panel; the
    live lane is the SSE stream, whose first connect replays nothing.
    """
    service: JobService = http_request.app.state.job_service
    record = await service.get_job(actor, job_id)
    if record is None:
        return _not_found_response()
    session_factory = http_request.app.state.sessionmaker
    async with session_factory() as session:
        events = await activity.list_events_for_job(session, job_id)
    return {"events": [_activity_item(event) for event in events]}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, http_request: Request, actor: Actor = Depends(get_actor)
) -> Any:
    service: JobService = http_request.app.state.job_service
    try:
        record = await service.cancel_job(actor, job_id)
    except JobError as exc:
        return _error_response(exc)
    return job_to_dto(record).model_dump(mode="json")
