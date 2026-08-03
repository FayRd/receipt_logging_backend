from fastapi import APIRouter, Depends
from src.Auth.rate_limiter import rate_limit
from src.Models.schemas import HealthResponse
from src.config import get_settings

router = APIRouter(prefix="/health", tags=["Health"])


@router.get(
    "/",
    response_model=HealthResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_health_per_minute))],
)
async def health_check():
    settings = get_settings()
    return HealthResponse(status="ok", environment=settings.environment)
