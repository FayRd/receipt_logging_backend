"""
Redis OTP Cache Service (Hybrid: Redis + In-Memory Fallback)

Provides secure OTP storage with:
- Salted SHA-256 hashing
- 10-minute expiration (TTL 600s)
- 60-second resend cooldown
- 5-attempt brute-force lockout
- Automatic in-memory TTL dictionary fallback when Redis is unavailable
"""

import hashlib
import secrets
import time
from src.Infrastructure.logger import get_logger

logger = get_logger("Infrastructure.redis_service")

# ── In-Memory Fallback Store ─────────────────────────────────────────────────
# Used when Redis is not available (e.g. local development / unit tests)
_memory_store: dict[str, dict] = {}  # key -> {"value": ..., "expires_at": float}


def _mem_set(key: str, value: dict, ttl_seconds: int) -> None:
    _memory_store[key] = {"value": value, "expires_at": time.monotonic() + ttl_seconds}


def _mem_get(key: str) -> dict | None:
    entry = _memory_store.get(key)
    if entry is None:
        return None
    if time.monotonic() > entry["expires_at"]:
        _memory_store.pop(key, None)
        return None
    return entry["value"]


def _mem_delete(key: str) -> None:
    _memory_store.pop(key, None)


def _mem_exists(key: str) -> bool:
    return _mem_get(key) is not None


# ── Redis Client (lazy-initialized) ─────────────────────────────────────────

_redis_client = None
_redis_available: bool | None = None  # None = not checked yet


def _get_redis():
    """Lazy-initialize Redis client. Returns None if Redis is unavailable."""
    global _redis_client, _redis_available

    if _redis_available is False:
        return None

    if _redis_client is not None:
        return _redis_client

    try:
        import redis as redis_lib  # type: ignore
        from src.config import get_settings
        settings = get_settings()

        client = redis_lib.Redis(
            host=getattr(settings, "redis_host", "localhost"),
            port=getattr(settings, "redis_port", 6379),
            db=0,
            decode_responses=True,
            socket_connect_timeout=2,
        )
        client.ping()  # Confirm connectivity
        _redis_client = client
        _redis_available = True
        logger.info("Redis connected successfully (OTP service)")
        return _redis_client
    except Exception as e:
        _redis_available = False
        logger.warning("Redis not available — falling back to in-memory OTP store: %s", e)
        return None


# ── OTP Hash Utilities ───────────────────────────────────────────────────────

_OTP_SALT = "SancFund_OTP_Salt_2026"


def _hash_otp(otp: str) -> str:
    """Compute a salted SHA-256 hash of the OTP."""
    salted = f"{_OTP_SALT}:{otp}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()


def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit numeric OTP."""
    return f"{secrets.randbelow(1_000_000):06d}"


# ── Public API ───────────────────────────────────────────────────────────────

OTP_TTL = 600       # 10 minutes
COOLDOWN_TTL = 60   # 60 seconds
MAX_ATTEMPTS = 5


def store_otp(user_id: str, target_type: str, identifier: str, otp: str) -> None:
    """Store a hashed OTP for a user's verification target (e.g. 'email')."""
    key = f"verify:otp:{user_id}:{target_type}"
    record = {
        "hash": _hash_otp(otp),
        "attempts": 0,
        "identifier": identifier.strip().lower(),
    }

    r = _get_redis()
    if r:
        import json
        try:
            r.setex(key, OTP_TTL, json.dumps(record))
            logger.debug("OTP stored in Redis: key=%s", key)
            return
        except Exception as e:
            logger.warning("Redis setex failed, falling back to memory: %s", e)

    _mem_set(key, record, OTP_TTL)
    logger.debug("OTP stored in memory: key=%s", key)


def verify_otp(user_id: str, target_type: str, identifier: str, input_otp: str) -> tuple[bool, str | None]:
    """Verify input OTP against the stored hash.

    Returns:
        (True, None) on success.
        (False, error_message) on failure.
    """
    key = f"verify:otp:{user_id}:{target_type}"
    r = _get_redis()

    if r:
        import json
        try:
            raw = r.get(key)
            if raw is None:
                return False, "Verification code has expired or was never issued."
            record = json.loads(raw)
        except Exception as e:
            logger.warning("Redis get failed during OTP verify, falling back to memory: %s", e)
            record = _mem_get(key)
            if record is None:
                return False, "Verification code has expired or was never issued."
    else:
        record = _mem_get(key)
        if record is None:
            return False, "Verification code has expired or was never issued."

    # Enforce brute-force lockout
    attempts = record.get("attempts", 0)
    if attempts >= MAX_ATTEMPTS:
        _delete_otp(key, r)
        return False, "Too many failed attempts. Please request a new verification code."

    # Compare stored identifier
    stored_id = record.get("identifier", "")
    if stored_id != identifier.strip().lower():
        return False, "Email address does not match the one this code was sent to."

    # Constant-time hash comparison
    input_hash = _hash_otp(input_otp.strip())
    if not secrets.compare_digest(input_hash, record["hash"]):
        # Increment attempts
        record["attempts"] = attempts + 1
        _update_record(key, record, r)
        remaining = MAX_ATTEMPTS - record["attempts"]
        if remaining <= 0:
            _delete_otp(key, r)
            return False, "Too many failed attempts. Please request a new verification code."
        return False, f"Incorrect verification code. {remaining} attempt(s) remaining."

    # Success — delete OTP to prevent reuse
    _delete_otp(key, r)
    return True, None


def check_resend_cooldown(user_id: str, target_type: str) -> tuple[bool, int]:
    """Check if the user is within the 60-second resend cooldown.

    Returns:
        (is_in_cooldown: bool, seconds_remaining: int)
    """
    key = f"verify:cooldown:{user_id}:{target_type}"
    r = _get_redis()

    if r:
        try:
            ttl = r.ttl(key)
            if ttl > 0:
                return True, ttl
            return False, 0
        except Exception as e:
            logger.warning("Redis ttl check failed, falling back to memory: %s", e)

    val = _mem_get(key)
    if val is not None:
        entry = _memory_store.get(key)
        remaining = max(0, int(entry["expires_at"] - time.monotonic())) if entry else 0
        return True, remaining
    return False, 0


def set_resend_cooldown(user_id: str, target_type: str) -> None:
    """Set/reset the 60-second resend cooldown for the user."""
    key = f"verify:cooldown:{user_id}:{target_type}"
    r = _get_redis()

    if r:
        try:
            r.setex(key, COOLDOWN_TTL, "1")
            return
        except Exception as e:
            logger.warning("Redis setex cooldown failed, falling back to memory: %s", e)

    _mem_set(key, {"v": 1}, COOLDOWN_TTL)


def delete_otp(user_id: str, target_type: str) -> None:
    """Explicitly delete a stored OTP (e.g. after successful verification)."""
    key = f"verify:otp:{user_id}:{target_type}"
    r = _get_redis()
    _delete_otp(key, r)


# ── Internal Helpers ─────────────────────────────────────────────────────────

def _delete_otp(key: str, r) -> None:
    if r:
        try:
            r.delete(key)
            return
        except Exception as e:
            logger.warning("Redis delete failed, deleting from memory: %s", e)
    _mem_delete(key)


def _update_record(key: str, record: dict, r) -> None:
    if r:
        import json
        try:
            # Preserve remaining TTL
            ttl = r.ttl(key)
            if ttl > 0:
                r.setex(key, ttl, json.dumps(record))
                return
        except Exception as e:
            logger.warning("Redis update record failed, updating memory: %s", e)
    # Update in memory fallback
    entry = _memory_store.get(key)
    if entry:
        entry["value"] = record
