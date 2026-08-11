import hmac
import hashlib
from src.config import get_settings


def hash_device_token(raw_token: str) -> str:
    """Hash raw device_token with server supabase_key secret using HMAC-SHA256.

    Guarantees that raw device tokens are never stored in plaintext in the database.
    """
    clean_token = raw_token.strip()
    settings = get_settings()
    secret = (getattr(settings, "secret_key", None) or settings.supabase_key).encode("utf-8")
    return hmac.new(
        secret,
        clean_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
