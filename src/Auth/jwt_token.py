import time
from datetime import datetime, timedelta, timezone
from typing import Any
import jwt
from fastapi import HTTPException, status
from src.config import get_settings
from src.Infrastructure.logger import get_logger

logger = get_logger("Auth.jwt_token")


def _get_jwt_secret() -> str:
    settings = get_settings()
    secret = (
        getattr(settings, "jwt_secret_key", None)
        or getattr(settings, "secret_key", None)
        or settings.data_encryption_key
        or settings.supabase_key
    )
    return secret


def create_access_token(user_id: str, username: str, expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT access token for an authenticated user."""
    settings = get_settings()
    secret = _get_jwt_secret()
    algorithm = getattr(settings, "jwt_algorithm", "HS256")
    expire_minutes = getattr(settings, "jwt_access_token_expire_minutes", 15)

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)

    payload: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "type": "access",
        "iat": datetime.now(timezone.utc),
        "exp": expire,
    }
    encoded_jwt = jwt.encode(payload, secret, algorithm=algorithm)
    logger.debug("Issued JWT access token for user_id=%s, expires=%s", user_id, expire.isoformat())
    return encoded_jwt


def create_refresh_token(user_id: str, username: str, expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT refresh token with longer TTL for session renewal."""
    settings = get_settings()
    secret = _get_jwt_secret()
    algorithm = getattr(settings, "jwt_algorithm", "HS256")
    expire_days = getattr(settings, "jwt_refresh_token_expire_days", 30)

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(days=expire_days)

    payload: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "type": "refresh",
        "iat": datetime.now(timezone.utc),
        "exp": expire,
    }
    encoded_jwt = jwt.encode(payload, secret, algorithm=algorithm)
    logger.debug("Issued JWT refresh token for user_id=%s, expires=%s", user_id, expire.isoformat())
    return encoded_jwt


def verify_jwt_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """Verify and decode a JWT token string, enforcing signature and claims."""
    settings = get_settings()
    secret = _get_jwt_secret()
    algorithm = getattr(settings, "jwt_algorithm", "HS256")

    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
        token_type = payload.get("type")
        if token_type != expected_type:
            logger.warning("JWT token type mismatch: expected '%s', got '%s'", expected_type, token_type)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token type. Expected {expected_type} token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token payload missing subject identifier.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("JWT token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired. Please refresh your session.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("Invalid JWT token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
