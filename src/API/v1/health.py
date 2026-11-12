from fastapi import APIRouter, Depends, Response, status
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import HealthResponse
from src.config import get_settings
from src.Services.token_denylist_service import get_token_denylist_service

router = APIRouter(prefix="/health", tags=["Health"])
logger = get_logger("API.health")


@router.get(
    "/",
    response_model=HealthResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_health_per_minute))],
)
async def health_check(response: Response):
    logger.debug("Entering health_check")
    settings = get_settings()
    denylist = get_token_denylist_service()
    is_ready, msg = await denylist.check_readiness()

    # In production, fail readiness check if Redis connectivity fails
    if not is_ready and getattr(settings, "environment", "").lower() == "production":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.error("Health check readiness failed in production: %s", msg)
        return HealthResponse(status="degraded", environment=settings.environment)

    logger.info("Health check completed: status=ok, environment=%s", settings.environment)
    return HealthResponse(status="ok", environment=settings.environment)

