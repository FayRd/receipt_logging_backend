"""Integration tests for Active JWT Revocation & Token Denylist on Account Deletion (TODO-33).

Validates:
- IT-01: Immediate token rejection upon DELETE /api/v1/user/me.
- IT-02: Multi-device revocation on account deletion (dual-layer user + JTI revocation).
- IT-03: Refresh token rejected for renewal post account deletion.
- IT-04: Cross-channel web deletion (POST /api/v1/web/confirm-deletion) immediately revokes mobile JWT.
- IT-05: Single-use refresh token rotation with denylisting preventing token reuse replay.
- IT-06: Concurrency and race safety during deletion cascade execution.
"""

import concurrent.futures
import uuid
import pytest
from src.Infrastructure.redis_service import store_otp
from src.Services.token_denylist_service import clear_memory_denylist


@pytest.fixture(autouse=True)
def clean_stores():
    """Reset denylist memory store between integration tests."""
    clear_memory_denylist()
    yield
    clear_memory_denylist()


def _unique_user_payload():
    uid = uuid.uuid4().hex[:6]
    return {
        "username": f"u_{uid}",
        "email": f"jwt_{uid}@example.com",
        "password": "StrongSecretPass123!",
    }


def _create_and_login_user(client):
    payload = _unique_user_payload()
    reg_res = client.post("/api/v1/user/create", json=payload)
    assert reg_res.status_code == 201, f"User registration failed: {reg_res.text}"

    login_res = client.post("/api/v1/user/login", json={
        "username": payload["username"],
        "password": payload["password"],
    })
    assert login_res.status_code == 200, f"User login failed: {login_res.text}"
    login_data = login_res.json()

    access_token = login_data["access_token"]
    refresh_token = login_data["refresh_token"]
    user_id = login_data["user"]["id"]

    return {
        "user_id": user_id,
        "username": payload["username"],
        "email": payload["email"],
        "password": payload["password"],
        "access_token": access_token,
        "refresh_token": refresh_token,
        "headers": {"Authorization": f"Bearer {access_token}"},
    }


