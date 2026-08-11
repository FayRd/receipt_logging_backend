import pytest
from fastapi.testclient import TestClient
import uuid
import sys
import os
import asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import app
from src.Auth.rate_limiter import limiter


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_rate_limiter_each_test():
    """Reset in-memory rate limiter windows between test functions."""
    asyncio.run(limiter.reset())


@pytest.fixture(scope="function")
def mock_device(client):
    device_name = f"MOCK-DEV-{uuid.uuid4().hex[:6]}"
    device_token = f"mock-secret-token-{uuid.uuid4().hex}"

    response = client.post(
        "/api/v1/devices/register",
        json={"device_name": device_name, "device_token": device_token},
    )

    assert response.status_code == 201

    headers = {"X-Device-Name": device_name, "X-Device-Token": device_token}

    device_info = {
        "device_id": device_name,
        "device_name": device_name,
        "device_token": device_token,
        "headers": headers,
    }

    yield device_info

    # TEARDOWN: Delete test device data after test run completes
    try:
        client.delete("/api/v1/devices/me", headers=headers)
    except Exception:
        pass


@pytest.fixture(scope="function")
def mock_user_session(client, mock_device):
    username = f"test_qa_user_{uuid.uuid4().hex[:6]}"
    email = f"{username}@test.example.com"
    password = "secret_password_123"

    response = client.post(
        "/api/v1/user/create",
        json={"username": username, "email": email, "password": password},
    )

    if response.status_code == 409:
        response = client.post(
            "/api/v1/user/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200
        user_id = response.json()["user"]["id"]
    else:
        assert response.status_code == 201
        user_id = response.json()["id"]

    # Link device to user — POST /devices/link requires all 3 headers (X-Device-Name, X-Device-Token, X-User-Name)
    link_headers = dict(mock_device["headers"])
    link_headers["X-User-Name"] = username

    link_res = client.post(
        "/api/v1/devices/link",
        json={
            "device_name": mock_device["device_name"],
            "device_token": mock_device["device_token"],
            "username": username,
        },
        headers=link_headers,
    )
    assert link_res.status_code == 200

    headers = dict(mock_device["headers"])
    headers["X-User-Name"] = username

    session_info = {
        "user_id": user_id,
        "username": username,
        "email": email,
        "headers": headers,
        "device_id": mock_device["device_name"],
        "device_name": mock_device["device_name"],
        "device_token": mock_device["device_token"],
    }

    yield session_info

    # TEARDOWN: Clean up test user account from database after test run completes
    try:
        client.delete("/api/v1/user/me", headers=headers)
    except Exception:
        pass


@pytest.fixture(scope="function")
def invalid_headers():
    return {
        "X-Device-Name": "FAKE-DEV-999",
        "X-Device-Token": "fake-token-that-doesnt-exist",
    }
