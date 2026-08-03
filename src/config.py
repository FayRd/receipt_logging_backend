from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # Cloud & API Credentials
    supabase_url: str
    supabase_key: str  
    gemini_api_key: str = ""
    openai_api_key: str = ""
    environment: str = "development"

    # CORS Configuration
    allowed_origins: list[str] = [
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:8085",
        "http://127.0.0.1",
        "http://127.0.0.1:8085",
    ]

    # Scanning & AI Vision Extraction
    max_image_size_bytes: int = 10 * 1024 * 1024  # 10 MB file ceiling
    confidence_threshold: float = 0.8             # Validation confidence threshold
    gemini_vision_model: str = "gemini-3.6-flash" # Vision model name

    # AI Chat Assistant & RAG
    gemini_chat_model: str = "gemini-3.6-flash"    # Chat model name
    rag_recent_receipts_limit: int = 30           # Receipts context window
    rag_history_messages_limit: int = 10          # Message turns context window
    max_conversations_per_identity: int = 10      # Active conversation hard cap

    # Rate Limiting Configuration
    rate_limit_enabled: bool = True
    rate_limit_scan_per_minute: int = 5           # Heavy Vision AI parsing
    rate_limit_chat_per_minute: int = 10          # AI Chat & RAG queries
    rate_limit_auth_per_minute: int = 10          # User & Device auth endpoints
    rate_limit_crud_per_minute: int = 60          # Standard session CRUD
    rate_limit_health_per_minute: int = 120       # Health check route


@lru_cache
def get_settings() -> Settings:
    return Settings()
