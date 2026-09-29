#!/usr/bin/env python3
"""Tests for Google OAuth Authentication (OIDC ID Token verification, account linking, and collision handling)."""

import uuid
from unittest.mock import patch, MagicMock


def _mock_claims(
    sub: str | None = None,
    email: str | None = None,
    name: str = "Test User",
    email_verified: bool = True,
    picture: str | None = "https://example.com/avatar.jpg",
) -> dict:
    raw_id = uuid.uuid4().hex[:8]
    return {
        "sub": sub or f"gid_{raw_id}",
        "email": email or f"guser_{raw_id}@gmail.com",
        "name": name,
        "email_verified": email_verified,
        "picture": picture,
        "iss": "https://accounts.google.com",
    }


# ── 1. Invalid Token Handling ──────────────────────────────────────────────────

def test_google_auth_invalid_token(client):
    """Submitting an invalid ID token triggers 401 Unauthorized."""
    res = client.post("/api/v1/user/auth/google", json={"id_token": "invalid_fake_token"})
    assert res.status_code == 401
    assert "Invalid Google authentication token." in res.json()["detail"]


# ── 2. New User Registration Workflow ──────────────────────────────────────────

def test_google_auth_new_user_needs_username(client):
    """New Google user without username is prompted with needs_username=True and a suggested username."""
    claims = _mock_claims(name="Alice Wonderland")
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims):
        res = client.post("/api/v1/user/auth/google", json={"id_token": "valid_token"})
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["needs_username"] is True
        assert data["email"] == claims["email"]
        assert data["suggested_username"] is not None
        assert len(data["suggested_username"]) >= 3


def test_google_auth_new_user_invalid_username(client):
    """Submitting an invalid username format returns 422 Unprocessable Entity."""
    claims = _mock_claims()
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims):
        res = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": "ab"},  # Too short (<3 chars)
        )
        assert res.status_code == 422
        assert "Username must be 3-10 characters" in res.json()["detail"]


def test_google_auth_new_user_taken_username(client):
    """Submitting an already-taken username returns 409 Conflict."""
    # First create a user with a specific username
    taken_username = f"u_{uuid.uuid4().hex[:4]}"[:10]
    client.post(
        "/api/v1/user/create",
        json={
            "username": taken_username,
            "email": f"{taken_username}@example.com",
            "password": "Password123!",
        },
    )

    claims = _mock_claims()
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims):
        res = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": taken_username},
        )
        assert res.status_code == 409
        assert "Username already taken." in res.json()["detail"]


def test_google_auth_new_user_success(client):
    """Submitting a valid username registers the user with null password and returns JWT tokens."""
    chosen_username = f"g_{uuid.uuid4().hex[:4]}"[:10]
    claims = _mock_claims()
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims):
        res = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": chosen_username},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["needs_username"] is False
        assert data["access_token"] is not None
        assert data["refresh_token"] is not None
        assert data["user"]["username"] == chosen_username
        assert data["user"]["email"] == claims["email"]
        assert data["user"]["google_id"] == claims["sub"]
        assert data["user"]["email_verified_at"] is not None


# ── 3. Existing User Login by Google ID ───────────────────────────────────────

def test_google_auth_existing_user_by_google_id(client):
    """Subsequent logins by an existing Google ID succeed immediately without username prompt."""
    chosen_username = f"g_{uuid.uuid4().hex[:4]}"[:10]
    claims = _mock_claims()

    with patch("src.API.v1.user.verify_google_id_token", return_value=claims):
        # 1. Sign up
        res1 = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": chosen_username},
        )
        assert res1.status_code == 200

        # 2. Login again without username
        res2 = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token"},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["success"] is True
        assert data2["needs_username"] is False
        assert data2["user"]["username"] == chosen_username
        assert data2["access_token"] is not None


# ── 4. Duplicate Email Handling (Requirement 1) ───────────────────────────────

def test_google_auth_duplicate_verified_email(client):
    """If email is already in use AND verified by an existing account, reject with 409 'Email already in use.'"""
    raw = uuid.uuid4().hex[:6]
    verified_email = f"verified_{raw}@example.com"
    username = f"u_{raw[:4]}"[:10]

    # 1. Register first user via Google (which verifies email)
    claims1 = _mock_claims(sub=f"gid_orig_{raw}", email=verified_email)
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims1):
        res1 = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": username},
        )
        assert res1.status_code == 200
        assert res1.json()["user"]["email_verified_at"] is not None

    # 2. Attempt Google auth with same email but different google_id
    claims2 = _mock_claims(sub=f"gid_other_{raw}", email=verified_email)
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims2):
        res2 = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token"},
        )
        assert res2.status_code == 409
        assert "Email already in use." in res2.json()["detail"]


