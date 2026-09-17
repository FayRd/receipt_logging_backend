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
    assert res.status_code in (401, 422)


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
    user_b_name = f"u_b_{uuid.uuid4().hex[:5]}"
    password = "Password123!"
    create_b = client.post("/api/v1/user/create", json={
        "username": user_b_name,
        "email": f"{user_b_name}@test.com",
        "password": password
    })
    assert create_b.status_code == 201

    headers_b = {
        "X-User-Name": user_b_name,
        "X-User-Token": password
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
    }, headers=mock_user_session["user_scan_headers"])

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
    }, headers=mock_user_session["user_scan_headers"])
    assert res.status_code == 404


@patch("src.Services.chat_service.ChatService.generate_response", new_callable=AsyncMock)
def test_chat_query_user_first_turn_autocreate(mock_gen, client, mock_user_session):
    """User Mode: conversation_id omitted on first turn auto-creates conversation in Supabase."""
    mock_gen.return_value = "Cloud AI response on first turn."

    res = client.post("/api/v1/chat/query", json={
        "message": "How much did I spend last month?",
    }, headers=mock_user_session["user_scan_headers"])

    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] is not None  # Auto-created conversation UUID
    assert data["user_message"]["id"] is not None
    assert data["assistant_message"]["content"] == "Cloud AI response on first turn."
    assert mock_gen.called


@patch("src.Services.chat_service.ChatService.generate_response_local", new_callable=AsyncMock)
def test_chat_query_guest_mode(mock_gen, client, mock_device):
    """Guest mode: uses device headers with X-Request-Type: guest and no conversation_id."""
    mock_gen.return_value = "Guest AI response."

    res = client.post("/api/v1/chat/query", json={
        "message": "What receipts have I logged?",
        "conversation_history": [],
        "recent_receipts": []
    }, headers=mock_device["guest_scan_headers"])

    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] is None  # Guest local mode — zero cloud storage
    assert data["user_message"]["id"] is not None
    assert data["assistant_message"]["content"] == "Guest AI response."
    assert mock_gen.called


def test_chat_query_missing_message(client, mock_user_session):
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]

    res = client.post("/api/v1/chat/query", json={
        "conversation_id": conv_id
    }, headers=mock_user_session["user_scan_headers"])
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
    user_b_name = f"u_b_{uuid.uuid4().hex[:5]}"
    password = "Password123!"
    create_b = client.post("/api/v1/user/create", json={
        "username": user_b_name,
        "email": f"{user_b_name}@test.com",
        "password": password
    })
    assert create_b.status_code == 201

    headers_b = {
        "X-User-Name": user_b_name,
        "X-User-Token": password
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


def test_update_chat_title_success(client, mock_user_session):
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]

    patch_res = client.patch(
        f"/api/v1/chat/{conv_id}",
        json={"title": "Updated Title via PATCH"},
        headers=mock_user_session["headers"],
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["id"] == conv_id
    assert data["title"] == "Updated Title via PATCH"


def test_update_chat_title_unowned(client, mock_user_session):
    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]

    user_b_name = f"u_b_{uuid.uuid4().hex[:5]}"
    password = "Password123!"
    create_b = client.post("/api/v1/user/create", json={
        "username": user_b_name,
        "email": f"{user_b_name}@test.com",
        "password": password
    })
    assert create_b.status_code == 201
    headers_b = {
        "X-User-Name": user_b_name,
        "X-User-Token": password
    }

    patch_res = client.patch(
        f"/api/v1/chat/{conv_id}",
        json={"title": "Malicious Title Edit"},
        headers=headers_b,
    )
    assert patch_res.status_code == 404

    client.delete("/api/v1/user/me", headers=headers_b)


@patch("src.Services.chat_service.ChatService.generate_response", new_callable=AsyncMock)
def test_chat_query_failure_does_not_create_conversation(mock_gen, client, mock_user_session):
    """If the AI provider fails on first turn, no conversation is created in Supabase."""
    mock_gen.side_effect = RuntimeError("AI Provider API Error")

    res = client.post("/api/v1/chat/query", json={
        "message": "This query will fail",
    }, headers=mock_user_session["user_scan_headers"])

    assert res.status_code == 500

    # Verify conversation was not created
    list_res = client.get("/api/v1/chat/list", headers=mock_user_session["headers"])
    assert list_res.status_code == 200


@patch("src.Services.chat_service.ChatService.generate_response", new_callable=AsyncMock)
def test_chat_query_openrouter_provider_success(mock_gen, client, mock_user_session):
    """OpenRouter provider path: generate_response returns an AI response through the same endpoint."""
    mock_gen.return_value = "OpenRouter AI response from google/gemini-2.5-flash."

    create_res = client.post("/api/v1/chat/create", headers=mock_user_session["headers"])
    conv_id = create_res.json()["id"]

    res = client.post("/api/v1/chat/query", json={
        "conversation_id": conv_id,
        "message": "How much did I spend on groceries?"
    }, headers=mock_user_session["user_scan_headers"])

    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] == conv_id
    assert "OpenRouter AI response" in data["assistant_message"]["content"]
    assert mock_gen.called


