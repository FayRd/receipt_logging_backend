import asyncio
from supabase import acreate_client, AsyncClient
from src.config import get_settings
from src.Infrastructure.logger import get_logger

logger = get_logger("Infrastructure.database")

_supabase_async_client: AsyncClient | None = None
_client_lock = asyncio.Lock()


async def get_supabase_client() -> AsyncClient:
    """FastAPI async dependency that returns a pooled, shared Supabase AsyncClient singleton."""
    global _supabase_async_client
    if _supabase_async_client is not None:
        return _supabase_async_client

    async with _client_lock:
        if _supabase_async_client is not None:
            return _supabase_async_client
        settings = get_settings()
        logger.info("Initializing shared Supabase async client for URL: %s", settings.supabase_url)
        _supabase_async_client = await acreate_client(settings.supabase_url, settings.supabase_key)
        logger.info("Shared Supabase async client initialized successfully")
        return _supabase_async_client


async def close_supabase_client() -> None:
    """Gracefully cleans up the shared Supabase client during app shutdown."""
    global _supabase_async_client
    async with _client_lock:
        _supabase_async_client = None
        logger.info("Shared Supabase async client reset.")

