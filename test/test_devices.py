#!/usr/bin/env python3
import uuid


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
    # Re-registering same device
    response = client.post("/api/v1/devices/register", json={
        "device_name": mock_device["device_name"],
        "device_token": mock_device["device_token"]
    })
    assert response.status_code == 201
    assert response.json()["name"] == mock_device["device_name"]


def test_get_device_me_success(client, mock_device):
    # GET /devices/me requires X-Device-Name and X-Device-Token (omits X-User-Name)
    response = client.get("/api/v1/devices/me", headers=mock_device["headers"])
    assert response.status_code == 200
    assert response.json()["name"] == mock_device["device_name"]


def test_get_device_me_with_uuid_header(client, mock_device):
    # Retrieve device record to get table UUID id
    res_me = client.get("/api/v1/devices/me", headers=mock_device["headers"])
    assert res_me.status_code == 200
    device_uuid = res_me.json()["id"]

    # Verify look up works using the table UUID in X-Device-Name header
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


def test_device_link_requires_all_three_headers(client, mock_device):
    # POST /devices/link missing X-User-Name header must return 401 or 422
    username = f"user_{uuid.uuid4().hex[:6]}"
    response = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "device_token": mock_device["device_token"],
        "username": username
    }, headers=mock_device["headers"])  # mock_device["headers"] has no X-User-Name
    assert response.status_code in (401, 422)


def test_device_link_success(client, mock_device):
    username = f"user_{uuid.uuid4().hex[:6]}"
    password = "secure_password123"
    create_res = client.post("/api/v1/user/create", json={
        "username": username,
        "email": f"{username}@test.example.com",
        "password": password
    })
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]

    # Link with all 3 headers (X-Device-Name, X-Device-Token, X-User-Name)
    link_headers = dict(mock_device["headers"])
    link_headers["X-User-Name"] = username
    
    response = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "device_token": mock_device["device_token"],
        "username": username
    }, headers=link_headers)
    assert response.status_code == 200
    assert response.json()["user_id"] == user_id
    assert response.json()["username"] == username

    # Cleanup user
    client.delete("/api/v1/user/me", headers=link_headers)


def test_device_link_wrong_token(client, mock_device):
    username = f"user_{uuid.uuid4().hex[:6]}"
    link_headers = dict(mock_device["headers"])
    link_headers["X-User-Name"] = username

    response = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "device_token": "wrong_token123",
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
    # 1. Create guest receipt
    payload = {"receipt": generate_receipt_payload()}
    res1 = client.post("/api/v1/receipts/create", json=payload, headers=mock_device["headers"])
    assert res1.status_code == 201
    receipt_id = res1.json()["id"]

    # 2. Create user
    username = f"user_{uuid.uuid4().hex[:6]}"
    create_res = client.post("/api/v1/user/create", json={
        "username": username,
        "email": f"{username}@test.example.com",
        "password": "secure_password",
    })
    user_id = create_res.json()["id"]

    # 3. Link device to user passing all 3 headers
    link_headers = dict(mock_device["headers"])
    link_headers["X-User-Name"] = username

    res2 = client.post("/api/v1/devices/link", json={
        "device_name": mock_device["device_name"],
        "device_token": mock_device["device_token"],
        "username": username
    }, headers=link_headers)
    assert res2.status_code == 200

    # 4. Fetch receipt and verify user_id migrated
    res3 = client.get(f"/api/v1/receipts/{receipt_id}", headers=link_headers)
    assert res3.status_code == 200
    assert res3.json()["user_id"] == user_id

    # Cleanup user and receipt
    client.delete(f"/api/v1/receipts/{receipt_id}", headers=link_headers)
    client.delete("/api/v1/user/me", headers=link_headers)


def test_device_unlink_retains_user_data(client, mock_user_session):
    # 1. Create a receipt as logged-in user
    payload = {"receipt": generate_receipt_payload()}
    res1 = client.post("/api/v1/receipts/create", json=payload, headers=mock_user_session["headers"])
    assert res1.status_code == 201
    receipt_id = res1.json()["id"]
    
    # 2. Unlink the device (set username = null)
    res2 = client.post("/api/v1/devices/link", json={
        "device_name": mock_user_session["device_name"],
        "device_token": mock_user_session["device_token"],
        "username": None
    }, headers=mock_user_session["headers"])
    assert res2.status_code == 200

    # 3. Query receipts as guest (without X-User-Name)
    guest_headers = {
        "X-Device-Name": mock_user_session["device_name"],
        "X-Device-Token": mock_user_session["device_token"]
    }
    res3 = client.get("/api/v1/receipts/", headers=guest_headers)
    assert res3.status_code == 200
    
    # Verify the guest has 0 receipts (data stayed with the user account)
    assert len(res3.json()) == 0

    # Cleanup receipt
    client.delete(f"/api/v1/receipts/{receipt_id}", headers=mock_user_session["headers"])


def test_delete_device_me_success(client, mock_device):
    # DELETE /devices/me omits X-User-Name
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
