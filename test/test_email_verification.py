#!/usr/bin/env python3
"""Tests for email verification endpoints: POST /user/verify-initiate and /user/verify-complete."""

import uuid
import pytest


def _unique_user() -> dict:
    raw_id = uuid.uuid4().hex[:6]
    name = f"vfy_{raw_id[:5]}"
    return {
        "username": name,
        "email": f"{name}@test.example.com",
        "password": "SecurePass123!",
    }


def _create_and_login(client) -> tuple[dict, str]:
    """Helper: create a user and return (user_payload, access_token)."""
    user = _unique_user()
    res = client.post("/api/v1/user/create", json=user)
    assert res.status_code == 201, f"Create failed: {res.text}"

    login_res = client.post(
        "/api/v1/user/login",
        json={"username": user["username"], "password": user["password"]},
    )
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    token = login_res.json()["access_token"]
    return user, token


# ── Unauthenticated access ────────────────────────────────────────────────────

def test_verify_initiate_unauthenticated(client):
    """verify-initiate without auth headers returns 401."""
    res = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": "test@example.com"},
    )
    assert res.status_code == 401


def test_verify_complete_unauthenticated(client):
    """verify-complete without auth headers returns 401."""
    res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": "test@example.com", "otp": "123456"},
    )
    assert res.status_code == 401


# ── Input validation ─────────────────────────────────────────────────────────

def test_verify_initiate_invalid_email_format(client):
    """verify-initiate with malformed email returns 422."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": "not-an-email"},
        headers=headers,
    )
    assert res.status_code in (400, 422)


def test_verify_complete_wrong_otp_length(client):
    """verify-complete with OTP shorter than 6 digits fails validation."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": user["email"], "otp": "123"},
        headers=headers,
    )
    assert res.status_code == 422


# ── OTP flow ─────────────────────────────────────────────────────────────────

def test_verify_initiate_success(client):
    """verify-initiate with valid email returns 200 with success=True."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": user["email"]},
        headers=headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["cooldown_seconds"] == 60


def test_verify_initiate_cooldown_blocks_immediate_resend(client):
    """Second initiate within 60s returns 429."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # First request succeeds
    first = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": user["email"]},
        headers=headers,
    )
    assert first.status_code == 200

    # Immediate second request hits cooldown
    second = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": user["email"]},
        headers=headers,
    )
    assert second.status_code == 429


def test_verify_complete_expired_or_missing_otp(client):
    """verify-complete without a prior initiate returns 400 (no OTP stored)."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": user["email"], "otp": "000000"},
        headers=headers,
    )
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower() or "never" in res.json()["detail"].lower()


def test_verify_complete_wrong_otp(client):
    """verify-complete with wrong code returns 400 with remaining attempts."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Initiate to store an OTP
    client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": user["email"]},
        headers=headers,
    )

    # Try wrong OTP
    res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": user["email"], "otp": "000000"},
        headers=headers,
    )
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    assert "incorrect" in detail or "wrong" in detail or "attempt" in detail


def test_verify_complete_brute_force_lockout(client):
    """Five wrong OTP attempts lock out further attempts."""
    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Initiate
    client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": user["email"]},
        headers=headers,
    )

    # Submit 5 wrong OTPs
    for _ in range(5):
        client.post(
            "/api/v1/user/verify-complete",
            json={"type": "email", "identifier": user["email"], "otp": "000000"},
            headers=headers,
        )

    # 6th attempt should report lockout (OTP deleted after 5 attempts)
    res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": user["email"], "otp": "000000"},
        headers=headers,
    )
    assert res.status_code == 400
    detail = res.json()["detail"].lower()
    assert "expired" in detail or "never" in detail or "too many" in detail


def test_verify_complete_success_with_dev_otp(client, monkeypatch):
    """Verify complete succeeds when correct OTP is submitted (captured via monkeypatch)."""
    from src.Infrastructure import redis_service as rs

    # Capture OTP from store_otp
    captured_otp: list[str] = []
    original_store = rs.store_otp

    def mock_store_otp(user_id, target_type, identifier, otp):
        captured_otp.append(otp)
        original_store(user_id, target_type, identifier, otp)

    monkeypatch.setattr(rs, "store_otp", mock_store_otp)

    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Initiate
    init_res = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": user["email"]},
        headers=headers,
    )
    assert init_res.status_code == 200
    assert len(captured_otp) == 1, "OTP was not captured"

    # Complete with correct OTP
    complete_res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": user["email"], "otp": captured_otp[0]},
        headers=headers,
    )
    assert complete_res.status_code == 200
    data = complete_res.json()
    assert data["email_verified_at"] is not None
    assert data["email"] == user["email"]


def test_verify_complete_success_updates_email(client, monkeypatch):
    """Verify complete with a new email updates users.email in DB."""
    from src.Infrastructure import redis_service as rs

    captured_otp: list[str] = []
    original_store = rs.store_otp

    def mock_store_otp(user_id, target_type, identifier, otp):
        captured_otp.append(otp)
        original_store(user_id, target_type, identifier, otp)

    monkeypatch.setattr(rs, "store_otp", mock_store_otp)

    user, token = _create_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    new_email = f"new_{uuid.uuid4().hex[:6]}@test.example.com"

    # Initiate with new (different) email
    init_res = client.post(
        "/api/v1/user/verify-initiate",
        json={"type": "email", "identifier": new_email},
        headers=headers,
    )
    assert init_res.status_code == 200
    assert len(captured_otp) == 1

    # Complete with correct OTP
    complete_res = client.post(
        "/api/v1/user/verify-complete",
        json={"type": "email", "identifier": new_email, "otp": captured_otp[0]},
        headers=headers,
    )
    assert complete_res.status_code == 200
    data = complete_res.json()
    assert data["email"] == new_email
    assert data["email_verified_at"] is not None


def test_verify_initiate_default_tier(client):
    """Newly created users have tier == 'free'."""
    user, _ = _create_and_login(client)
    me_res = client.get(
        "/api/v1/user/me",
        headers={"X-User-Name": user["username"], "X-User-Token": user["password"]},
    )
    assert me_res.status_code == 200
    assert me_res.json().get("tier") == "free"