def test_google_auth_unverified_email_claim_and_wipe_password(client):
    """If email matches an unverified account, claim it, set email_verified_at, link google_id, and wipe password stub."""
    raw = uuid.uuid4().hex[:6]
    unverified_email = f"unverified_{raw}@example.com"
    username = f"u_{raw[:4]}"[:10]

    # Create account (email_verified_at is None by default)
    res_create = client.post(
        "/api/v1/user/create",
        json={"username": username, "email": unverified_email, "password": "AttackerPassword123!"},
    )
    assert res_create.status_code == 201
    user_id = res_create.json()["id"]

    # Google user signs in with the same email
    claims = _mock_claims(email=unverified_email)
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims):
        res = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["user"]["id"] == user_id
        assert data["user"]["email_verified_at"] is not None
        assert data["user"]["google_id"] == claims["sub"]

    # OWASP Defense Verification: Attacker cannot log in with the old password anymore!
    res_login = client.post(
        "/api/v1/user/login",
        json={"username": unverified_email, "password": "AttackerPassword123!"},
    )
    assert res_login.status_code == 401
    assert "Invalid username or password." in res_login.json()["detail"]


# ── 5. Passwordless Login Guard ───────────────────────────────────────────────

def test_google_user_cannot_login_with_password(client):
    """A Google-only user with password=None cannot log in via password endpoint and gets 401 without 500 error."""
    chosen_username = f"g_{uuid.uuid4().hex[:4]}"[:10]
    claims = _mock_claims()
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims):
        res = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": chosen_username},
        )
        assert res.status_code == 200

    # Attempt to log in with an arbitrary password
    res_login = client.post(
        "/api/v1/user/login",
        json={"username": chosen_username, "password": "AnyPassword123!"},
    )
    assert res_login.status_code == 401
    assert "Invalid username or password." in res_login.json()["detail"]


# ── 6. is_new_user Flag & Avatar Ingestion Tests ──────────────────────────────

def test_google_auth_new_user_flag_and_avatar_ingestion(client):
    """Verify is_new_user=True on first creation and avatar is uploaded to storage."""
    raw = uuid.uuid4().hex[:6]
    chosen_username = f"av_{raw[:4]}"[:10]
    picture_url = "https://lh3.googleusercontent.com/test_avatar.jpg"
    claims = _mock_claims(picture=picture_url)

    # Mock httpx download and image_storage upload
    mock_avatar_bytes = b"\xff\xd8\xff\xe0fake_jpeg_binary"
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims), \
         patch("httpx.AsyncClient.get") as mock_get, \
         patch("src.Services.image_service.ImageStorageService.upload_avatar", return_value="fake_user_id/avatar_images/") as mock_upload:

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = mock_avatar_bytes
        mock_get.return_value = mock_resp

        res = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": chosen_username},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["is_new_user"] is True
        assert data["user"]["avatar_image_path"] == "fake_user_id/avatar_images/"
        assert mock_upload.called

        # Second login with same google_id returns is_new_user=False
        res_repeat = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token"},
        )
        assert res_repeat.status_code == 200
        data_repeat = res_repeat.json()
        assert data_repeat["is_new_user"] is False


def test_google_auth_avatar_download_failure_graceful(client):
    """If Google avatar download fails, user creation still succeeds gracefully."""
    raw = uuid.uuid4().hex[:6]
    chosen_username = f"fl_{raw[:4]}"[:10]
    picture_url = "https://lh3.googleusercontent.com/broken_avatar.jpg"
    claims = _mock_claims(picture=picture_url)

    with patch("src.API.v1.user.verify_google_id_token", return_value=claims), \
         patch("httpx.AsyncClient.get", side_effect=Exception("CDN Timeout")):

        res = client.post(
            "/api/v1/user/auth/google",
            json={"id_token": "valid_token", "username": chosen_username},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["is_new_user"] is True
        assert data["user"]["avatar_image_path"] is None


def test_google_auth_device_trial_reuse_rejected(client):
    """When a device has already consumed a trial, subsequent Google registrations are placed on Free tier."""
    raw = uuid.uuid4().hex[:6]
    device_id = f"dev_test_reuse_{raw}"
    
    # 1. First user registers with device_id -> gets trial
    claims1 = _mock_claims()
    username1 = f"tr1_{raw[:4]}"[:10]
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims1):
        res1 = client.post(
            "/api/v1/user/auth/google",
            json={
                "id_token": "valid_token",
                "username": username1,
                "preferences": {"trial_device_id": device_id},
            },
        )
        assert res1.status_code == 200
        user1 = res1.json()["user"]
        assert user1["tier"] == "premium"
        assert user1["preferences"].get("is_in_trial") is True

    # 2. Second user registers with the SAME device_id -> trial rejected (Free tier)
    claims2 = _mock_claims()
    username2 = f"tr2_{raw[:4]}"[:10]
    with patch("src.API.v1.user.verify_google_id_token", return_value=claims2):
        res2 = client.post(
            "/api/v1/user/auth/google",
            json={
                "id_token": "valid_token",
                "username": username2,
                "preferences": {"trial_device_id": device_id},
            },
        )
        assert res2.status_code == 200
        user2 = res2.json()["user"]
        assert user2["tier"] == "free"
        assert user2["preferences"].get("is_in_trial") is False
        assert user2["preferences"].get("trial_ineligible") is True

