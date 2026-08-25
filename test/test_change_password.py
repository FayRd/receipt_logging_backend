import uuid
import pytest


def _unique_user() -> dict:
    raw_id = uuid.uuid4().hex[:6]
    name = f"u_{raw_id[:4]}"[:10]
    return {
        "username": name,
        "email": f"{name}_{raw_id}@test.example.com",
        "password": "Password123!",
    }


def test_change_password_unauthenticated(client):
    """Calling /user/change-password without token returns 401."""
    res = client.post(
        "/api/v1/user/change-password",
        json={"old_password": "OldPassword1!", "new_password": "NewPassword1!"},
    )
    assert res.status_code == 401


def test_change_password_success(client):
    """User changes password with correct old password and valid new password."""
    payload = _unique_user()
    create_res = client.post("/api/v1/user/create", json=payload)
    assert create_res.status_code == 201

    # Login to obtain access token
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": payload["username"], "password": payload["password"]},
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Change password
    change_res = client.post(
        "/api/v1/user/change-password",
        headers=headers,
        json={"old_password": payload["password"], "new_password": "BrandNewPassword1!"},
    )
    assert change_res.status_code == 200
    assert change_res.json()["success"] is True

    # Old password should no longer work
    old_login = client.post(
        "/api/v1/user/login",
        json={"username": payload["username"], "password": payload["password"]},
    )
    assert old_login.status_code == 401

    # New password works for login
    new_login = client.post(
        "/api/v1/user/login",
        json={"username": payload["username"], "password": "BrandNewPassword1!"},
    )
    assert new_login.status_code == 200


def test_change_password_wrong_old_password(client):
    """Incorrect old password returns 400."""
    payload = _unique_user()
    client.post("/api/v1/user/create", json=payload)
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": payload["username"], "password": payload["password"]},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post(
        "/api/v1/user/change-password",
        headers=headers,
        json={"old_password": "WrongPassword1!", "new_password": "BrandNewPassword1!"},
    )
    assert res.status_code == 400
    assert "Current password is incorrect" in res.json()["detail"]


def test_change_password_same_as_old(client):
    """New password identical to old password returns 400."""
    payload = _unique_user()
    client.post("/api/v1/user/create", json=payload)
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": payload["username"], "password": payload["password"]},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post(
        "/api/v1/user/change-password",
        headers=headers,
        json={"old_password": payload["password"], "new_password": payload["password"]},
    )
    assert res.status_code == 400
    assert "cannot be the same" in res.json()["detail"]


def test_change_password_weak_new_password(client):
    """Weak new password (e.g. short or missing numbers/symbols) fails validation."""
    payload = _unique_user()
    client.post("/api/v1/user/create", json=payload)
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": payload["username"], "password": payload["password"]},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post(
        "/api/v1/user/change-password",
        headers=headers,
        json={"old_password": payload["password"], "new_password": "short"},
    )
    assert res.status_code == 422


def test_change_password_7_day_cooldown_rate_limit(client):
    """Attempting to change password within 7 days returns HTTP 429."""
    payload = _unique_user()
    client.post("/api/v1/user/create", json=payload)
    login_res = client.post(
        "/api/v1/user/login",
        json={"username": payload["username"], "password": payload["password"]},
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. First change succeeds
    res1 = client.post(
        "/api/v1/user/change-password",
        headers=headers,
        json={"old_password": payload["password"], "new_password": "BrandNewPassword1!"},
    )
    assert res1.status_code == 200
    assert "password_changed_at" in res1.json()

    # 2. Immediate second change attempt fails with HTTP 429
    res2 = client.post(
        "/api/v1/user/change-password",
        headers=headers,
        json={"old_password": "BrandNewPassword1!", "new_password": "AnotherNewPassword1!"},
    )
    assert res2.status_code == 429
    assert "Change allowed in 7 days" in res2.json()["detail"]

