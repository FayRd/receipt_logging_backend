"""Unit tests for Active JWT Revocation and TokenDenylistService (TODO-33).

Validates:
- UT-01: 128-bit CSPRNG JTI entropy and uniqueness across access and refresh tokens.
- UT-02: Token-level revocation and TTL eviction in memory store.
- UT-03: User-level revocation superseding individual token JTIs.
- UT-04: Graceful fallback to in-memory store during Redis connectivity outages.
- UT-05: Clock skew leeway computation (CLOCK_SKEW_SECONDS = 60).
- Production readiness check behavior under Redis offline conditions.
- Rejection of revoked tokens in verify_jwt_token_async.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi import HTTPException

from src.Auth.jwt_token import (
    create_access_token,
    create_refresh_token,
    create_2fa_temp_token,
    verify_jwt_token,
    verify_jwt_token_async,
    _get_jwt_secret,
)
from src.Services.token_denylist_service import (
    TokenDenylistService,
    CLOCK_SKEW_SECONDS,
    clear_memory_denylist,
    set_token_denylist_service,
    get_token_denylist_service,
)
from src.config import get_settings


@pytest.fixture(autouse=True)
def clean_denylist():
    """Reset memory denylist before and after every unit test."""
    clear_memory_denylist()
    yield
    clear_memory_denylist()


class TestTokenDenylistUnit:
    """UT-01 through UT-05 unit test suite for JWT revocation."""

    def test_jwt_jti_entropy_uniqueness(self):
        """UT-01: Generate 1,000 tokens and ensure 100% unique 128-bit hex JTIs."""
        secret = _get_jwt_secret()
        settings = get_settings()
        algorithm = getattr(settings, "jwt_algorithm", "HS256")

        access_jtis = set()
        refresh_jtis = set()
        temp_jtis = set()

        for i in range(500):
            acc_tok = create_access_token(user_id=f"user_{i}", username=f"user_{i}")
            ref_tok = create_refresh_token(user_id=f"user_{i}", username=f"user_{i}")

            payload_acc = jwt.decode(acc_tok, secret, algorithms=[algorithm])
            payload_ref = jwt.decode(ref_tok, secret, algorithms=[algorithm])

            assert "jti" in payload_acc, "Access token missing jti claim"
            assert "jti" in payload_ref, "Refresh token missing jti claim"
            assert len(payload_acc["jti"]) == 32, "JTI must be a 32-character hex UUID4 string"
            assert len(payload_ref["jti"]) == 32, "JTI must be a 32-character hex UUID4 string"

            access_jtis.add(payload_acc["jti"])
            refresh_jtis.add(payload_ref["jti"])

        assert len(access_jtis) == 500, "All 500 access token JTIs must be globally unique"
        assert len(refresh_jtis) == 500, "All 500 refresh token JTIs must be globally unique"
        assert access_jtis.isdisjoint(refresh_jtis), "Access and refresh token JTIs must never collide"

        # Also test 2FA token JTI
        temp_tok = create_2fa_temp_token(user_id="user_2fa", username="user_2fa")
        payload_temp = jwt.decode(temp_tok, secret, algorithms=[algorithm])
        assert "jti" in payload_temp
        assert len(payload_temp["jti"]) == 32

    @pytest.mark.anyio
    async def test_token_revocation_in_memory(self):
        """UT-02: Revoke token with TTL, check is_revoked, verify expiration after TTL in memory fallback."""
        service = TokenDenylistService()
        # Force memory fallback to test memory store logic directly
        service._get_redis = AsyncMock(return_value=None)
        test_jti = uuid.uuid4().hex
        now = time.time()
        # Use an expiration 1 second in the future (plus 60s skew = 61s TTL)
        exp_ts = now + 1.0

        # Initially unrevoked
        assert await service.is_revoked(jti=test_jti) is False

        # Revoke the token
        await service.revoke_token(jti=test_jti, exp=exp_ts, user_id="user_test", reason="logout")

        # Now revoked
        assert await service.is_revoked(jti=test_jti) is True

        # Simulate fast-forwarding time past expiration
        from src.Services import token_denylist_service as tds
        key = f"denylist:jti:{test_jti}"
        assert key in tds._memory_denylist
        # Manually backdate the entry's monotonic expiration
        tds._memory_denylist[key]["expires_at"] = time.monotonic() - 1.0

        # After expiration, is_revoked must return False and purge key
        assert await service.is_revoked(jti=test_jti) is False
        assert key not in tds._memory_denylist

    @pytest.mark.anyio
    async def test_user_revocation_blocks_all_jtis(self):
        """UT-03: User-level revocation blocks any token bearing that user_id."""
        service = TokenDenylistService()
        victim_user_id = f"user_{uuid.uuid4().hex[:8]}"
        unrelated_jti_1 = uuid.uuid4().hex
        unrelated_jti_2 = uuid.uuid4().hex

        # Not revoked yet
        assert await service.is_revoked(jti=unrelated_jti_1, user_id=victim_user_id) is False

        # Revoke the user globally (e.g. account deletion)
        await service.revoke_user(user_id=victim_user_id, reason="account_deleted")

        # Both JTIs for this user are now blocked
        assert await service.is_revoked(jti=unrelated_jti_1, user_id=victim_user_id) is True
        assert await service.is_revoked(jti=unrelated_jti_2, user_id=victim_user_id) is True
        assert await service.is_revoked(user_id=victim_user_id) is True

        # Unrelated user with same JTI is NOT blocked
        different_user_id = f"user_{uuid.uuid4().hex[:8]}"
        assert await service.is_revoked(jti=unrelated_jti_1, user_id=different_user_id) is False

    @pytest.mark.anyio
    async def test_redis_outage_graceful_fallback(self):
        """UT-04: Mock Redis to fail/raise errors; service falls back to in-memory store."""
        mock_redis = AsyncMock()
        mock_redis.setex.side_effect = ConnectionError("Redis cluster unreachable")
        mock_redis.exists.side_effect = ConnectionError("Redis cluster unreachable")
        mock_redis.ping.side_effect = ConnectionError("Redis cluster unreachable")

        service = TokenDenylistService(redis_client=mock_redis)
        test_jti = uuid.uuid4().hex
        exp = time.time() + 300

        # Revoking must NOT raise an exception, must write to memory fallback
        await service.revoke_token(jti=test_jti, exp=exp, user_id="user_fallback")

        # Querying must NOT raise an exception, must retrieve from memory fallback
        is_rev = await service.is_revoked(jti=test_jti)
        assert is_rev is True

    @pytest.mark.anyio
    async def test_clock_skew_leeway_calculation(self):
        """UT-05: Verify TTL adds CLOCK_SKEW_SECONDS (60s) to raw expiration difference."""
        mock_redis = AsyncMock()
        service = TokenDenylistService(redis_client=mock_redis)

        test_jti = uuid.uuid4().hex
        delta_seconds = 120
        exp_time = time.time() + delta_seconds

        await service.revoke_token(jti=test_jti, exp=exp_time, user_id="user_skew")

        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.await_args[0]
        # call_args: (key, ttl, payload)
        passed_key = call_args[0]
        passed_ttl = call_args[1]

        assert passed_key == f"denylist:jti:{test_jti}"
        # TTL should be approximately delta_seconds + CLOCK_SKEW_SECONDS (tolerance +/- 2s)
        expected_ttl = delta_seconds + CLOCK_SKEW_SECONDS
        assert abs(passed_ttl - expected_ttl) <= 2

    @pytest.mark.anyio
    async def test_production_readiness_failure_when_redis_offline(self):
        """Test supervisor requirement: in production, check_readiness reports failure when Redis offline."""
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = ConnectionError("Cannot reach Redis in production")

        service = TokenDenylistService(redis_client=mock_redis)

        with patch("src.Services.token_denylist_service.get_settings") as mock_settings:
            mock_settings.return_value.environment = "production"
            is_ready, msg = await service.check_readiness()
            assert is_ready is False
            assert "Redis ping failed" in msg

        # In development, it falls back to memory and does not fail readiness
        with patch("src.Services.token_denylist_service.get_settings") as mock_settings:
            mock_settings.return_value.environment = "development"
            is_ready, msg = await service.check_readiness()
            assert is_ready is True
            assert "Memory fallback active" in msg

    @pytest.mark.anyio
    async def test_verify_jwt_token_async_rejects_revoked(self):
        """Verify that verify_jwt_token_async raises HTTP 401 when token or user is revoked."""
        token = create_access_token(user_id="user_revoked_test", username="revoked_user")
        secret = _get_jwt_secret()
        settings = get_settings()
        decoded = jwt.decode(token, secret, algorithms=[getattr(settings, "jwt_algorithm", "HS256")])
        jti = decoded["jti"]

        service = get_token_denylist_service()

        # Before revocation: valid
        payload = await verify_jwt_token_async(token, expected_type="access")
        assert payload["sub"] == "user_revoked_test"
        assert payload["jti"] == jti

        # Revoke by JTI
        await service.revoke_token(jti=jti, exp=decoded["exp"], user_id="user_revoked_test")

        # After revocation: raises HTTPException 401
        with pytest.raises(HTTPException) as exc_info:
            await verify_jwt_token_async(token, expected_type="access")
        assert exc_info.value.status_code == 401
        assert "Authentication token has been revoked" in exc_info.value.detail
