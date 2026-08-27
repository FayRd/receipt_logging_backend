#!/usr/bin/env python3
import os
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from src.Services import email_service
from src.Models.Users.user_repository import UserRepository


def _unique_user() -> dict:
    raw_id = uuid.uuid4().hex[:6]
    name = f"rst_{raw_id[:5]}"
    return {
        "username": name,
        "email": f"{name}@test.example.com",
        "password": "OldPassword123!",
        "country_code": "+60",
        "mobile_number": f"12{raw_id[:7]}",
    }


def test_reset_initiate_success_no_dev_otp_leakage(client, monkeypatch):
    """Password reset initiate succeeds, dispatches email via email_service, and never leaks dev_otp in JSON."""
    captured_emails: list[dict] = []

    async def mock_send_email(to_email: str, otp: str, username: str = "User") -> bool:
        captured_emails.append({"to_email": to_email, "otp": otp, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_email", mock_send_email)

    user = _unique_user()
    res = client.post("/api/v1/user/create", json=user)
    assert res.status_code == 201

    # Initiate via email
    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    data = res_init.json()
    assert data["success"] is True
    assert "dev_otp" not in data, "dev_otp must NOT be leaked in API response"

    # Verify email service was called
    assert len(captured_emails) == 1
    assert captured_emails[0]["to_email"] == user["email"].lower()
    assert len(captured_emails[0]["otp"]) == 6

    # Verify no otp_dev.log file created on disk
    assert not os.path.exists("otp_dev.log")


def test_reset_initiate_unknown_identifier_returns_generic_200(client, monkeypatch):
    """Non-existent email should still return 200 to prevent user enumeration and send no email."""
    captured_emails: list[dict] = []

    async def mock_send_email(to_email: str, otp: str, username: str = "User") -> bool:
        captured_emails.append({"to_email": to_email, "otp": otp, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_email", mock_send_email)

    res = client.post("/api/v1/user/reset-password-initiate", json={"identifier": "nonexistent_email_qa_99@test.com"})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "dev_otp" not in data
    assert len(captured_emails) == 0


def test_reset_initiate_during_cooldown_dispatches_cooldown_email(client, monkeypatch):
    """When user account is in 7-day cooldown, reset initiation dispatches a cooldown email stating remaining days and hours."""
    captured_otp_emails: list[dict] = []
    captured_cooldown_emails: list[dict] = []

    async def mock_send_otp(to_email: str, otp: str, username: str = "User") -> bool:
        captured_otp_emails.append({"to_email": to_email, "otp": otp, "username": username})
        return True

    async def mock_send_cooldown(to_email: str, countdown_str: str, username: str = "User") -> bool:
        captured_cooldown_emails.append({"to_email": to_email, "countdown_str": countdown_str, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_email", mock_send_otp)
    monkeypatch.setattr(email_service, "send_password_reset_cooldown_email", mock_send_cooldown)

    user = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user)
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    # Set password_changed_at to 2 days ago (5 days remaining)
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    client.patch(
        "/api/v1/user/me",
        json={"preferences": {"password_changed_at": two_days_ago}},
        headers={"X-User-Name": user["username"], "X-User-Token": user["password"]},
    )

    # Initiate password reset
    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    data = res_init.json()
    assert data["success"] is True
    assert "dev_otp" not in data

    # OTP email should NOT have been sent
    assert len(captured_otp_emails) == 0

    # Cooldown advisory email SHOULD have been sent
    assert len(captured_cooldown_emails) == 1
    advisory = captured_cooldown_emails[0]
    assert advisory["to_email"] == user["email"].lower()
    assert "4 days and" in advisory["countdown_str"] or "5 days and" in advisory["countdown_str"]
    assert "hours" in advisory["countdown_str"] or "hour" in advisory["countdown_str"]


def test_reset_initiate_during_cooldown_with_raw_string_preferences_dispatches_cooldown_email(client, monkeypatch):
    """When preferences is returned as a JSON-encoded string, cooldown is still properly detected."""
    captured_cooldown_emails: list[dict] = []

    async def mock_send_cooldown(to_email: str, countdown_str: str, username: str = "User") -> bool:
        captured_cooldown_emails.append({"to_email": to_email, "countdown_str": countdown_str, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_cooldown_email", mock_send_cooldown)

    user = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user)
    assert create_res.status_code == 201

    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    # Monkeypatch UserRepository.get_by_email_or_mobile to return preferences as a JSON string
    from src.Models.Users.user_repository import UserRepository
    orig_get = UserRepository.get_by_email_or_mobile

    async def mock_get(self, identifier):
        u = await orig_get(self, identifier)
        if u:
            import json
            u = dict(u)
            u["preferences"] = json.dumps({"password_changed_at": two_days_ago})
        return u

    monkeypatch.setattr(UserRepository, "get_by_email_or_mobile", mock_get)

    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    assert len(captured_cooldown_emails) == 1
    assert "days and" in captured_cooldown_emails[0]["countdown_str"]


def test_reset_initiate_during_cooldown_with_forget_password_fallback_dispatches_cooldown_email(client, monkeypatch):
    """When preferences has no password_changed_at, forget_password table's latest completed reset triggers cooldown."""
    captured_cooldown_emails: list[dict] = []

    async def mock_send_cooldown(to_email: str, countdown_str: str, username: str = "User") -> bool:
        captured_cooldown_emails.append({"to_email": to_email, "countdown_str": countdown_str, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_cooldown_email", mock_send_cooldown)

    user = _unique_user()
    create_res = client.post("/api/v1/user/create", json=user)
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    # Fallback to forget_password record
    from src.Models.Users.password_reset_repository import PasswordResetRepository
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    async def mock_get_latest(self, uid):
        if uid == user_id:
            return three_days_ago
        return None

    monkeypatch.setattr(PasswordResetRepository, "get_latest_reset_timestamp", mock_get_latest)

    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    assert len(captured_cooldown_emails) == 1
    assert "3 days and" in captured_cooldown_emails[0]["countdown_str"] or "4 days and" in captured_cooldown_emails[0]["countdown_str"]


def test_reset_otp_verification_success(client, monkeypatch):
    """Verifying correct OTP issues a single-use reset_token."""
    captured_emails: list[dict] = []

    async def mock_send_email(to_email: str, otp: str, username: str = "User") -> bool:
        captured_emails.append({"to_email": to_email, "otp": otp, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_email", mock_send_email)

    user = _unique_user()
    client.post("/api/v1/user/create", json=user)

    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    assert len(captured_emails) == 1
    otp = captured_emails[0]["otp"]

    # Verify OTP
    res_otp = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": otp,
    })
    assert res_otp.status_code == 200
    data_otp = res_otp.json()
    assert data_otp["success"] is True
    assert "reset_token" in data_otp
    assert data_otp["reset_token"].startswith("rst_")


def test_reset_otp_verification_wrong_code(client, monkeypatch):
    """Submitting incorrect OTP returns 400."""
    captured_emails: list[dict] = []

    async def mock_send_email(to_email: str, otp: str, username: str = "User") -> bool:
        captured_emails.append({"to_email": to_email, "otp": otp, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_email", mock_send_email)

    user = _unique_user()
    client.post("/api/v1/user/create", json=user)
    client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})

    res_wrong = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": "000000",
    })
    assert res_wrong.status_code == 400
    assert "Invalid reset code" in res_wrong.json()["detail"]


def test_end_to_end_password_reset_flow(client, monkeypatch):
    """Complete end-to-end flow: Initiate -> Verify OTP -> Change Password -> Login with new password."""
    captured_emails: list[dict] = []
    captured_cooldown_emails: list[dict] = []

    async def mock_send_email(to_email: str, otp: str, username: str = "User") -> bool:
        captured_emails.append({"to_email": to_email, "otp": otp, "username": username})
        return True

    async def mock_send_cooldown(to_email: str, countdown_str: str, username: str = "User") -> bool:
        captured_cooldown_emails.append({"to_email": to_email, "countdown_str": countdown_str, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_email", mock_send_email)
    monkeypatch.setattr(email_service, "send_password_reset_cooldown_email", mock_send_cooldown)

    user = _unique_user()
    client.post("/api/v1/user/create", json=user)

    # Step 1: Initiate
    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    assert len(captured_emails) == 1
    otp = captured_emails[0]["otp"]

    # Step 2: Verify OTP
    res_otp = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": otp,
    })
    reset_token = res_otp.json()["reset_token"]

    # Step 3: Set New Password
    new_pwd = "NewSecurePass2026!"
    res_new = client.post("/api/v1/user/password-reset-new", json={
        "reset_token": reset_token,
        "new_password": new_pwd,
    })
    assert res_new.status_code == 200
    assert res_new.json()["success"] is True

    # Old password fails
    res_old_login = client.post("/api/v1/user/login", json={
        "username": user["username"],
        "password": user["password"],
    })
    assert res_old_login.status_code == 401

    # New password succeeds
    res_new_login = client.post("/api/v1/user/login", json={
        "username": user["username"],
        "password": new_pwd,
    })
    assert res_new_login.status_code == 200
    assert res_new_login.json()["success"] is True

    # Step 4: Subsequent reset initiation immediately hits active cooldown!
    captured_emails.clear()
    res_init_again = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init_again.status_code == 200
    assert len(captured_emails) == 0, "No OTP email should be sent during cooldown"
    assert len(captured_cooldown_emails) == 1, "Cooldown advisory email must be dispatched"
    assert "6 days and" in captured_cooldown_emails[0]["countdown_str"]


def test_reset_token_reuse_rejected(client, monkeypatch):
    """Reset tokens are strictly single-use."""
    captured_emails: list[dict] = []

    async def mock_send_email(to_email: str, otp: str, username: str = "User") -> bool:
        captured_emails.append({"to_email": to_email, "otp": otp, "username": username})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_email", mock_send_email)

    user = _unique_user()
    client.post("/api/v1/user/create", json=user)

    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    otp = captured_emails[0]["otp"]

    res_otp = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": otp,
    })
    reset_token = res_otp.json()["reset_token"]

    # First reset works
    res1 = client.post("/api/v1/user/password-reset-new", json={
        "reset_token": reset_token,
        "new_password": "NewPasswordStep1!",
    })
    assert res1.status_code == 200

    # Second reset with same token is rejected (single-use)
    res2 = client.post("/api/v1/user/password-reset-new", json={
        "reset_token": reset_token,
        "new_password": "NewPasswordStep2!",
    })
    assert res2.status_code == 400
    assert "Invalid or expired reset token" in res2.json()["detail"]
