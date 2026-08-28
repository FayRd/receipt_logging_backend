"""
Quota Service: Tier-Based Daily Usage Tracking & Enforcement

Tracks daily usage for /scan/* (scans/day) and /chat/query (tokens/day) based on user tiers.
- Free Tier (and Guests): 10 scans/day, 10k tokens/day
- Premium Tier: 50 scans/day, 50k tokens/day
- Dev Tier: Unlimited (-1)
- Resets daily at 00:00 UTC
- Scan and Chat token quotas are completely independent.
- Atomic Redis counters backed by an in-memory TTL dictionary fallback.
"""

from datetime import datetime, timezone, timedelta
import time
from src.config import get_settings
from src.Infrastructure.logger import get_logger
from src.Auth.identity import Identity

logger = get_logger("Services.quota_service")

# ── In-Memory Fallback Store ─────────────────────────────────────────────────
# key -> {"used": int, "expires_at": float}
_memory_quota_store: dict[str, dict] = {}


def _mem_get_used(key: str) -> int:
    entry = _memory_quota_store.get(key)
    if not entry:
        return 0
    if time.time() > entry["expires_at"]:
        _memory_quota_store.pop(key, None)
        return 0
    return entry["used"]


def _mem_incr_by(key: str, amount: int, ttl_seconds: int) -> int:
    now = time.time()
    entry = _memory_quota_store.get(key)
    if not entry or now > entry["expires_at"]:
        new_val = amount
        _memory_quota_store[key] = {"used": new_val, "expires_at": now + ttl_seconds}
        return new_val
    else:
        entry["used"] += amount
        return entry["used"]


def reset_quota_store_for_testing() -> None:
    """Clear in-memory quota dictionary for testing."""
    _memory_quota_store.clear()


# ── Redis Client Helper ──────────────────────────────────────────────────────

_redis_client = None
_redis_available: bool | None = None