class TestJWTRevocationIntegration:
    """End-to-end integration test suite for TODO-33."""

    def test_account_deletion_immediate_token_rejection(self, client):
        """IT-01: Token used to delete account is immediately rejected on subsequent requests."""
        user_session = _create_and_login_user(client)
        headers = user_session["headers"]

        # 1. Verify token is initially active and accepted
        me_res = client.get("/api/v1/user/me", headers=headers)
        assert me_res.status_code == 200, f"Expected 200, got: {me_res.text}"
        assert me_res.json()["username"] == user_session["username"]

        # 2. Issue account deletion
        del_res = client.delete("/api/v1/user/me", headers=headers)
        assert del_res.status_code == 200, f"Deletion failed: {del_res.text}"
        assert del_res.json()["success"] is True

        # 3. Immediately re-query /user/me with the same token -> must return 401 Revoked
        after_del = client.get("/api/v1/user/me", headers=headers)
        assert after_del.status_code == 401, f"Expected 401 Unauthorized, got: {after_del.status_code}"
        assert "revoked" in after_del.json().get("detail", "").lower(), (
            f"Expected detail to indicate token was revoked, got: {after_del.json()}"
        )

    def test_multi_device_revocation_on_deletion(self, client):
        """IT-02: Account deletion from Device A immediately invalidates active session on Device B."""
        user_session = _create_and_login_user(client)

        # Device A token
        token_a = user_session["access_token"]
        headers_a = {"Authorization": f"Bearer {token_a}"}

        # Device B logs in separately to obtain an independent JWT with distinct JTI
        login_b = client.post("/api/v1/user/login", json={
            "username": user_session["username"],
            "password": user_session["password"],
        })
        assert login_b.status_code == 200
        token_b = login_b.json()["access_token"]
        headers_b = {"Authorization": f"Bearer {token_b}"}

        assert token_a != token_b, "Tokens across sessions must have distinct JTIs"

        # Both sessions are currently valid
        assert client.get("/api/v1/user/me", headers=headers_a).status_code == 200
        assert client.get("/api/v1/user/me", headers=headers_b).status_code == 200

        # Device A deletes the account
        del_res = client.delete("/api/v1/user/me", headers=headers_a)
        assert del_res.status_code == 200

        # Device B must be immediately rejected (user-level revocation in denylist)
        res_b = client.get("/api/v1/user/me", headers=headers_b)
        assert res_b.status_code == 401, f"Session B was not revoked; got HTTP {res_b.status_code}"
        assert "revoked" in res_b.json().get("detail", "").lower()

    def test_refresh_token_rejected_after_account_deletion(self, client):
        """IT-03: Refresh tokens cannot be used to renew sessions after account deletion."""
        user_session = _create_and_login_user(client)
        refresh_token = user_session["refresh_token"]

        # Delete account using access token
        del_res = client.delete("/api/v1/user/me", headers=user_session["headers"])
        assert del_res.status_code == 200

        # Attempt to refresh token post-deletion
        refresh_res = client.post("/api/v1/user/refresh", json={"refresh_token": refresh_token})
        assert refresh_res.status_code == 401
        assert "revoked" in refresh_res.json().get("detail", "").lower() or (
            "not found" in refresh_res.json().get("detail", "").lower()
        )

    def test_web_deletion_revokes_api_jwt(self, client):
        """IT-04: Account deletion via web flow (POST /api/v1/web/confirm-deletion) revokes API JWT."""
        user_session = _create_and_login_user(client)
        email = user_session["email"]
        user_id = user_session["user_id"]
        headers = user_session["headers"]

        # Pre-seed OTP in Redis/memory store for web deletion
        otp_code = "889900"
        store_otp(
            user_id=user_id,
            target_type="web_deletion",
            identifier=email,
            otp=otp_code,
        )

        # Execute web deletion confirmation
        confirm_res = client.post("/api/v1/web/confirm-deletion", json={
            "email": email,
            "otp": otp_code,
            "website": "",
        })
        assert confirm_res.status_code == 200, f"Web deletion failed: {confirm_res.text}"

        # Mobile API token must now be rejected
        after_web_del = client.get("/api/v1/user/me", headers=headers)
        assert after_web_del.status_code == 401, f"Expected 401, got: {after_web_del.status_code}"
        assert "revoked" in after_web_del.json().get("detail", "").lower()

    def test_refresh_token_rotation_single_use(self, client):
        """IT-05: Consumed refresh tokens are denylisted and rejected upon replay (single-use rotation)."""
        user_session = _create_and_login_user(client)
        initial_refresh_token = user_session["refresh_token"]

        # 1. First refresh attempt: Valid, returns new token pair
        rotate_res = client.post("/api/v1/user/refresh", json={"refresh_token": initial_refresh_token})
        assert rotate_res.status_code == 200, f"Token refresh failed: {rotate_res.text}"
        data = rotate_res.json()
        assert data["success"] is True
        rotated_access_token = data["access_token"]
        rotated_refresh_token = data["refresh_token"]

        assert rotated_access_token != user_session["access_token"]
        assert rotated_refresh_token != initial_refresh_token

        # 2. Second refresh attempt (Replay attack with consumed refresh token) -> MUST FAIL 401
        replay_res = client.post("/api/v1/user/refresh", json={"refresh_token": initial_refresh_token})
        assert replay_res.status_code == 401, (
            f"Expected 401 for replayed refresh token, got: {replay_res.status_code}"
        )
        assert "revoked" in replay_res.json().get("detail", "").lower()

        # 3. New rotated refresh token continues to work for subsequent rotation
        next_rotate = client.post("/api/v1/user/refresh", json={"refresh_token": rotated_refresh_token})
        assert next_rotate.status_code == 200

        # Clean up test user
        client.delete("/api/v1/user/me", headers={"Authorization": f"Bearer {next_rotate.json()['access_token']}"})

    def test_concurrent_requests_during_deletion(self, client):
        """IT-06: Concurrency stress test: verify no race conditions leak data during deletion cascade."""
        user_session = _create_and_login_user(client)
        headers = user_session["headers"]

        results = []

        def make_get_request():
            return client.get("/api/v1/user/me", headers=headers)

        def make_delete_request():
            return client.delete("/api/v1/user/me", headers=headers)

        # Launch 10 read requests concurrently with 1 delete request
        with concurrent.futures.ThreadPoolExecutor(max_workers=11) as executor:
            futures = [executor.submit(make_get_request) for _ in range(5)]
            futures.append(executor.submit(make_delete_request))
            futures.extend([executor.submit(make_get_request) for _ in range(5)])

            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        statuses = [r.status_code for r in results]
        # Valid statuses during race are 200 (pre-deletion or deletion itself) and 401 (post-deletion)
        # Absolutely NO 500 internal server errors allowed
        for status in statuses:
            assert status in (200, 401, 404), f"Unexpected status code {status} during concurrent deletion"

        # Subsequent request after all concurrency completes MUST be 401
        final_check = client.get("/api/v1/user/me", headers=headers)
        assert final_check.status_code == 401
        assert "revoked" in final_check.json().get("detail", "").lower()
