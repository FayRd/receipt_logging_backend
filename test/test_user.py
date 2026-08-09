#!/usr/bin/env python3
import uuid

def test_user_create_success(client):
    username = f"user_{uuid.uuid4().hex[:6]}"
    response = client.post("/api/v1/user/create", json={
        "username": username,
        "password": "secure_password"
    })
    assert response.status_code == 201
    assert response.json()["username"] == username

def test_user_create_duplicate(client):
    username = f"user_{uuid.uuid4().hex[:6]}"
    payload = {
        "username": username,
        "password": "secure_password"
    }
    res1 = client.post("/api/v1/user/create", json=payload)
    assert res1.status_code == 201
    
    res2 = client.post("/api/v1/user/create", json=payload)
    assert res2.status_code == 409

def test_user_create_short_password(client):
    response = client.post("/api/v1/user/create", json={
        "username": f"user_{uuid.uuid4().hex[:6]}",
        "password": "short"
    })
    assert response.status_code == 422

def test_user_login_success(client):
    username = f"user_{uuid.uuid4().hex[:6]}"
    payload = {
        "username": username,
        "password": "secure_password"
    }
    client.post("/api/v1/user/create", json=payload)
    
    response = client.post("/api/v1/user/login", json=payload)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["user"]["username"] == username

def test_user_login_wrong_password(client):
    username = f"user_{uuid.uuid4().hex[:6]}"
    client.post("/api/v1/user/create", json={
        "username": username,
        "password": "secure_password"
    })
    
    response = client.post("/api/v1/user/login", json={
        "username": username,
        "password": "wrong_password"
    })
    assert response.status_code == 401

def test_user_me_success(client, mock_user_session):
    response = client.get("/api/v1/user/me", headers=mock_user_session["headers"])
    assert response.status_code == 200
    assert response.json()["id"] == mock_user_session["user_id"]
    assert response.json()["username"] == mock_user_session["username"]

def test_user_me_unauthorized(client, mock_device):
    # Missing X-User-ID on guest device
    response = client.get("/api/v1/user/me", headers=mock_device["headers"])
    assert response.status_code == 401

def test_user_me_missing_x_user_id_on_linked_device(client, mock_user_session):
    # Device is linked to user in DB, but client omits X-User-ID header
    incomplete_headers = {
        "X-Device-ID": mock_user_session["headers"]["X-Device-ID"],
        "X-Device-Token": mock_user_session["headers"]["X-Device-Token"],
    }
    get_res = client.get("/api/v1/user/me", headers=incomplete_headers)
    assert get_res.status_code == 401
    assert "Header X-User-ID is required" in get_res.json()["detail"]

    del_res = client.delete("/api/v1/user/me", headers=incomplete_headers)
    assert del_res.status_code == 401
    assert "Header X-User-ID is required" in del_res.json()["detail"]

def test_delete_user_me_success(client, mock_user_session):
    response = client.delete("/api/v1/user/me", headers=mock_user_session["headers"])
    assert response.status_code == 200

def test_delete_user_me_guest(client, mock_device):
    response = client.delete("/api/v1/user/me", headers=mock_device["headers"])
    assert response.status_code == 401

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

