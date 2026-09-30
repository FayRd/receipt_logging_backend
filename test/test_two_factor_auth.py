#!/usr/bin/env python3
"""Comprehensive test suite for Two-Factor Authentication (2FA) via Mailtrap OTP."""

import uuid
import pytest
from src.Infrastructure import redis_service as rs
from src.API.v1 import user as user_module


def _unique_user() -> dict:
    raw_id = uuid.uuid4().hex[:6]
    name = f"tfa_{raw_id[:5]}"
    return {
        "username": name,
        "email": f"{name}@test.example.com",
        "password": "SecurePass123!",
    }


def _create_and_login(client) -> tuple[dict, str]:
    """Helper: create user and return (user_payload, access_token)."""
    user = _unique_user()
    res = client.post("/api/v1/user/create", json=user)
    assert res.status_code == 201, f"Create failed: {res.text}"
    user["id"] = res.json()["id"]

    login_res = client.post(
        "/api/v1/user/login",
        json={"username": user["username"], "password": user["password"]},
    )
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    token = login_res.json()["access_token"]
    return user, token


@pytest.fixture(autouse=True)
def mock_emails_and_capture_otps(monkeypatch):
    """Mock slow email dispatches and intercept all store_otp calls."""
    captured_otps: dict[str, list[str]] = {
        "email": [],
        "2fa_login": [],
        "2fa_action": [],
    }

    original_store = rs.store_otp

    def mock_store(uid, ttype, identifier, otp, ttl_seconds=300):
        if ttype in captured_otps:
            captured_otps[ttype].append(otp)
        original_store(uid, ttype, identifier, otp, ttl_seconds)

    monkeypatch.setattr(rs, "store_otp", mock_store)
    monkeypatch.setattr(user_module, "store_otp", mock_store)

    async def mock_email(*args, **kwargs):
        return True

    monkeypatch.setattr(user_module, "send_2fa_login_email", mock_email)
    monkeypatch.setattr(user_module, "send_2fa_action_email", mock_email)
    from src.Services import email_service
    monkeypatch.setattr(email_service, "send_verification_email", mock_email)

    return captured_otps


def _verify_user_email(client, user: dict, token: str, captured_otps: dict) -> None:
    """Helper: verify the user's email via OTP flow."""
    headers = {"Authorization": f"Bearer {token}"}

    init_res = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": user["email"]},
        headers=headers,
    )
    assert init_res.status_code == 200
    assert len(captured_otps["email"]) > 0

    vfy_res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": user["email"], "otp": captured_otps["email"][-1]},
        headers=headers,
    )
    assert vfy_res.status_code == 200
    assert vfy_res.json()["email_verified_at"] is not None


# ── 1. Unverified Email Cannot Enable 2FA ─────────────────────────────────────

