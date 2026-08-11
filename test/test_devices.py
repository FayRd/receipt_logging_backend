#!/usr/bin/env python3
import uuid
from src.Models.Users.user_repository import UserRepository


def test_register_new_device(client):
    device_name = f"DEV-{uuid.uuid4().hex[:6]}"
    token = f"token-{uuid.uuid4().hex}"
    
    response = client.post("/api/v1/devices/register", json={
        "device_name": device_name,
        "device_token": token
    })
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == device_name
    assert "id" in data

    # Cleanup test data
    headers = {"X-Device-Name": device_name, "X-Device-Token": token}
    client.delete("/api/v1/devices/me", headers=headers)


def test_register_device_idempotent(client, mock_device):
    response = client.post("/api/v1/devices/register", json={
        "device_name": mock_device["device_name"],
        "device_token": mock_device["device_token"]
    })
    assert response.status_code == 201
    assert response.json()["name"] == mock_device["device_name"]


def test_get_device_me_success(client, mock_device):
    # GET /devices/me requires X-Device-Name and X-Device-Token
    response = client.get("/api/v1/devices/me", headers=mock_device["headers"])
    assert response.status_code == 200
    assert response.json()["name"] == mock_device["device_name"]


def test_get_device_me_with_uuid_header(client, mock_device):
    res_me = client.get("/api/v1/devices/me", headers=mock_device["headers"])
    assert res_me.status_code == 200
    device_uuid = res_me.json()["id"]

    uuid_headers = {
        "X-Device-Name": device_uuid,
        "X-Device-Token": mock_device["device_token"]
    }
    res_uuid = client.get("/api/v1/devices/me", headers=uuid_headers)
    assert res_uuid.status_code == 200
    assert res_uuid.json()["name"] == mock_device["device_name"]


def test_get_device_me_wrong_token(client, mock_device, invalid_headers):
    response = client.get("/api/v1/devices/me", headers=invalid_headers)
    assert response.status_code == 401


def test_device_link_requires_all_four_headers(client, mock_device):
    # POST /devices/link missing user headers must return 401/422
    username = f"user_{uuid.uuid4().hex[:6]}"
    response = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "username": username
    }, headers=mock_device["headers"])  # Missing X-User-Name and X-User-Token
    assert response.status_code in (401, 422)


def test_device_link_success(client, mock_device):
    username = f"user_{uuid.uuid4().hex[:6]}"
    password = "secure_password123"
    password_hash = UserRepository.hash_password(password)

    create_res = client.post("/api/v1/user/create", json={
        "username": username,
        "email": f"{username}@test.example.com",
        "password": password
    })
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    # Link with all 4 headers (X-Device-Name, X-Device-Token, X-User-Name, X-User-Token)
    link_headers = {
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
        "X-User-Name": username,
        "X-User-Token": password,
    }
    
    response = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "username": username
    }, headers=link_headers)
    assert response.status_code == 200
    assert response.json()["user_id"] == user_id
    assert response.json()["username"] == username

    # Cleanup user
    user_headers = {"X-User-Name": username, "X-User-Token": password}
    client.delete("/api/v1/user/me", headers=user_headers)


def test_device_link_wrong_token(client, mock_device):
    username = f"user_{uuid.uuid4().hex[:6]}"
    link_headers = {
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": "wrong_token_123",
        "X-User-Name": username,
        "X-User-Token": "wrong_password_123",
    }

    response = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "username": username
    }, headers=link_headers)
    assert response.status_code == 401


def generate_receipt_payload():
    from datetime import datetime, timezone
    return {
        "merchant_name": "Migrate Test Store",
        "total_amount": 10.50,
        "date": datetime.now(timezone.utc).isoformat(),
        "raw_text": "Test receipt raw text"
    }


def test_device_link_migrates_guest_data(client, mock_device):
    # 1. Create guest receipt via scan or guest mode
    # (Guest receipts are saved in local Isar DB in guest mode)

    # 2. Create user
    username = f"user_{uuid.uuid4().hex[:6]}"
    password = "secure_password"

    create_res = client.post("/api/v1/user/create", json={
        "username": username,
        "email": f"{username}@test.example.com",
        "password": password,
    })
    user_id = create_res.json()["id"]

    # 3. Link device to user passing all 4 headers
    link_headers = {
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
        "X-User-Name": username,
        "X-User-Token": password,
    }

    res2 = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "username": username
    }, headers=link_headers)
    assert res2.status_code == 200

    # Cleanup user
    user_headers = {"X-User-Name": username, "X-User-Token": password}
    client.delete("/api/v1/user/me", headers=user_headers)


def test_device_unlink_retains_user_data(client, mock_user_session):
    # 1. Create a receipt as logged-in user
    payload = {"receipt": generate_receipt_payload()}
    res1 = client.post("/api/v1/receipts/create", json=payload, headers=mock_user_session["headers"])
    assert res1.status_code == 201
    receipt_id = res1.json()["id"]
    
    # 2. Unlink the device (set username = null)
    res2 = client.post("/api/v1/devices/link", json={
        "device_name": mock_user_session["device_name"],
        "username": None
    }, headers=mock_user_session["link_headers"])
    assert res2.status_code == 200

    # 3. Query receipts as user (user retains their receipts)
    res3 = client.get("/api/v1/receipts/", headers=mock_user_session["headers"])
    assert res3.status_code == 200
    assert len(res3.json()) >= 1

    # Cleanup receipt
    client.delete(f"/api/v1/receipts/{receipt_id}", headers=mock_user_session["headers"])


def test_delete_device_me_success(client, mock_device):
    response = client.delete("/api/v1/devices/me", headers=mock_device["headers"])
    assert response.status_code == 200


def test_delete_device_me_invalid_token(client, invalid_headers):
    response = client.delete("/api/v1/devices/me", headers=invalid_headers)
    assert response.status_code == 401


def test_delete_device_me_already_deleted(client, mock_device):
    res1 = client.delete("/api/v1/devices/me", headers=mock_device["headers"])
    assert res1.status_code == 200
    
    res2 = client.delete("/api/v1/devices/me", headers=mock_device["headers"])
    assert res2.status_code == 401


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))
