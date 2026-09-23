from fastapi import APIRouter

from app.schemas.health import HealthResponse

router = APIRouter()


@router.get("/health", summary="Service health check")
async def read_health() -> HealthResponse:
    return HealthResponse(status="ok")
