from fastapi import APIRouter

from app.api.v1 import analysis, events, health, jobs, library

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(analysis.router)
api_router.include_router(jobs.router)
api_router.include_router(library.router)
api_router.include_router(events.router)
