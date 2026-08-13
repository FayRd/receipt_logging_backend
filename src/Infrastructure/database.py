from supabase import acreate_client, AsyncClient
from src.config import get_settings
from src.Infrastructure.logger import get_logger

logger = get_logger("Infrastructure.database")


async def get_supabase_client() -> AsyncClient:
    """FastAPI async dependency that yields a Supabase AsyncClient per request."""
    settings = get_settings()
    logger.debug("Initializing Supabase async client for URL: %s", settings.supabase_url)
    client = await acreate_client(settings.supabase_url, settings.supabase_key)
    logger.debug("Supabase async client initialized successfully")
    return client
