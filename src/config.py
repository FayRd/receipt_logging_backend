from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # Infrastructure Credentials & Services
    supabase_url: str
    supabase_key: str  
    redis_connection_string: str = "redis://localhost:6379"
    gemini_api_key: str = ""
    openai_api_key: str = ""
    logfire_token: str = ""
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
    gemini_vision_model: str = "gemini-3.6-flash" # Gemini vision model name

    # AI Chat Assistant & RAG
    gemini_chat_model: str = "gemini-3.6-flash"    # Gemini chat model name
    rag_recent_receipts_limit: int = 100          # Receipts context window (matches guest limit)
    rag_history_messages_limit: int = 50          # Message turns context window (matches guest limit)
    max_conversations_per_identity: int = 10      # Active conversation hard cap

    # Image Compression & Supabase Storage
    supabase_user_data_bucket: str = "user-data"          # Supabase Storage bucket ID
    max_upload_size_bytes: int = 20 * 1024 * 1024         # 20MB raw upload rejection ceiling
    max_compressed_image_bytes: int = 5 * 1024 * 1024     # 5MB maximum stored image target

    # AI Provider Selection
    # Set AI_PROVIDER to "gemini" (default) or "openrouter".
    # Fallback: if AI_PROVIDER is unset but OPENROUTER_API_KEY is provided while GEMINI_API_KEY is
    # empty, OpenRouter is automatically activated.
    ai_provider: str = "gemini"

    # OpenRouter Configuration
    # Obtain your API key at https://openrouter.ai/keys
    # Vision-capable models: "google/gemini-2.5-flash", "openai/gpt-4o", "anthropic/claude-3.5-haiku"
    # Chat-only models:      "openai/gpt-4o-mini", "meta-llama/llama-3.3-70b-instruct"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_vision_model: str = "google/gemini-2.5-flash"
    openrouter_chat_model: str = "google/gemini-2.5-flash"

    # Rate Limiting Configuration
    rate_limit_enabled: bool = True
    rate_limit_scan_per_minute: int = 5           # Heavy Vision AI parsing
    rate_limit_chat_per_minute: int = 10          # AI Chat & RAG queries
    rate_limit_auth_per_minute: int = 10          # User & Device auth endpoints
    rate_limit_crud_per_minute: int = 60          # Standard session CRUD
    rate_limit_health_per_minute: int = 120       # Health check route

    # SSE Bulk Batch Stream Settings
    sse_poll_interval_seconds: float = 1.0        # Redis poll cadence per loop
    sse_batch_timeout_seconds: int = 300          # Max wait before emitting timeout
    redis_job_ttl_seconds: int = 600              # Redis job and batch expiration TTL (10 minutes)

    # Togglable Logging Configuration
    enable_file_logging: bool = True
    log_file_path: str = "app.log"
    log_level: str = "DEBUG"
    enable_console_logging: bool = True

    @property
    def effective_ai_provider(self) -> str:
        """Return the resolved AI provider ("gemini" or "openrouter").

        Resolution order:
        1. Explicit AI_PROVIDER env var ("gemini" or "openrouter").
        2. Auto-detect: if AI_PROVIDER is unset/empty and only OPENROUTER_API_KEY is populated,
           defaults to "openrouter".
        3. Otherwise defaults to "gemini".
        """
        prov = (self.ai_provider or "").lower().strip()
        if prov == "openrouter":
            return "openrouter"
        if not self.gemini_api_key and self.openrouter_api_key:
            return "openrouter"
        return "gemini"


@lru_cache
def get_settings() -> Settings:
    return Settings()
