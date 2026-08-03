import asyncio
import time
from collections import defaultdict
from fastapi import HTTPException, Request, status
from src.config import get_settings


class SlidingWindowRateLimiter:
    """In-memory Sliding Window rate limiter keyed by client identity or IP."""

    def __init__(self):
        # Storage: dict mapping key string -> list of float timestamps
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self, key: str, max_requests: int, window_seconds: int = 60
    ) -> tuple[bool, int, int]:
        """Check if request key is within rate limits over rolling window_seconds.

        Returns:
            (is_allowed, remaining_requests, retry_after_seconds)
        """
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return True, max_requests, 0

        now = time.time()
        window_start = now - window_seconds

        async with self._lock:
            # Purge timestamps outside the rolling window
            timestamps = [ts for ts in self._requests[key] if ts > window_start]
            self._requests[key] = timestamps

            if len(timestamps) >= max_requests:
                # Calculate retry_after seconds until oldest request in window expires
                oldest_timestamp = timestamps[0]
                retry_after = int(oldest_timestamp + window_seconds - now) + 1
                return False, 0, max(1, retry_after)

            # Record current request timestamp
            self._requests[key].append(now)
            remaining = max_requests - len(self._requests[key])
            return True, remaining, 0

    async def reset(self):
        """Clear all stored rate limit windows (used in unit tests)."""
        async with self._lock:
            self._requests.clear()


# Global singleton rate limiter instance
limiter = SlidingWindowRateLimiter()


def rate_limit(max_requests_getter, window_seconds: int = 60):
    """FastAPI dependency factory enforcing rate limits on route handlers.

    Args:
        max_requests_getter: Int limit or callable taking Settings -> int limit.
        window_seconds: Rolling window duration in seconds (default: 60).
    """

    async def dependency(request: Request):
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return

        max_requests = (
            max_requests_getter(settings)
            if callable(max_requests_getter)
            else max_requests_getter
        )

        # Derive rate limiting key: X-Device-ID header if present, else client host IP
        device_id = request.headers.get("X-Device-ID", "").strip()
        client_ip = request.client.host if request.client else "127.0.0.1"
        key_identifier = device_id if device_id else client_ip
        key = f"{request.url.path}:{key_identifier}"

        is_allowed, remaining, retry_after = await limiter.check_rate_limit(
            key=key, max_requests=max_requests, window_seconds=window_seconds
        )

        if not is_allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Please try again in {retry_after} seconds.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                },
            )

    return dependency
