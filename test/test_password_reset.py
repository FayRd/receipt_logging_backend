#!/usr/bin/env python3
import uuid


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


def test_reset_initiate_success(client):
    user = _unique_user()
    res = client.post("/api/v1/user/create", json=user)
    assert res.status_code == 201

    # Initiate via email
    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    assert res_init.status_code == 200
    data = res_init.json()
    assert data["success"] is True
    assert data["dev_otp"] is not None
    assert len(data["dev_otp"]) == 6


def test_reset_initiate_unknown_identifier_returns_generic_200(client):
    # Non-existent email should still return 200 to prevent user enumeration
    res = client.post("/api/v1/user/reset-password-initiate", json={"identifier": "nonexistent_email_qa_99@test.com"})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["dev_otp"] is None


def test_reset_otp_verification_success(client):
    user = _unique_user()
    client.post("/api/v1/user/create", json=user)

    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    dev_otp = res_init.json()["dev_otp"]

    # Verify OTP
    res_otp = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": dev_otp,
    })
    assert res_otp.status_code == 200
    data_otp = res_otp.json()
    assert data_otp["success"] is True
    assert "reset_token" in data_otp
    assert data_otp["reset_token"].startswith("rst_")


def test_reset_otp_verification_wrong_code(client):
    user = _unique_user()
    client.post("/api/v1/user/create", json=user)
    client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})

    res_wrong = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": "000000",
    })
    assert res_wrong.status_code == 400
    assert "Invalid reset code" in res_wrong.json()["detail"]


def test_end_to_end_password_reset_flow(client):
    user = _unique_user()
    client.post("/api/v1/user/create", json=user)

    # Step 1: Initiate
    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    dev_otp = res_init.json()["dev_otp"]

    # Step 2: Verify OTP
    res_otp = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": dev_otp,
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


def test_reset_token_reuse_rejected(client):
    user = _unique_user()
    client.post("/api/v1/user/create", json=user)

    res_init = client.post("/api/v1/user/reset-password-initiate", json={"identifier": user["email"]})
    dev_otp = res_init.json()["dev_otp"]

    res_otp = client.post("/api/v1/user/reset-password-otp", json={
        "identifier": user["email"],
        "otp": dev_otp,
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