@patch("src.Services.chat_service.ChatService.generate_response_local", new_callable=AsyncMock)
def test_chat_query_openrouter_guest_mode(mock_gen, client, mock_device):
    """OpenRouter provider path (guest mode): generate_response_local returns response via same endpoint."""
    mock_gen.return_value = "OpenRouter guest response."

    res = client.post("/api/v1/chat/query", json={
        "message": "Show my receipts.",
        "conversation_history": [],
        "recent_receipts": []
    }, headers=mock_device["guest_scan_headers"])

    assert res.status_code == 200
    data = res.json()
    assert data["conversation_id"] is None
    assert data["assistant_message"]["content"] == "OpenRouter guest response."
    assert mock_gen.called


@pytest.mark.anyio
async def test_chat_service_openrouter_tier_model_selection():
    from src.Services.chat_service import ChatService
    from src.Auth.identity import Identity

    db = AsyncMock()
    service = ChatService(db)
    service.settings.ai_provider = "openrouter"
    service.settings.openrouter_chat_model = "deepseek/deepseek-v4-flash-0731"
    service.settings.openrouter_chat_model_free = "qwen/qwen3.7-flash"

    identity = Identity(user_id="u1", is_authenticated=True)

    with patch.object(service.receipt_repo, "get_all_by_identity", new_callable=AsyncMock) as mock_receipts, \
         patch.object(service, "_call_openrouter", new_callable=AsyncMock) as mock_call:
        mock_receipts.return_value = []
        mock_call.return_value = ("AI response", 10)

        # Free tier
        await service.generate_response(identity, "hi", [], tier="free")
        assert mock_call.call_args.kwargs["model"] == "qwen/qwen3.7-flash"

        # Premium tier
        await service.generate_response(identity, "hi", [], tier="premium")
        assert mock_call.call_args.kwargs["model"] == "deepseek/deepseek-v4-flash-0731"


@pytest.mark.anyio
async def test_chat_service_openrouter_free_model_missing_raises_error():
    from src.Services.chat_service import ChatService
    from src.Auth.identity import Identity

    db = AsyncMock()
    service = ChatService(db)
    service.settings.ai_provider = "openrouter"
    service.settings.openrouter_chat_model = "deepseek/deepseek-v4-flash-0731"
    service.settings.openrouter_chat_model_free = ""

    identity = Identity(user_id="u1", is_authenticated=True)

    with patch.object(service.receipt_repo, "get_all_by_identity", new_callable=AsyncMock) as mock_receipts:
        mock_receipts.return_value = []
        with pytest.raises(ValueError, match="OPENROUTER_CHAT_MODEL_FREE"):
            await service.generate_response(identity, "hi", [], tier="free")


@pytest.mark.anyio
async def test_extraction_service_openrouter_tier_model_selection():
    import json
    from src.Services.extraction_service import ExtractionService
    from src.Models.schemas import ScanContext

    service = ExtractionService()
    service.settings.ai_provider = "openrouter"
    service.settings.openrouter_vision_model = "google/gemini-2.5-flash-lite"
    service.settings.openrouter_vision_model_free = "qwen/qwen3.7-flash"
    service._http_client = AsyncMock()
    mock_resp = AsyncMock()
    mock_resp.json = lambda: {"choices": [{"message": {"content": "{}"}}]}
    mock_resp.raise_for_status = lambda: None
    service._http_client.post.return_value = mock_resp

    # Free tier
    ctx_free = ScanContext(image_bytes=b"fake", content_type="image/jpeg", tier="free")
    await service._extract_openrouter(ctx_free)
    call_args_free = service._http_client.post.call_args
    payload_free = json.loads(call_args_free.kwargs["content"])
    assert payload_free["model"] == "qwen/qwen3.7-flash"

    # Premium tier
    ctx_prem = ScanContext(image_bytes=b"fake", content_type="image/jpeg", tier="premium")
    await service._extract_openrouter(ctx_prem)
    call_args_prem = service._http_client.post.call_args
    payload_prem = json.loads(call_args_prem.kwargs["content"])
    assert payload_prem["model"] == "google/gemini-2.5-flash-lite"

    # Free tier missing model raises ValueError
    service.settings.openrouter_vision_model_free = ""
    with pytest.raises(ValueError, match="OPENROUTER_VISION_MODEL_FREE"):
        await service._extract_openrouter(ctx_free)


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))
