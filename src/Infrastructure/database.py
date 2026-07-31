from supabase import acreate_client, AsyncClient
from src.config import get_settings


async def get_supabase_client() -> AsyncClient:
    """FastAPI async dependency that yields a Supabase AsyncClient per request."""
    settings = get_settings()
    return await acreate_client(settings.supabase_url, settings.supabase_key)
