"""Top-level API router: mounts every API version under ``/api``."""

from fastapi import APIRouter

from app.api.v1.router import router as v1_router

api_router = APIRouter(prefix="/api")
api_router.include_router(v1_router)
