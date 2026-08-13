from fastapi import APIRouter, Depends
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import HealthResponse
from src.config import get_settings

router = APIRouter(prefix="/health", tags=["Health"])
logger = get_logger("API.health")


@router.get(
    "/",
    response_model=HealthResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_health_per_minute))],
)
async def health_check():
    logger.debug("Entering health_check")
    settings = get_settings()
    logger.info("Health check completed: status=ok, environment=%s", settings.environment)
    return HealthResponse(status="ok", environment=settings.environment)

