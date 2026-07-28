from supabase import create_client, Client
from src.config import get_settings

def get_supabase_client() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_key)
