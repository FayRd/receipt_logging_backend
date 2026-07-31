import uuid

def test_register_new_device(client):
    device_id = f"DEV-{uuid.uuid4().hex[:6]}"
    token = f"token-{uuid.uuid4().hex}"
    
    response = client.post("/api/v1/devices/register", json={
        "device_id": device_id,
        "device_token": token
    })
    assert response.status_code == 201
    data = response.json()
    assert data["device_id"] == device_id

def test_register_device_idempotent(client, mock_device):
    # Re-registering same device
    response = client.post("/api/v1/devices/register", json={
        "device_id": mock_device["device_id"],
        "device_token": mock_device["device_token"]
    })
    assert response.status_code == 201
    assert response.json()["device_id"] == mock_device["device_id"]

def test_get_device_me_success(client, mock_device):
    response = client.get("/api/v1/devices/me", headers=mock_device["headers"])
    assert response.status_code == 200
    assert response.json()["device_id"] == mock_device["device_id"]

def test_get_device_me_wrong_token(client, mock_device, invalid_headers):
    response = client.get("/api/v1/devices/me", headers=invalid_headers)
    assert response.status_code == 401

def test_device_link_success(client, mock_device):
    # We need a user to link
    username = f"user_{uuid.uuid4().hex[:6]}"
    password = "secure_password123"
    create_res = client.post("/api/v1/user/create", json={
        "username": username,
        "password": password
    })
    assert create_res.status_code == 201
    user_id = create_res.json()["id"]
    
    response = client.post("/api/v1/devices/link", json={
        "device_id": mock_device["device_id"],
        "device_token": mock_device["device_token"],
        "user_id": user_id
    }, headers=mock_device["headers"])
    assert response.status_code == 200
    assert response.json()["user_id"] == user_id

def test_device_link_wrong_token(client, mock_device):
    response = client.post("/api/v1/devices/link", json={
        "device_id": mock_device["device_id"],
        "device_token": "wrong_token123",
        "user_id": "some_user_id"
    }, headers=mock_device["headers"])
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
    res1 = client.post("/api/v1/receipts/", json=payload, headers=mock_device["headers"])
    assert res1.status_code == 201
    receipt_id = res1.json()["id"]

    # 2. Create user
    username = f"user_{uuid.uuid4().hex[:6]}"
    create_res = client.post("/api/v1/user/create", json={"username": username, "password": "secure_password"})
    user_id = create_res.json()["id"]

    # 3. Link device to user
    res2 = client.post("/api/v1/devices/link", json={
        "device_id": mock_device["device_id"],
        "device_token": mock_device["device_token"],
        "user_id": user_id
    }, headers=mock_device["headers"])
    assert res2.status_code == 200

    # 4. Fetch receipt and verify user_id migrated
    # Update headers to include X-User-ID for user fetch
    headers = dict(mock_device["headers"])
    headers["X-User-ID"] = user_id
    res3 = client.get(f"/api/v1/receipts/{receipt_id}", headers=headers)
    assert res3.status_code == 200
    assert res3.json()["user_id"] == user_id

def test_device_unlink_retains_user_data(client, mock_user_session):
    # 1. Create a receipt as logged-in user
    payload = {"receipt": generate_receipt_payload()}
    res1 = client.post("/api/v1/receipts/", json=payload, headers=mock_user_session["headers"])
    assert res1.status_code == 201
    
    # 2. Unlink the device (set user_id = null)
    res2 = client.post("/api/v1/devices/link", json={
        "device_id": mock_user_session["device_id"],
        "device_token": mock_user_session["device_token"],
        "user_id": None
    }, headers=mock_user_session["headers"])
    assert res2.status_code == 200

    # 3. Query receipts as guest (without X-User-ID)
    guest_headers = {
        "X-Device-ID": mock_user_session["device_id"],
        "X-Device-Token": mock_user_session["device_token"]
    }
    res3 = client.get("/api/v1/receipts/", headers=guest_headers)
    assert res3.status_code == 200
    
    # Verify the guest has 0 receipts (data stayed with the user account)
    assert len(res3.json()) == 0