def test_2fa_enable_requires_verified_email(client):
    """Attempting to enable 2FA on an account without a verified email returns 400."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post(
        "/api/v1/user/2fa/enable",
        json={"otp": "123456"},
        headers=headers,
    )
    assert res.status_code == 400
    assert "verified" in res.json()["detail"].lower()


# ── 2. Enable 2FA Flow ────────────────────────────────────────────────────────

def test_2fa_enable_success(client, mock_emails_and_capture_otps):
    """Enabling 2FA succeeds with verified email and valid action OTP."""
    captured = mock_emails_and_capture_otps
    user, token = _create_and_login(client)
    _verify_user_email(client, user, token, captured)

    headers = {"Authorization": f"Bearer {token}"}

    # Request action OTP for enable_2fa
    req_res = client.post("/api/v1/user/2fa/request-otp?action=enable_2fa", headers=headers)
    assert req_res.status_code == 200
    assert len(captured["2fa_action"]) == 1

    # Enable 2FA with captured OTP
    enable_res = client.post(
        "/api/v1/user/2fa/enable",
        json={"otp": captured["2fa_action"][-1]},
        headers=headers,
    )
    assert enable_res.status_code == 200
    assert enable_res.json()["is_2fa_enabled"] is True


# ── 3. Login Flow with 2FA Challenge ──────────────────────────────────────────

def test_2fa_login_challenge_and_verification(client, mock_emails_and_capture_otps):
    """Once 2FA is enabled, login intercepts with temp_token and requires login-2fa-verify."""
    captured = mock_emails_and_capture_otps
    user, token = _create_and_login(client)
    _verify_user_email(client, user, token, captured)

    headers = {"Authorization": f"Bearer {token}"}

    # Request OTP and enable 2FA
    client.post("/api/v1/user/2fa/request-otp?action=enable_2fa", headers=headers)
    client.post("/api/v1/user/2fa/enable", json={"otp": captured["2fa_action"][-1]}, headers=headers)

    # Now attempt login with valid credentials
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": user["username"], "password": user["password"]},
    )
    assert login_res.status_code == 200
    data = login_res.json()
    assert data["requires_2fa"] is True
    assert data["temp_token"] is not None
    assert data["masked_email"] is not None
    assert data["access_token"] is None
    temp_token = data["temp_token"]
    assert len(captured["2fa_login"]) == 1

    # Attempt verify with invalid OTP
    bad_vfy = client.post(
        "/api/v1/user/login-2fa-verify",
        json={"temp_token": temp_token, "otp": "000000"},
    )
    assert bad_vfy.status_code == 400
    assert "incorrect" in bad_vfy.json()["detail"].lower() or "attempt" in bad_vfy.json()["detail"].lower()

    # Attempt verify with correct OTP
    good_vfy = client.post(
        "/api/v1/user/login-2fa-verify",
        json={"temp_token": temp_token, "otp": captured["2fa_login"][-1]},
    )
    assert good_vfy.status_code == 200
    vfy_data = good_vfy.json()
    assert vfy_data["requires_2fa"] is False
    assert vfy_data["access_token"] is not None
    assert vfy_data["refresh_token"] is not None
    assert vfy_data["user"]["is_2fa_enabled"] is True


# ── 4. Sensitive Actions 2FA Protection (Password Change) ────────────────────

def test_2fa_sensitive_action_requires_header(client, mock_emails_and_capture_otps):
    """When 2FA is enabled, change-password requires X-2FA-OTP header."""
    captured = mock_emails_and_capture_otps
    user, token = _create_and_login(client)
    _verify_user_email(client, user, token, captured)

    headers = {"Authorization": f"Bearer {token}"}

    # Enable 2FA
    client.post("/api/v1/user/2fa/request-otp?action=enable_2fa", headers=headers)
    client.post("/api/v1/user/2fa/enable", json={"otp": captured["2fa_action"][-1]}, headers=headers)

    # Change password without header -> 403 Forbidden
    no_header_res = client.post(
        "/api/v1/user/change-password",
        json={"old_password": user["password"], "new_password": "BrandNewPass999!"},
        headers=headers,
    )
    assert no_header_res.status_code == 403
    assert no_header_res.headers.get("x-2fa-required") == "true"

    # Request new action OTP for password change (clear cooldown first)
    rs.clear_resend_cooldown(user["id"], "2fa_action")
    client.post("/api/v1/user/2fa/request-otp?action=change_password", headers=headers)
    action_otp = captured["2fa_action"][-1]

    # Change password with valid header -> 200 OK
    headers_with_otp = {**headers, "X-2FA-OTP": action_otp}
    pw_res = client.post(
        "/api/v1/user/change-password",
        json={"old_password": user["password"], "new_password": "BrandNewPass999!"},
        headers=headers_with_otp,
    )
    assert pw_res.status_code == 200
    assert pw_res.json()["success"] is True


# ── 5. Disable 2FA Flow ───────────────────────────────────────────────────────

def test_2fa_disable_success(client, mock_emails_and_capture_otps):
    """Disabling 2FA with valid action OTP updates is_2fa_enabled to False."""
    captured = mock_emails_and_capture_otps
    user, token = _create_and_login(client)
    _verify_user_email(client, user, token, captured)

    headers = {"Authorization": f"Bearer {token}"}

    # Enable 2FA
    client.post("/api/v1/user/2fa/request-otp?action=enable_2fa", headers=headers)
    client.post("/api/v1/user/2fa/enable", json={"otp": captured["2fa_action"][-1]}, headers=headers)

    # Request OTP to disable (clear cooldown first)
    rs.clear_resend_cooldown(user["id"], "2fa_action")
    client.post("/api/v1/user/2fa/request-otp?action=disable_2fa", headers=headers)
    disable_otp = captured["2fa_action"][-1]

    # Disable 2FA
    dis_res = client.post(
        "/api/v1/user/2fa/disable",
        json={"otp": disable_otp},
        headers=headers,
    )
    assert dis_res.status_code == 200
    assert dis_res.json()["is_2fa_enabled"] is False

    # Next login should NOT require 2FA
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": user["username"], "password": user["password"]},
    )
    assert login_res.status_code == 200
    assert login_res.json()["requires_2fa"] is False
    assert login_res.json()["access_token"] is not None
