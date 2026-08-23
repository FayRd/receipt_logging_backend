#!/usr/bin/env python3
import uuid


# ── Helper ─────────────────────────────────────────────────────────────────────

def _unique_user(suffix: str = "") -> dict:
    """Generate a unique user registration payload (3-10 chars username, complex password)."""
    raw_id = uuid.uuid4().hex[:6]
    name = f"u_{raw_id[:4]}{suffix}"[:10]
    return {
        "username": name,
        "email": f"{name}_{raw_id}@test.example.com",
        "password": "Password123!",
    }


# ── POST /user/create ──────────────────────────────────────────────────────────

def test_user_create_success(client):
    payload = _unique_user()
    response = client.post("/api/v1/user/create", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["username"] == payload["username"]
    assert data["email"] == payload["email"]


def test_user_create_with_contact_fields(client):
    """Registration with optional country_code and mobile_number is stored and returned."""
    payload = {**_unique_user(), "country_code": "+60", "mobile_number": "123456789"}
    response = client.post("/api/v1/user/create", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["country_code"] == "+60"
    assert data["mobile_number"] == "123456789"


def test_user_create_duplicate_username(client):
    payload = _unique_user()
    res1 = client.post("/api/v1/user/create", json=payload)
    assert res1.status_code == 201

    # Same username, different email — should 409 on username
    payload2 = {**payload, "email": f"other_{uuid.uuid4().hex[:6]}@test.example.com"}
    res2 = client.post("/api/v1/user/create", json=payload2)
    assert res2.status_code == 409
    assert "Username" in res2.json()["detail"]


def test_user_create_duplicate_email(client):
    """Duplicate email should be rejected with HTTP 409 regardless of different username."""
    payload = _unique_user()
    res1 = client.post("/api/v1/user/create", json=payload)
    assert res1.status_code == 201

    # Different username, same email
    payload2 = {**payload, "username": f"u_{uuid.uuid4().hex[:6]}"}
    res2 = client.post("/api/v1/user/create", json=payload2)
    assert res2.status_code == 409
    assert "email" in res2.json()["detail"].lower()


def test_user_create_short_password(client):
    payload = {**_unique_user(), "password": "short"}
    response = client.post("/api/v1/user/create", json=payload)
    assert response.status_code == 422


def test_user_create_weak_password_no_special(client):
    payload = {**_unique_user(), "password": "Password123"}
    response = client.post("/api/v1/user/create", json=payload)
    assert response.status_code == 422


def test_user_create_invalid_username_format(client):
    payload = {**_unique_user(), "username": "bad user!"}
    response = client.post("/api/v1/user/create", json=payload)
    assert response.status_code == 422


def test_user_create_missing_email(client):
    """Omitting the mandatory email field should return HTTP 422."""
    response = client.post("/api/v1/user/create", json={
        "username": f"u_{uuid.uuid4().hex[:6]}",
        "password": "Password123!",
    })
    assert response.status_code == 422


# ── POST /user/login ───────────────────────────────────────────────────────────

def test_user_login_with_username(client):
    payload = _unique_user()
    client.post("/api/v1/user/create", json=payload)

    response = client.post("/api/v1/user/login", json={
        "username": payload["username"],
        "password": payload["password"],
    })
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["user"]["username"] == payload["username"]


def test_user_login_with_email(client):
    """Login using the email address instead of username."""
    payload = _unique_user()
    client.post("/api/v1/user/create", json=payload)

    response = client.post("/api/v1/user/login", json={
        "username": payload["email"],   # email passed as `username` field
        "password": payload["password"],
    })
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["user"]["email"] == payload["email"]


def test_user_login_wrong_password(client):
    payload = _unique_user()
    client.post("/api/v1/user/create", json=payload)

    response = client.post("/api/v1/user/login", json={
        "username": payload["username"],
        "password": "wrong_password",
    })
    assert response.status_code == 401


# ── GET /user/me ───────────────────────────────────────────────────────────────

def test_user_me_success(client, mock_user_session):
    response = client.get("/api/v1/user/me", headers=mock_user_session["headers"])
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == mock_user_session["user_id"]
    assert data["username"] == mock_user_session["username"]
    assert data["email"] == mock_user_session["email"]


def test_user_me_unauthorized(client, mock_device):
    # Calling /user/me without user session headers returns HTTP 401 or 422
    response = client.get("/api/v1/user/me", headers=mock_device["headers"])
    assert response.status_code in (401, 422)


# ── PATCH /user/me ─────────────────────────────────────────────────────────────

def test_user_update_profile_success(client, mock_user_session):
    """PATCH /user/me updates contact fields and returns updated profile."""
    response = client.patch(
        "/api/v1/user/me",
        headers=mock_user_session["headers"],
        json={"country_code": "+60", "mobile_number": "198765432"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["country_code"] == "+60"
    assert data["mobile_number"] == "198765432"


def test_user_update_email_success(client, mock_user_session):
    """PATCH /user/me allows updating email to a new unique address."""
    new_email = f"updated_{uuid.uuid4().hex[:6]}@test.example.com"
    response = client.patch(
        "/api/v1/user/me",
        headers=mock_user_session["headers"],
        json={"email": new_email},
    )
    assert response.status_code == 200
    assert response.json()["email"] == new_email


def test_user_update_profile_duplicate_email(client, mock_user_session):
    """PATCH /user/me with an email already used by another user returns HTTP 409."""
    # Register a second user
    other_payload = _unique_user()
    other_res = client.post("/api/v1/user/create", json=other_payload)
    assert other_res.status_code == 201

    # Try to update mock_user_session with the other user's email
    response = client.patch(
        "/api/v1/user/me",
        headers=mock_user_session["headers"],
        json={"email": other_payload["email"]},
    )
    assert response.status_code == 409
    assert "email" in response.json()["detail"].lower()


def test_user_update_profile_unauthorized(client, mock_device):
    """PATCH /user/me without user session returns HTTP 401 or 422."""
    response = client.patch(
        "/api/v1/user/me",
        headers=mock_device["headers"],
        json={"country_code": "+1"},
    )
    assert response.status_code in (401, 422)


# ── DELETE /user/me ────────────────────────────────────────────────────────────

def test_delete_user_me_success(client, mock_user_session):
    response = client.delete("/api/v1/user/me", headers=mock_user_session["headers"])
    assert response.status_code == 200


def test_delete_user_me_guest(client, mock_device):
    response = client.delete("/api/v1/user/me", headers=mock_device["headers"])
    assert response.status_code in (401, 422)


def test_delete_user_me_already_deleted(client, mock_user_session):
    # First delete
    res1 = client.delete("/api/v1/user/me", headers=mock_user_session["headers"])
    assert res1.status_code == 200

    # Second delete should be 401 (Unauthorized) because session is revoked
    res2 = client.delete("/api/v1/user/me", headers=mock_user_session["headers"])
    assert res2.status_code == 401


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))
