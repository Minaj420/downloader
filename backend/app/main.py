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
from app.integrations.ytdlp import YtDlpAnalyzer
from app.services.analysis_service import AnalysisService, URLAnalyzer


def create_app(settings: Settings | None = None, analyzer: URLAnalyzer | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for directory in (
            resolved_settings.state_dir,
            resolved_settings.staging_dir,
            resolved_settings.library_dir,
        ):
            Path(directory).mkdir(parents=True, exist_ok=True)
        run_migrations(resolved_settings.sync_database_url)
        yield
        await app.state.engine.dispose()

    app = FastAPI(title="Downloader", version="0.1.0", lifespan=lifespan)

    engine, session_factory = create_engine_and_sessionmaker(resolved_settings.database_url)
    app.state.engine = engine
    app.state.sessionmaker = session_factory

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
