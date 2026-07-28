from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    supabase_url: str
    supabase_key: str  
    gemini_api_key: str = ""
    openai_api_key: str = ""
    environment: str = "development"
    allowed_origins: list[str] = ["*"]
    
    class Config:
        env_file = ".env"
        case_sensitive = False

@lru_cache
def get_settings() -> Settings:
    return Settings()
