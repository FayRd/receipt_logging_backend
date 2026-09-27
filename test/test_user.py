#!/usr/bin/env python3
import asyncio
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
    """PATCH /user/me with an email already verified by another user returns HTTP 409."""
    import datetime
    from supabase import acreate_client
    from src.config import get_settings

    settings = get_settings()

    # Register a second user
    other_payload = _unique_user()
    other_res = client.post("/api/v1/user/create", json=other_payload)
    assert other_res.status_code == 201
    other_user_id = other_res.json()["id"]

    # Mark second user's email as verified
    async def _verify(uid: str):
        db = await acreate_client(settings.supabase_url, settings.supabase_key)
        await (
            db.table("users")
            .update({"email_verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
            .eq("id", uid)
            .execute()
        )

    asyncio.run(_verify(other_user_id))

    # Try to update mock_user_session with the other user's verified email
    response = client.patch(
        "/api/v1/user/me",
        headers=mock_user_session["headers"],
        json={"email": other_payload["email"]},
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


def test_user_contact_change_cooldown_and_verification_reset(client, mock_user_session):
    """PATCH /user/me with email change resets email_verified_at and enforces 5-min cooldown (HTTP 429)."""
    # 1. First change should succeed
    new_email = f"cooldown_{uuid.uuid4().hex[:6]}@test.example.com"
    res1 = client.patch(
        "/api/v1/user/me",
        headers=mock_user_session["headers"],
        json={"email": new_email},
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["email"] == new_email
    assert data1.get("email_verified_at") is None

    # 2. Second change within 5 minutes must return 429
    res2 = client.patch(
        "/api/v1/user/me",
        headers=mock_user_session["headers"],
        json={"email": f"another_{uuid.uuid4().hex[:6]}@test.example.com"},
    )
    assert res2.status_code == 429
    assert "Please wait and try again later." in res2.json()["detail"]


def test_user_email_conflict_displaces_unverified_and_blocks_verified(client, mock_device):
    """Unverified email can be displaced when another user claims it; verified email returns 409."""
    import datetime
    from supabase import acreate_client
    from src.config import get_settings
    from src.Models.Users.user_repository import UserRepository

    settings = get_settings()

    # 1. Create User A (unverified)
    user_a = _unique_user(suffix="_a")
    res_a = client.post("/api/v1/user/create", json=user_a)
    assert res_a.status_code == 201
    user_a_id = res_a.json()["id"]

    # 2. Create User B
    user_b = _unique_user(suffix="_b")
    res_b = client.post("/api/v1/user/create", json=user_b)
    assert res_b.status_code == 201
    user_b_id = res_b.json()["id"]

    # User B auth headers
    headers_b = {
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
        "X-User-Name": user_b["username"],
        "X-User-Token": user_b["password"],
    }
    # Link device to User B
    client.post(
        "/api/v1/devices/link",
        json={"device_name": mock_device["device_name"], "username": user_b["username"]},
        headers=headers_b,
    )

    # 3. User B claims User A's unverified email -> Should displace User A and succeed
    claim_res = client.patch(
        "/api/v1/user/me",
        headers=headers_b,
        json={"email": user_a["email"]},
    )
    assert claim_res.status_code == 200
    assert claim_res.json()["email"] == user_a["email"]

    # Check User A's email is displaced and verify User B's email
    async def _check_and_verify():
        db = await acreate_client(settings.supabase_url, settings.supabase_key)
        repo = UserRepository(db)
        updated_a = await repo.get_by_id(user_a_id)
        assert updated_a["email"] == f"unverified_{user_a_id}@sancfund.internal"
        await (
            db.table("users")
            .update({"email_verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
            .eq("id", user_b_id)
            .execute()
        )

    asyncio.run(_check_and_verify())

    # 4. Create User C and try to claim User B's now-verified email
    user_c = _unique_user(suffix="_c")
    res_c = client.post("/api/v1/user/create", json=user_c)
    assert res_c.status_code == 201

    headers_c = {
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
        "X-User-Name": user_c["username"],
        "X-User-Token": user_c["password"],
    }
    client.post(
        "/api/v1/devices/link",
        json={"device_name": mock_device["device_name"], "username": user_c["username"]},
        headers=headers_c,
    )

    conflict_res = client.patch(
        "/api/v1/user/me",
        headers=headers_c,
        json={"email": user_a["email"]},  # which is now verified by User B
    )
    assert conflict_res.status_code == 409
    assert "already exists" in conflict_res.json()["detail"].lower()


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


# ── POST /user/me/simulate-trial-expiry ────────────────────────────────────────

def test_simulate_trial_expiry_endpoint(client, mock_user_session):
    """POST /user/me/simulate-trial-expiry fast-forwards trial and applies downgrade idempotently."""
    res1 = client.post("/api/v1/user/me/simulate-trial-expiry", headers=mock_user_session["headers"])
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["success"] is True
    assert data1["tier"] == "free"
    assert data1["is_in_trial"] is False
    assert data1["discount_offer_shown_at"] is not None

    # Verify idempotency
    res2 = client.post("/api/v1/user/me/simulate-trial-expiry", headers=mock_user_session["headers"])
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["success"] is True
    assert data2["tier"] == "free"
    assert data2["is_in_trial"] is False


def test_simulate_trial_expiry_unauthorized(client, mock_device):
    """POST /user/me/simulate-trial-expiry without user token returns HTTP 401 or 422."""
    response = client.post("/api/v1/user/me/simulate-trial-expiry", headers=mock_device["headers"])
    assert response.status_code in (401, 422)


def test_simulate_trial_expiry_forbidden_in_production(client, mock_user_session, monkeypatch):
    """POST /user/me/simulate-trial-expiry in production returns HTTP 403."""
    from src.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")

    response = client.post("/api/v1/user/me/simulate-trial-expiry", headers=mock_user_session["headers"])
    assert response.status_code == 403
    assert "Simulation endpoints are only available in development environment" in response.json()["detail"]



if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))
