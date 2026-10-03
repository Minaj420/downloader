from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import api_router
from app.config import Settings, get_settings
from app.database import create_engine_and_sessionmaker, run_migrations
from app.integrations.ffmpeg import FFmpegProcessor
from app.integrations.ytdlp import YtDlpAnalyzer, YtDlpDownloader
from app.services.analysis_service import AnalysisService, URLAnalyzer
from app.services.event_bus import EventBus
from app.services.job_runner import RunnerDeps
from app.services.job_service import JobService
from app.services.library_service import LibraryService
from app.services.queue_manager import QueueManager
from app.services.storage_service import StorageService


def create_app(settings: Settings | None = None, analyzer: URLAnalyzer | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    engine, session_factory = create_engine_and_sessionmaker(resolved_settings.database_url)
    storage = StorageService(
        resolved_settings.staging_dir,
        resolved_settings.library_dir,
        plex_music_root=resolved_settings.plex_music_dir,
        plex_video_root=resolved_settings.plex_video_dir,
    )
    bus = EventBus()
    queue_manager = QueueManager(
        session_factory=session_factory,
        deps=RunnerDeps(
            session_factory=session_factory,
            downloader=YtDlpDownloader(
                binary=resolved_settings.yt_dlp_binary,
                timeout_seconds=resolved_settings.download_timeout_seconds,
            ),
            ffmpeg=FFmpegProcessor(
                binary=resolved_settings.ffmpeg_binary,
                timeout_seconds=resolved_settings.ffmpeg_timeout_seconds,
            ),
            storage=storage,
            bus=bus,
            progress_min_interval=resolved_settings.progress_min_interval_seconds,
        ),
        concurrency=resolved_settings.download_concurrency,
        poll_interval_seconds=resolved_settings.queue_poll_interval_seconds,
    )
    job_service = JobService(session_factory, queue_manager, bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for directory in (
            resolved_settings.state_dir,
            resolved_settings.staging_dir,
            resolved_settings.library_dir,
        ):
            Path(directory).mkdir(parents=True, exist_ok=True)
        storage.ensure_layout()
        # Capture delete-root identities after the layout exists and
        # before migrations/queue start: deletions trust only these
        # roots, fail-closed against anything captured later.
        storage.initialize_delete_roots()
        run_migrations(resolved_settings.sync_database_url)
        await queue_manager.start()
        try:
            yield
        finally:
            await queue_manager.stop()
            await app.state.engine.dispose()

    app = FastAPI(title="Downloader", version="0.1.0", lifespan=lifespan)

    app.state.engine = engine
    app.state.sessionmaker = session_factory
    app.state.event_bus = bus
    app.state.queue_manager = queue_manager
    app.state.job_service = job_service
    app.state.library_service = LibraryService(session_factory, storage)

    app.state.analysis_service = AnalysisService(
        analyzer=analyzer
        or YtDlpAnalyzer(
            binary=resolved_settings.yt_dlp_binary,
            timeout_seconds=resolved_settings.analysis_timeout_seconds,
        ),
        session_factory=session_factory,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del exc
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request payload is invalid.",
                    "retryable": False,
                    "analysis_id": None,
                }
            },
        )

    app.include_router(api_router, prefix="/api/v1")

    _mount_frontend(app, resolved_settings.frontend_dist_dir)
    return app


def _mount_frontend(app: FastAPI, dist_dir: Path | None) -> None:
    if dist_dir is None or not (dist_dir / "index.html").is_file():
        return

    dist_root = dist_dir.resolve()
    assets_dir = dist_root / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        if full_path:
            candidate = (dist_root / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist_root):
                return FileResponse(candidate)
        return FileResponse(dist_root / "index.html")


app = create_app()
