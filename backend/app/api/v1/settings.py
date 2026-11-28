"""Read-only Settings (M7-S1; PRODUCT_SPEC.md §23, ARCHITECTURE.md §47).

Reports the effective values of the ``Settings`` object this application was
created with, which destinations are configured, and the startup storage
write check stored at startup. It never runs a new check, touches the
filesystem or the database, and never returns App Library/staging/Plex root
paths, other configuration or raw error text. Format and quality defaults are
browser-local (last selection) and therefore not part of this contract.
"""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.services.job_service import APP_LIBRARY_DESTINATION, SKIP_EXISTING_POLICY
from app.services.storage_service import StartupProbeResult

router = APIRouter(tags=["settings"])

DestinationTarget = Literal["app_library", "plex_music", "plex_video"]
ProbeTarget = Literal["staging", "app_library", "plex_music", "plex_video"]


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DestinationSettings(_ResponseModel):
    target: DestinationTarget
    configured: bool
    # Plex is not selectable for downloads yet, even when configured.
    selectable: bool
    # Explicitly configured display path of a configured Plex root only.
    display_path: str | None


class StartupProbeEntry(_ResponseModel):
    target: ProbeTarget
    ok: bool
    reason: str | None


class StartupStorageCheck(_ResponseModel):
    status: Literal["completed", "unavailable"]
    results: list[StartupProbeEntry]


class SettingsResponse(_ResponseModel):
    max_concurrent_jobs: int
    # The job-creation defaults themselves (job_service constants).
    default_destination: str
    conflict_policy: str
    destinations: list[DestinationSettings]
    startup_storage_check: StartupStorageCheck


def _effective_concurrency(settings: Settings) -> int:
    # Mirrors QueueManager's clamp (``max(1, concurrency)``).
    return max(1, settings.download_concurrency)


def _plex_destination(
    target: DestinationTarget, root: Path | None, display_path: str | None
) -> DestinationSettings:
    configured = root is not None
    # Only an explicitly set display path; the raw root is never returned.
    explicit = display_path is not None and display_path.strip() != ""
    shown = display_path if configured and explicit else None
    return DestinationSettings(
        target=target, configured=configured, selectable=False, display_path=shown
    )


def _startup_check(snapshot: tuple[StartupProbeResult, ...] | None) -> StartupStorageCheck:
    if snapshot is None:
        return StartupStorageCheck(status="unavailable", results=[])
    return StartupStorageCheck(
        status="completed",
        results=[
            StartupProbeEntry.model_validate(
                {"target": item.target, "ok": item.ok, "reason": item.reason}
            )
            for item in snapshot
        ],
    )


@router.get("/settings", response_model=SettingsResponse)
async def get_app_settings(request: Request) -> SettingsResponse:
    settings: Settings = request.app.state.settings
    snapshot = getattr(request.app.state, "startup_storage_probe", None)
    return SettingsResponse(
        max_concurrent_jobs=_effective_concurrency(settings),
        default_destination=APP_LIBRARY_DESTINATION,
        conflict_policy=SKIP_EXISTING_POLICY,
        destinations=[
            DestinationSettings(
                target="app_library", configured=True, selectable=True, display_path=None
            ),
            _plex_destination(
                "plex_music", settings.plex_music_dir, settings.plex_music_display_path
            ),
            _plex_destination(
                "plex_video", settings.plex_video_dir, settings.plex_video_display_path
            ),
        ],
        startup_storage_check=_startup_check(snapshot),
    )