def _get_redis():
    """Lazy-initialize Redis client for quota operations."""
    global _redis_client, _redis_available
    if _redis_available is False:
        return None
    if _redis_client is not None:
        return _redis_client

    try:
        import redis as redis_lib  # type: ignore
        settings = get_settings()
        client = redis_lib.Redis(
            host=getattr(settings, "redis_host", "localhost"),
            port=getattr(settings, "redis_port", 6379),
            db=0,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        client.ping()
        _redis_client = client
        _redis_available = True
        logger.info("Redis connected for QuotaService")
        return _redis_client
    except Exception as e:
        _redis_available = False
        logger.warning("Redis not available for QuotaService — using in-memory store: %s", e)
        return None


# ── Core Quota Logic ─────────────────────────────────────────────────────────

class QuotaService:
    def __init__(self):
        self.settings = get_settings()

    def _get_identity_key(self, identity: Identity) -> str:
        if identity.is_authenticated and identity.user_id:
            return f"user:{identity.user_id}"
        device_id = identity.device_id or "anonymous"
        return f"device:{device_id}"

    async def get_identity_tier(self, identity: Identity, user_repo=None) -> str:
        """Resolve the user's tier in lowercase ('free', 'premium', 'dev')."""
        if not identity.is_authenticated or not identity.user_id:
            return "free"

        if user_repo:
            try:
                user = await user_repo.get_by_id(identity.user_id)
                if user and user.get("tier"):
                    return str(user["tier"]).strip().lower()
            except Exception as e:
                logger.warning("Failed to fetch user tier for user_id=%s: %s", identity.user_id, e)

        return "free"

    def _get_utc_reset_window(self) -> tuple[int, str, str]:
        """Compute seconds until next 00:00:00 UTC, ISO reset timestamp, and date string."""
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_to_reset = max(1, int((tomorrow - now).total_seconds()))
        reset_at_iso = tomorrow.isoformat()
        return seconds_to_reset, reset_at_iso, date_str

    def format_countdown(self, seconds: int) -> str:
        """Format seconds into human-readable '{hours}h {mins}m'."""
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}h {mins}m"

    def _get_tier_limits(self, tier: str) -> tuple[int, int]:
        """Return (max_scans_per_day, max_chat_tokens_per_day) for the given tier."""
        tier_cfg = self.settings.tier_quotas.get(tier.lower())
        if not tier_cfg:
            tier_cfg = self.settings.tier_quotas.get("free", {"max_scans_per_day": 10, "max_chat_tokens_per_day": 10_000})
        max_scans = tier_cfg.get("max_scans_per_day", 10)
        max_chat_tokens = tier_cfg.get("max_chat_tokens_per_day", 10_000)
        return max_scans, max_chat_tokens

    async def _get_used_count(self, key: str) -> int:
        r = _get_redis()
        if r:
            try:
                val = r.get(key)
                return int(val) if val else 0
            except Exception:
                pass
        return _mem_get_used(key)

    async def _incr_count(self, key: str, amount: int, ttl_seconds: int) -> int:
        r = _get_redis()
        if r:
            try:
                pipe = r.pipeline()
                pipe.incrby(key, amount)
                pipe.expire(key, ttl_seconds)
                results = pipe.execute()
                return int(results[0])
            except Exception:
                pass
        return _mem_incr_by(key, amount, ttl_seconds)

    async def get_quota_status(self, identity: Identity, user_repo=None) -> dict:
        """Fetch real-time scan and chat quota metrics for caller."""
        tier = await self.get_identity_tier(identity, user_repo)
        max_scans, max_chat_tokens = self._get_tier_limits(tier)
        seconds_to_reset, reset_at_iso, date_str = self._get_utc_reset_window()
        countdown_str = self.format_countdown(seconds_to_reset)

        id_key = self._get_identity_key(identity)
        scan_redis_key = f"quota:scan:{date_str}:{id_key}"
        chat_redis_key = f"quota:chat:{date_str}:{id_key}"

        used_scans = await self._get_used_count(scan_redis_key)
        used_chat_tokens = await self._get_used_count(chat_redis_key)

        # Calculate scan metrics
        if max_scans == -1:
            remaining_scans = -1
            is_scan_exhausted = False
        else:
            remaining_scans = max(0, max_scans - used_scans)
            is_scan_exhausted = used_scans >= max_scans

        # Calculate chat metrics
        if max_chat_tokens == -1:
            remaining_chat_tokens = -1
            is_chat_exhausted = False
        else:
            remaining_chat_tokens = max(0, max_chat_tokens - used_chat_tokens)
            is_chat_exhausted = used_chat_tokens >= max_chat_tokens

        return {
            "tier": tier,
            "scan": {
                "used": used_scans,
                "limit": max_scans,
                "remaining": remaining_scans,
                "is_exhausted": is_scan_exhausted,
            },
            "chat": {
                "used": used_chat_tokens,
                "limit": max_chat_tokens,
                "remaining": remaining_chat_tokens,
                "is_exhausted": is_chat_exhausted,
            },
            "reset_at": reset_at_iso,
            "seconds_to_reset": seconds_to_reset,
            "reset_countdown": countdown_str,
        }

    async def check_scan_quota(self, identity: Identity, count: int = 1, user_repo=None) -> tuple[bool, dict, str]:
        """Check if caller has sufficient scan quota for `count` files.

        Returns (allowed: bool, status: dict, error_message: str).
        """
        status = await self.get_quota_status(identity, user_repo)
        max_scans = status["scan"]["limit"]
        used_scans = status["scan"]["used"]

        if max_scans == -1:
            return True, status, ""

        if used_scans + count > max_scans:
            countdown = status["reset_countdown"]
            limit_str = str(max_scans)
            used_str = str(used_scans)
            err = f"Daily scan quota reached ({used_str}/{limit_str}). Resets in {countdown} at 00:00 UTC"
            return False, status, err

        return True, status, ""

    async def consume_scan_quota(self, identity: Identity, count: int = 1, user_repo=None) -> dict:
        """Atomically increment scan usage by `count` and return updated status."""
        seconds_to_reset, _, date_str = self._get_utc_reset_window()
        id_key = self._get_identity_key(identity)
        scan_redis_key = f"quota:scan:{date_str}:{id_key}"

        await self._incr_count(scan_redis_key, count, seconds_to_reset)
        return await self.get_quota_status(identity, user_repo)

    async def check_chat_quota(self, identity: Identity, user_repo=None) -> tuple[bool, dict, str]:
        """Check if caller has remaining chat token quota before query execution."""
        status = await self.get_quota_status(identity, user_repo)
        max_tokens = status["chat"]["limit"]
        used_tokens = status["chat"]["used"]

        if max_tokens == -1:
            return True, status, ""

        if used_tokens >= max_tokens:
            countdown = status["reset_countdown"]
            limit_str = f"{max_tokens // 1000}k" if max_tokens >= 1000 else str(max_tokens)
            used_str = f"{used_tokens // 1000}k" if used_tokens >= 1000 else str(used_tokens)
            err = f"Daily chat token quota reached ({used_str}/{limit_str}). Resets in {countdown} at 00:00 UTC"
            return False, status, err

        return True, status, ""

    async def consume_chat_quota(self, identity: Identity, tokens: int, user_repo=None) -> dict:
        """Atomically increment chat token usage by `tokens` and return updated status."""
        if tokens <= 0:
            return await self.get_quota_status(identity, user_repo)

        seconds_to_reset, _, date_str = self._get_utc_reset_window()
        id_key = self._get_identity_key(identity)
        chat_redis_key = f"quota:chat:{date_str}:{id_key}"

        await self._incr_count(chat_redis_key, tokens, seconds_to_reset)
        return await self.get_quota_status(identity, user_repo)


_quota_service_instance = QuotaService()


def get_quota_service() -> QuotaService:
    return _quota_service_instance
