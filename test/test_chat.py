#!/usr/bin/env python3
from unittest.mock import patch, AsyncMock
import pytest
import uuid
from src.Models.Users.user_repository import UserRepository


def test_chat_create_success(client, mock_user_session):
    res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    assert res.status_code == 201
    data = res.json()
    assert "id" in data
    assert "created_at" in data
    assert "updated_at" in data


def test_chat_create_unauthorized(client):
    res = client.post("/api/v1/chat/create")
    assert res.status_code == 422


def test_chat_create_default_title(client, mock_user_session):
    res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    assert res.status_code == 201
    assert res.json()["title"] == "New Conversation"


def test_chat_create_custom_title(client, mock_user_session):
    res = client.post("/api/v1/chat/create", json={"title": "My Expenses"}, headers=mock_user_session["headers"])
    assert res.status_code == 201
    assert res.json()["title"] == "My Expenses"


def test_chat_create_limit(client, mock_user_session):
    for _ in range(12):
        res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
        if res.status_code == 400:
            assert "limit" in res.json()["detail"].lower()
            break
    else:
        pytest.fail("Did not hit the 10 conversation limit")


def test_chat_list(client, mock_user_session):
    client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    res = client.get("/api/v1/chat/list", headers=mock_user_session["headers"])
    assert res.status_code == 200
    assert isinstance(res.json(), list)
    assert len(res.json()) > 0


def test_chat_history_success(client, mock_user_session):
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]
    
    res = client.get(f"/api/v1/chat/history?conversation_id={conv_id}", headers=mock_user_session["headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] == conv_id


def test_chat_history_unowned(client, mock_user_session):
    # User A creates conversation
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]
    
    # User B tries to access it
    user_b_name = f"user_b_{uuid.uuid4().hex[:6]}"
    password = "password_123"
    create_b = client.post("/api/v1/user/create", json={
        "username": user_b_name,
        "email": f"{user_b_name}@test.com",
        "password": password
    })
    assert create_b.status_code == 201

    headers_b = {
        "X-User-Name": user_b_name,
        "X-User-Token": UserRepository.hash_password(password)
    }
    
    res = client.get(f"/api/v1/chat/history?conversation_id={conv_id}", headers=headers_b)
    assert res.status_code == 404

    client.delete("/api/v1/user/me", headers=headers_b)


@patch("src.Services.chat_service.ChatService.generate_response", new_callable=AsyncMock)
def test_chat_query_success(mock_gen, client, mock_user_session):
    mock_gen.return_value = "Mocked AI response."
    
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]
    
    res = client.post("/api/v1/chat/query", json={
        "conversation_id": conv_id,
        "message": "Hello"
    }, headers=mock_user_session["headers"])
    
    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] == conv_id
    assert data["user_message"]["content"] == "Hello"
    assert data["assistant_message"]["content"] == "Mocked AI response."
    assert mock_gen.called


def test_chat_query_unowned(client, mock_user_session):
    res = client.post("/api/v1/chat/query", json={
        "conversation_id": str(uuid.uuid4()),
        "message": "Hello"
    }, headers=mock_user_session["headers"])
    assert res.status_code == 404


def test_chat_query_missing_message(client, mock_user_session):
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]
    
    res = client.post("/api/v1/chat/query", json={
        "conversation_id": conv_id
    }, headers=mock_user_session["headers"])
    assert res.status_code == 422


def test_delete_chat_success(client, mock_user_session):
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]
    
    del_res = client.delete(f"/api/v1/chat/{conv_id}", headers=mock_user_session["headers"])
    assert del_res.status_code == 200


def test_delete_chat_unowned(client, mock_user_session):
    # User A creates conversation
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]
    
    # User B tries to delete it
    user_b_name = f"user_b_{uuid.uuid4().hex[:6]}"
    password = "password_123"
    create_b = client.post("/api/v1/user/create", json={
        "username": user_b_name,
        "email": f"{user_b_name}@test.com",
        "password": password
    })
    assert create_b.status_code == 201

    headers_b = {
        "X-User-Name": user_b_name,
        "X-User-Token": UserRepository.hash_password(password)
    }
    
    del_res = client.delete(f"/api/v1/chat/{conv_id}", headers=headers_b)
    assert del_res.status_code == 404

    client.delete("/api/v1/user/me", headers=headers_b)


def test_delete_chat_already_deleted(client, mock_user_session):
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]
    
    del_res = client.delete(f"/api/v1/chat/{conv_id}", headers=mock_user_session["headers"])
    assert del_res.status_code == 200
    
    del_res_2 = client.delete(f"/api/v1/chat/{conv_id}", headers=mock_user_session["headers"])
    assert del_res_2.status_code == 404


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))
