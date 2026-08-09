#!/usr/bin/env python3
from unittest.mock import patch, AsyncMock
import pytest

def test_chat_create_success(client, mock_device):
    res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    assert res.status_code == 201
    data = res.json()
    assert "id" in data
    assert "created_at" in data
    assert "updated_at" in data
    assert data["device_id"] == mock_device["device_id"]

def test_chat_create_unauthorized(client):
    res = client.post("/api/v1/chat/create")
    assert res.status_code == 422

def test_chat_create_default_title(client, mock_device):
    res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    assert res.status_code == 201
    assert res.json()["title"] == "New Conversation"

def test_chat_create_custom_title(client, mock_device):
    res = client.post("/api/v1/chat/create", json={"title": "My Expenses"}, headers=mock_device["headers"])
    assert res.status_code == 201
    assert res.json()["title"] == "My Expenses"

def test_chat_create_limit(client, mock_device):
    # Depending on DB state, we might already have some from previous tests
    # Let's just create until we hit 400
    for _ in range(12):
        res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
        if res.status_code == 400:
            assert "limit" in res.json()["detail"].lower()
            break
    else:
        pytest.fail("Did not hit the 10 conversation limit")

def test_chat_list(client, mock_device):
    # Ensures we have at least one to list
    client.post("/api/v1/chat/create", headers=mock_device["headers"])
    res = client.get("/api/v1/chat/list", headers=mock_device["headers"])
    assert res.status_code == 200
    assert isinstance(res.json(), list)
    assert len(res.json()) > 0

def test_chat_history_success(client, mock_device):
    create_res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    conv_id = create_res.json()["id"]
    
    res = client.get(f"/api/v1/chat/history?conversation_id={conv_id}", headers=mock_device["headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] == conv_id
    assert "messages" in data
    assert isinstance(data["messages"], list)

def test_chat_history_unowned(client, mock_device):
    # Device A creates conversation
    create_res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    conv_id = create_res.json()["id"]
    
    # Device B tries to access it
    device_b_id = "DEV-B-100"
    device_b_token = "token-B-123456"
    client.post("/api/v1/devices/register", json={
        "device_id": device_b_id,
        "device_token": device_b_token
    })
    
    headers_b = {
        "X-Device-ID": device_b_id,
        "X-Device-Token": device_b_token
    }
    
    res = client.get(f"/api/v1/chat/history?conversation_id={conv_id}", headers=headers_b)
    assert res.status_code == 404

@patch("src.Services.chat_service.ChatService.generate_response", new_callable=AsyncMock)
def test_chat_query_success(mock_gen, client, mock_device):
    mock_gen.return_value = "Mocked AI response."
    
    # Need a fresh device/convo if limit is reached, mock_device is function scoped so it's fresh
    create_res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    conv_id = create_res.json()["id"]
    
    res = client.post("/api/v1/chat/query", json={
        "conversation_id": conv_id,
        "message": "Hello"
    }, headers=mock_device["headers"])
    
    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] == conv_id
    assert data["user_message"]["content"] == "Hello"
    assert data["assistant_message"]["content"] == "Mocked AI response."
    assert mock_gen.called

def test_chat_query_unowned(client, mock_device):
    import uuid
    res = client.post("/api/v1/chat/query", json={
        "conversation_id": str(uuid.uuid4()),
        "message": "Hello"
    }, headers=mock_device["headers"])
    assert res.status_code == 404

def test_chat_query_missing_message(client, mock_device):
    create_res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    conv_id = create_res.json()["id"]
    
    res = client.post("/api/v1/chat/query", json={
        "conversation_id": conv_id
    }, headers=mock_device["headers"])
    assert res.status_code == 422

def test_delete_chat_success(client, mock_device):
    create_res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    conv_id = create_res.json()["id"]
    
    del_res = client.delete(f"/api/v1/chat/{conv_id}", headers=mock_device["headers"])
    assert del_res.status_code == 200

def test_delete_chat_unowned(client, mock_device):
    # Device A creates conversation
    create_res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    conv_id = create_res.json()["id"]
    
    # Device B tries to delete it
    import uuid
    device_b_id = f"DEV-B-{uuid.uuid4().hex[:6]}"
    device_b_token = f"token-B-{uuid.uuid4().hex}"
    client.post("/api/v1/devices/register", json={
        "device_id": device_b_id,
        "device_token": device_b_token
    })
    
    headers_b = {
        "X-Device-ID": device_b_id,
        "X-Device-Token": device_b_token
    }
    
    del_res = client.delete(f"/api/v1/chat/{conv_id}", headers=headers_b)
    assert del_res.status_code == 404

def test_delete_chat_already_deleted(client, mock_device):
    create_res = client.post("/api/v1/chat/create", headers=mock_device["headers"])
    conv_id = create_res.json()["id"]
    
    res1 = client.delete(f"/api/v1/chat/{conv_id}", headers=mock_device["headers"])
    assert res1.status_code == 200
    
    res2 = client.delete(f"/api/v1/chat/{conv_id}", headers=mock_device["headers"])
    assert res2.status_code == 404


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))


