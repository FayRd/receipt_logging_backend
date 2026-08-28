#!/usr/bin/env python3
import io
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from src.Services.quota_service import reset_quota_store_for_testing, get_quota_service
from src.Models.schemas import Receipt


@pytest.fixture(autouse=True)
def clean_quota():
    reset_quota_store_for_testing()
    yield
    reset_quota_store_for_testing()


def _unique_user(tier: str = "free") -> dict:
    raw_id = uuid.uuid4().hex[:6]
    name = f"tier_{raw_id[:5]}"
    return {
        "username": name,
        "email": f"{name}@test.example.com",
        "password": "Password123!",
        "country_code": "+60",
        "mobile_number": f"12{raw_id[:7]}",
    }


def test_get_quota_guest(client, mock_device):
    """Guest session defaults to Free tier with 10 scans and 10k tokens."""
    res = client.get("/api/v1/user/quota", headers=mock_device["guest_scan_headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "free"
    assert data["scan"]["limit"] == 10
    assert data["scan"]["used"] == 0
    assert data["scan"]["remaining"] == 10
    assert data["scan"]["is_exhausted"] is False
    assert data["chat"]["limit"] == 10000
    assert data["chat"]["used"] == 0
    assert data["chat"]["remaining"] == 10000
    assert data["chat"]["is_exhausted"] is False
    assert data["seconds_to_reset"] > 0
    assert "h " in data["reset_countdown"]


def test_get_quota_free_user(client):
    """Authenticated free user receives 10 scans / 10k chat tokens limit."""
    user = _unique_user("free")
    create_res = client.post("/api/v1/user/create", json=user)
    assert create_res.status_code == 201

    headers = {
        "X-Request-Type": "user",
        "X-User-Name": user["username"],
        "X-User-Token": user["password"],
    }
    res = client.get("/api/v1/user/quota", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["tier"] == "free"
    assert data["scan"]["limit"] == 10
    assert data["chat"]["limit"] == 10000


def test_get_quota_premium_and_dev_tiers(client, monkeypatch):
    """Premium tier gets 50 scans / 50k tokens; Dev tier gets -1 (unlimited)."""
    user_prem = _unique_user("premium")
    client.post("/api/v1/user/create", json=user_prem)

    from src.Models.Users.user_repository import UserRepository
    orig_get = UserRepository.get_by_id

    async def mock_get_premium(self, uid):
        u = await orig_get(self, uid)
        if u:
            u = dict(u)
            u["tier"] = "premium"
        return u

    monkeypatch.setattr(UserRepository, "get_by_id", mock_get_premium)

    headers_prem = {
        "X-Request-Type": "user",
        "X-User-Name": user_prem["username"],
        "X-User-Token": user_prem["password"],
    }
    res_prem = client.get("/api/v1/user/quota", headers=headers_prem)
    assert res_prem.status_code == 200
    data_prem = res_prem.json()
    assert data_prem["tier"] == "premium"
    assert data_prem["scan"]["limit"] == 50
    assert data_prem["chat"]["limit"] == 50000

    # Dev tier test
    async def mock_get_dev(self, uid):
        u = await orig_get(self, uid)
        if u:
            u = dict(u)
            u["tier"] = "dev"
        return u

    monkeypatch.setattr(UserRepository, "get_by_id", mock_get_dev)
    res_dev = client.get("/api/v1/user/quota", headers=headers_prem)
    assert res_dev.status_code == 200
    data_dev = res_dev.json()
    assert data_dev["tier"] == "dev"
    assert data_dev["scan"]["limit"] == -1
    assert data_dev["scan"]["remaining"] == -1
    assert data_dev["scan"]["is_exhausted"] is False
    assert data_dev["chat"]["limit"] == -1


from src.Auth.rate_limiter import limiter
import asyncio


@patch("src.Services.extraction_service.ExtractionService.extract_from_image", new_callable=AsyncMock)
def test_scan_quota_exceeded_rejection(mock_extract, client, mock_device):
    """Exceeding 10 scans on free tier returns HTTP 429 with reset countdown."""
    mock_receipt = Receipt(
        merchant_name="Store",
        date="2026-08-28",
        total_amount=12.50,
        confidence_score=0.95,
    )
    mock_extract.return_value = mock_receipt

    # Use up 10 scans
    for i in range(10):
        asyncio.run(limiter.reset())
        img_bytes = b"fake_image_bytes_" + str(i).encode()
        files = {"image": ("receipt.jpg", io.BytesIO(img_bytes), "image/jpeg")}
        res = client.post("/api/v1/scan/parse", files=files, headers=mock_device["guest_scan_headers"])
        assert res.status_code == 200

    # 11th scan should fail with 429
    asyncio.run(limiter.reset())
    img_bytes = b"fake_image_bytes_overflow"
    files = {"image": ("receipt.jpg", io.BytesIO(img_bytes), "image/jpeg")}
    res_overflow = client.post("/api/v1/scan/parse", files=files, headers=mock_device["guest_scan_headers"])
    assert res_overflow.status_code == 429
    assert "Daily scan quota reached (10/10)" in res_overflow.json()["detail"]
    assert "00:00 UTC" in res_overflow.json()["detail"]
    assert "Retry-After" in res_overflow.headers


@patch("src.Services.extraction_service.ExtractionService.extract_from_image", new_callable=AsyncMock)
def test_bulk_scan_upfront_quota_check(mock_extract, client, mock_device):
    """Submitting 5 files in parse-many when only 2 remaining returns 429 upfront."""
    mock_receipt = Receipt(
        merchant_name="Store",
        date="2026-08-28",
        total_amount=12.50,
        confidence_score=0.95,
    )
    mock_extract.return_value = mock_receipt

    # Consume 8 scans via single parse endpoint so 2 remain
    for i in range(8):
        asyncio.run(limiter.reset())
        img_bytes = b"pre_scan_" + str(i).encode()
        files = {"image": ("receipt.jpg", io.BytesIO(img_bytes), "image/jpeg")}
        res = client.post("/api/v1/scan/parse", files=files, headers=mock_device["guest_scan_headers"])
        assert res.status_code == 200

    # Attempt to upload 5 files
    asyncio.run(limiter.reset())
    files = [
        ("files", (f"r{i}.jpg", io.BytesIO(b"dummy_bytes"), "image/jpeg"))
        for i in range(5)
    ]
    res = client.post("/api/v1/scan/parse-many", files=files, headers=mock_device["guest_scan_headers"])
    assert res.status_code == 429
    assert "Daily scan quota reached" in res.json()["detail"]


@patch("src.Services.chat_service.ChatService.generate_response_local", new_callable=AsyncMock)
def test_chat_token_quota_enforcement(mock_chat, client, mock_device):
    """Chat queries consume tokens and reject with 429 once 10k token limit is reached."""
    mock_chat.return_value = ("Here is your spending analysis.", 4000)

    # First turn: 4000 tokens
    asyncio.run(limiter.reset())
    res1 = client.post(
        "/api/v1/chat/query",
        json={"message": "Analyze expenses"},
        headers=mock_device["guest_scan_headers"],
    )
    assert res1.status_code == 200

    # Second turn: 4000 tokens (8000 used)
    asyncio.run(limiter.reset())
    res2 = client.post(
        "/api/v1/chat/query",
        json={"message": "Analyze more"},
        headers=mock_device["guest_scan_headers"],
    )
    assert res2.status_code == 200

    # Third turn: 4000 tokens (12000 used -> exceeds limit)
    asyncio.run(limiter.reset())
    res3 = client.post(
        "/api/v1/chat/query",
        json={"message": "Analyze again"},
        headers=mock_device["guest_scan_headers"],
    )
    assert res3.status_code == 200

    # Fourth turn should now be blocked with 429
    asyncio.run(limiter.reset())
    res4 = client.post(
        "/api/v1/chat/query",
        json={"message": "Analyze once more"},
        headers=mock_device["guest_scan_headers"],
    )
    assert res4.status_code == 429
    assert "Daily chat token quota reached" in res4.json()["detail"]
    assert "00:00 UTC" in res4.json()["detail"]


@patch("src.Services.extraction_service.ExtractionService.extract_from_image", new_callable=AsyncMock)
@patch("src.Services.chat_service.ChatService.generate_response_local", new_callable=AsyncMock)
def test_scan_and_chat_quotas_are_independent(mock_chat, mock_extract, client, mock_device):
    """Exhausting scan quota does NOT block chat queries."""
    mock_receipt = Receipt(
        merchant_name="Store",
        date="2026-08-28",
        total_amount=12.50,
        confidence_score=0.95,
    )
    mock_extract.return_value = mock_receipt

    # Exhaust all 10 scans via single parse endpoint
    for i in range(10):
        asyncio.run(limiter.reset())
        img_bytes = b"scan_exhaust_" + str(i).encode()
        files = {"image": ("receipt.jpg", io.BytesIO(img_bytes), "image/jpeg")}
        res = client.post("/api/v1/scan/parse", files=files, headers=mock_device["guest_scan_headers"])
        assert res.status_code == 200

    # Verify scan quota is exhausted
    asyncio.run(limiter.reset())
    q_res = client.get("/api/v1/user/quota", headers=mock_device["guest_scan_headers"])
    assert q_res.json()["scan"]["is_exhausted"] is True
    assert q_res.json()["chat"]["is_exhausted"] is False

    # Chat query still succeeds!
    mock_chat.return_value = ("Chat works fine!", 150)
    asyncio.run(limiter.reset())
    res_chat = client.post(
        "/api/v1/chat/query",
        json={"message": "Hello AI"},
        headers=mock_device["guest_scan_headers"],
    )
    assert res_chat.status_code == 200
    assert res_chat.json()["assistant_message"]["content"] == "Chat works fine!"


@patch("src.Services.extraction_service.ExtractionService.extract_from_image", new_callable=AsyncMock)
def test_guest_and_user_quota_isolation(mock_extract, client, mock_device):
    """Exhausting guest mode quota does NOT affect the user account's quota."""
    mock_receipt = Receipt(
        merchant_name="Store",
        date="2026-08-28",
        total_amount=12.50,
        confidence_score=0.95,
    )
    mock_extract.return_value = mock_receipt

    # 1. Create a user
    user = _unique_user("free")
    create_res = client.post("/api/v1/user/create", json=user)
    assert create_res.status_code == 201

    # 2. Exhaust guest quota (10 scans)
    for i in range(10):
        asyncio.run(limiter.reset())
        img_bytes = b"guest_scan_" + str(i).encode()
        files = {"image": ("receipt.jpg", io.BytesIO(img_bytes), "image/jpeg")}
        res = client.post("/api/v1/scan/parse", files=files, headers=mock_device["guest_scan_headers"])
        assert res.status_code == 200

    # 3. Guest quota is exhausted
    asyncio.run(limiter.reset())
    guest_q = client.get("/api/v1/user/quota", headers=mock_device["guest_scan_headers"])
    assert guest_q.status_code == 200
    assert guest_q.json()["scan"]["used"] == 10
    assert guest_q.json()["scan"]["is_exhausted"] is True

    # 4. User logs in and checks quota: should be completely fresh (0 used)
    user_headers = {
        "X-Request-Type": "user",
        "X-User-Name": user["username"],
        "X-User-Token": user["password"],
    }
    asyncio.run(limiter.reset())
    user_q = client.get("/api/v1/user/quota", headers=user_headers)
    assert user_q.status_code == 200
    assert user_q.json()["scan"]["used"] == 0
    assert user_q.json()["scan"]["remaining"] == 10
    assert user_q.json()["scan"]["is_exhausted"] is False

    # 5. User can scan successfully
    asyncio.run(limiter.reset())
    img_bytes = b"user_scan_1"
    files = {"image": ("receipt.jpg", io.BytesIO(img_bytes), "image/jpeg")}
    res_user_scan = client.post("/api/v1/scan/parse", files=files, headers=user_headers)
    assert res_user_scan.status_code == 200

