#!/usr/bin/env python3
"""Unit tests for public web account deletion endpoints (TODO-28)."""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch
import pytest

from src.Auth.rate_limiter import limiter
from src.Infrastructure.database import get_supabase_client
from src.Infrastructure.redis_service import clear_resend_cooldown, store_otp


def _unique_user() -> dict:
    raw_id = uuid.uuid4().hex[:6]
    name = f"del_{raw_id[:4]}"[:10]
    return {
        "username": name,
        "email": f"{name}_{raw_id}@test.example.com",
        "password": "Password123!",
    }


# ── GET /delete-account ──────────────────────────────────────────────────────

def test_get_delete_account_page_returns_html(client):
    """GET /delete-account serves the public responsive HTML page."""
    res = client.get("/delete-account")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    content = res.text
    assert "Delete Account &amp; Data" in content or "Delete Account & Data" in content
    assert "Google Play Data Safety" in content
    assert 'name="website"' in content  # Honeypot field present
    assert "onRequestOtp" in content


# ── POST /api/v1/web/request-deletion ────────────────────────────────────────

def test_request_deletion_unknown_email_always_200(client):
    """Unknown email returns generic 200 (prevents user enumeration)."""
    res = client.post(
        "/api/v1/web/request-deletion",
        json={"email": "nobody_exists_123456789@example.com"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "verification code has been sent" in data["message"]


def test_request_deletion_honeypot_silently_rejects(client):
    """When honeypot field 'website' is filled, request is silently dropped with 200."""
    with patch("src.API.web.deletion.send_2fa_action_email", new_callable=AsyncMock) as mock_email:
        res = client.post(
            "/api/v1/web/request-deletion",
            json={"email": "bot@example.com", "website": "https://spam.bot.net"},
        )
        assert res.status_code == 200
        assert res.json()["success"] is True
        mock_email.assert_not_called()


def test_request_deletion_valid_user_sends_email(client):
    """Registered user receives 2FA action email with OTP."""
    user_payload = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user_payload)
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    clear_resend_cooldown(user_id, "web_deletion")

    with patch("src.API.web.deletion.send_2fa_action_email", new_callable=AsyncMock) as mock_email:
        mock_email.return_value = True
        res = client.post(
            "/api/v1/web/request-deletion",
            json={"email": user_payload["email"]},
        )
        assert res.status_code == 200
        assert res.json()["success"] is True
        mock_email.assert_called_once()
        call_kwargs = mock_email.call_args.kwargs
        assert call_kwargs["to_email"] == user_payload["email"].lower()
        assert "deletion" in call_kwargs["action_name"]


def test_request_deletion_cooldown_respected(client):
    """Consecutive OTP requests within 60s cooldown do not dispatch a duplicate email."""
    user_payload = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user_payload)
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    clear_resend_cooldown(user_id, "web_deletion")

    with patch("src.API.web.deletion.send_2fa_action_email", new_callable=AsyncMock) as mock_email:
        mock_email.return_value = True
        # First request dispatches
        res1 = client.post("/api/v1/web/request-deletion", json={"email": user_payload["email"]})
        assert res1.status_code == 200
        assert mock_email.call_count == 1

        # Second immediate request suppressed by cooldown
        res2 = client.post("/api/v1/web/request-deletion", json={"email": user_payload["email"]})
        assert res2.status_code == 200
        assert mock_email.call_count == 1  # Still 1, not called again


def test_request_deletion_rate_limit_enforced(client):
    """4th request within rolling window from same IP is rate-limited with HTTP 429."""
    for i in range(3):
        res = client.post(
            "/api/v1/web/request-deletion",
            json={"email": f"ratelimit_{i}@test.example.com"},
        )
        assert res.status_code == 200

    # 4th request exceeds max_requests=3
    res_limited = client.post(
        "/api/v1/web/request-deletion",
        json={"email": "ratelimit_excess@test.example.com"},
    )
    assert res_limited.status_code == 429
    assert "Rate limit exceeded" in res_limited.json()["detail"]


# ── POST /api/v1/web/confirm-deletion ────────────────────────────────────────

def test_confirm_deletion_honeypot_rejects(client):
    """Honeypot filled on confirmation returns HTTP 400 Bad Request."""
    res = client.post(
        "/api/v1/web/confirm-deletion",
        json={"email": "test@example.com", "otp": "123456", "website": "bot-spam"},
    )
    assert res.status_code == 400
    assert "Invalid request" in res.json()["detail"]


def test_confirm_deletion_unknown_user_rejects(client):
    """Unknown email on confirmation returns HTTP 400 Bad Request."""
    res = client.post(
        "/api/v1/web/confirm-deletion",
        json={"email": "nonexistent_del_user@example.com", "otp": "123456"},
    )
    assert res.status_code == 400
    assert "Invalid or expired" in res.json()["detail"]


def test_confirm_deletion_invalid_otp(client):
    """Providing wrong OTP returns HTTP 400."""
    user_payload = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user_payload)
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    # Store a valid OTP in cache
    store_otp(user_id=user_id, target_type="web_deletion", identifier=user_payload["email"], otp="123456")

    res = client.post(
        "/api/v1/web/confirm-deletion",
        json={"email": user_payload["email"], "otp": "999999"},
    )
    assert res.status_code == 400
    assert "verification code" in res.json()["detail"].lower()


def test_confirm_deletion_max_attempts_lockout(client):
    """5 failed OTP attempts lock out and invalidate the code."""
    user_payload = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user_payload)
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    store_otp(user_id=user_id, target_type="web_deletion", identifier=user_payload["email"], otp="123456")

    # Send 5 incorrect attempts, resetting HTTP rate limiter so we test OTP lockout specifically
    for i in range(5):
        asyncio.run(limiter.reset())
        res = client.post(
            "/api/v1/web/confirm-deletion",
            json={"email": user_payload["email"], "otp": f"00000{i}"},
        )
        assert res.status_code == 400

    # 6th attempt should be rejected as expired/invalidated even with correct OTP
    asyncio.run(limiter.reset())
    res_after = client.post(
        "/api/v1/web/confirm-deletion",
        json={"email": user_payload["email"], "otp": "123456"},
    )
    assert res_after.status_code == 400
    assert "invalid" in res_after.json()["detail"].lower() or "expired" in res_after.json()["detail"].lower()


def test_confirm_deletion_success_executes_cascade(client):
    """Valid OTP initiates full deletion cascade, tombstoning the user."""
    user_payload = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user_payload)
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    correct_otp = "847291"
    store_otp(user_id=user_id, target_type="web_deletion", identifier=user_payload["email"], otp=correct_otp)

    res = client.post(
        "/api/v1/web/confirm-deletion",
        json={"email": user_payload["email"], "otp": correct_otp},
    )
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert "permanently deleted" in res.json()["message"]

    # 1. Verify that logging in with previous credentials fails (user no longer exists/tombstoned)
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": user_payload["username"], "password": user_payload["password"]},
    )
    assert login_res.status_code in (401, 404)

    # 2. Verify that the email was freed (proves tombstone updated email to deleted_<id>@deleted.local)
    re_register_res = client.post(
        "/api/v1/user/create",
        json={
            "username": f"new_{uuid.uuid4().hex[:6]}",
            "email": user_payload["email"],
            "password": "Password123!",
        },
    )
    assert re_register_res.status_code == 201
