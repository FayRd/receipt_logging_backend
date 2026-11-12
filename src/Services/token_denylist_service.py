"""TokenDenylistService: High-performance distributed JWT revocation store.

Provides O(1) token and user revocation checks backed by Redis with
automatic in-memory TTL dictionary fallback for test/dev environments.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any
import redis.asyncio as aioredis
from src.Infrastructure.logger import get_logger
from src.config import get_settings

logger = get_logger("Services.token_denylist_service")

# Clock skew allowance in seconds
CLOCK_SKEW_SECONDS = 60

# In-memory fallback dictionary: key -> {"value": str, "expires_at": float}
_memory_denylist: dict[str, dict[str, Any]] = {}


def _cleanup_memory_denylist() -> None:
    """Purge expired entries from in-memory fallback store."""
    now = time.monotonic()
    expired_keys = [k for k, v in _memory_denylist.items() if now > v["expires_at"]]
    for k in expired_keys:
        _memory_denylist.pop(k, None)


def clear_memory_denylist() -> None:
    """Clear all entries from in-memory fallback store (primarily for unit test isolation)."""
    _memory_denylist.clear()


class TokenDenylistService:
    """Active JWT revocation denylist manager."""

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
        self._redis = redis_client
        self._redis_checked = False if redis_client is None else True

    async def _get_redis(self) -> aioredis.Redis | None:
        """Resolve active async Redis client, initializing if necessary."""
        if self._redis is not None:
            return self._redis

        settings = get_settings()
        conn_str = (settings.redis_connection_string or "").strip()
        if not conn_str or not (conn_str.startswith("redis://") or conn_str.startswith("rediss://")):
            if getattr(settings, "environment", "").lower() == "production":
                logger.critical(
                    "PRODUCTION MISCONFIGURATION: Redis connection string is empty or invalid in production! "
                    "Multi-worker clusters cannot share token revocation state."
                )
            return None

        try:
            client = aioredis.from_url(
                conn_str,
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await client.ping()
            self._redis = client
            return self._redis
        except Exception as exc:
            if getattr(settings, "environment", "").lower() == "production":
                logger.critical(
                    "PRODUCTION READINESS FAILURE: Redis unavailable for TokenDenylistService in production (%s)! "
                    "Multi-worker Uvicorn clusters will operate on partitioned local memory denylists.",
                    exc,
                )
            else:
                logger.warning("Redis unavailable for TokenDenylistService; falling back to memory: %s", exc)
            return None

    async def revoke_token(
        self,
        jti: str,
        exp: float | int | datetime,
        user_id: str | None = None,
        reason: str = "revoked",
    ) -> None:
        """Denylist an individual JWT by JTI until its expiration timestamp."""
        if not jti:
            return

        now_ts = time.time()
        if isinstance(exp, datetime):
            exp_ts = exp.timestamp()
        else:
            exp_ts = float(exp)

        ttl = max(0, int(exp_ts - now_ts)) + CLOCK_SKEW_SECONDS
        if ttl <= 0:
            return  # Token already expired

        key = f"denylist:jti:{jti}"
        payload = json.dumps({
            "jti": jti,
            "user_id": user_id,
            "revoked_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        })

        redis = await self._get_redis()
        if redis:
            try:
                await redis.setex(key, ttl, payload)
                logger.info("Revoked JTI in Redis: key=%s (ttl=%ds)", key, ttl)
                return
            except Exception as exc:
                logger.warning("Failed to write JTI revocation to Redis (%s); falling back to memory", exc)

        _cleanup_memory_denylist()
        _memory_denylist[key] = {
            "value": payload,
            "expires_at": time.monotonic() + ttl,
        }
        logger.info("Revoked JTI in memory: key=%s (ttl=%ds)", key, ttl)

    async def revoke_user(
        self,
        user_id: str,
        ttl_seconds: int | None = None,
        reason: str = "account_deleted",
    ) -> None:
        """Denylist all active and future tokens for a user ID post-deletion."""
        if not user_id:
            return

        settings = get_settings()
        if ttl_seconds is None:
            # Default TTL: maximum refresh token expiration (e.g. 30 days) + clock skew
            refresh_days = getattr(settings, "jwt_refresh_token_expire_days", 30)
            ttl_seconds = int(refresh_days * 86400) + CLOCK_SKEW_SECONDS

        key = f"denylist:user:{user_id}"
        payload = json.dumps({
            "user_id": user_id,
            "revoked_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        })

        redis = await self._get_redis()
        if redis:
            try:
                await redis.setex(key, ttl_seconds, payload)
                logger.info("Revoked User ID in Redis: key=%s (ttl=%ds)", key, ttl_seconds)
                return
            except Exception as exc:
                logger.warning("Failed to write User revocation to Redis (%s); falling back to memory", exc)

        _cleanup_memory_denylist()
        _memory_denylist[key] = {
            "value": payload,
            "expires_at": time.monotonic() + ttl_seconds,
        }
        logger.info("Revoked User ID in memory: key=%s (ttl=%ds)", key, ttl_seconds)

    async def is_revoked(self, jti: str | None = None, user_id: str | None = None) -> bool:
        """Check whether a specific JTI or user ID has been denylisted."""
        _cleanup_memory_denylist()

        keys_to_check: list[str] = []
        if jti:
            keys_to_check.append(f"denylist:jti:{jti}")
        if user_id:
            keys_to_check.append(f"denylist:user:{user_id}")

        if not keys_to_check:
            return False

        redis = await self._get_redis()
        if redis:
            try:
                for key in keys_to_check:
                    if await redis.exists(key):
                        logger.debug("Denylist HIT in Redis for key=%s", key)
                        return True
                return False
            except Exception as exc:
                logger.warning("Redis query error in is_revoked (%s); checking memory fallback", exc)

        now_mono = time.monotonic()
        for key in keys_to_check:
            entry = _memory_denylist.get(key)
            if entry and now_mono <= entry["expires_at"]:
                logger.debug("Denylist HIT in memory for key=%s", key)
                return True

        return False

    async def check_readiness(self) -> tuple[bool, str]:
        """Verify Redis connectivity for readiness probes."""
        settings = get_settings()
        is_prod = getattr(settings, "environment", "").lower() == "production"
        redis = await self._get_redis()
        if redis is not None:
            try:
                await redis.ping()
                return True, "Redis denylist connected"
            except Exception as exc:
                msg = f"Redis ping failed: {exc}"
                if is_prod:
                    logger.critical("PRODUCTION READINESS FAILURE: %s", msg)
                    return False, msg
                return True, f"Memory fallback active ({msg})"

        if is_prod:
            msg = "Redis is unavailable in production environment"
            logger.critical("PRODUCTION READINESS FAILURE: %s", msg)
            return False, msg

        return True, "Memory fallback active (non-production)"


# Global singleton instance
_token_denylist_service: TokenDenylistService | None = None


def get_token_denylist_service() -> TokenDenylistService:
    """Return the global TokenDenylistService singleton instance."""
    global _token_denylist_service
    if _token_denylist_service is None:
        _token_denylist_service = TokenDenylistService()
    return _token_denylist_service


def set_token_denylist_service(service: TokenDenylistService | None) -> None:
    """Set or reset the global TokenDenylistService singleton instance (for testing)."""
    global _token_denylist_service
    _token_denylist_service = service
