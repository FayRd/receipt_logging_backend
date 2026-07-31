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
    })
    assert response.status_code == 200
    assert response.json()["user_id"] == user_id

def test_device_link_wrong_token(client, mock_device):
    response = client.post("/api/v1/devices/link", json={
        "device_id": mock_device["device_id"],
        "device_token": "wrong_token123",
        "user_id": "some_user_id"
    })
    assert response.status_code == 401
