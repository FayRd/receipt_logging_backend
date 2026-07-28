from fastapi import APIRouter
from src.Models.schemas import HealthResponse
from src.config import get_settings

router = APIRouter(prefix="/health", tags=["Health"])

@router.get("/", response_model=HealthResponse)
async def health_check():
    settings = get_settings()
    return HealthResponse(status="ok", environment=settings.environment)
