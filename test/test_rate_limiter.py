#!/usr/bin/env python3
import pytest
from src.Auth.rate_limiter import limiter



def test_rate_limit_exceeded_auth(client):
    headers = {"X-Device-Name": "TEST-RATE-LIMIT-DEV-001"}
    payload = {"username": "nonexistent_user", "password": "wrong_password"}

    # Execute 10 requests (auth limit is 10/min)
    for _ in range(10):
        res = client.post("/api/v1/user/login", json=payload, headers=headers)
        assert res.status_code == 401

    # 11th request triggers 429 Too Many Requests
    res_exceeded = client.post("/api/v1/user/login", json=payload, headers=headers)
    assert res_exceeded.status_code == 429
    data = res_exceeded.json()
    assert "Rate limit exceeded" in data["detail"]
    assert "Retry-After" in res_exceeded.headers
    assert res_exceeded.headers["X-RateLimit-Limit"] == "10"
    assert res_exceeded.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limit_exceeded_scan_endpoint(client, mock_device):
    # Scan endpoint limit is 5 requests per minute
    file_bytes = b"fake_scan_image"
    files = {"image": ("receipt.jpg", file_bytes, "image/jpeg")}

    # Send 5 requests (mock extraction errors out or returns invalid, but passes rate limiter)
    for _ in range(5):
        client.post("/api/v1/scan/parse", headers=mock_device["headers"], files={"image": ("r.jpg", b"fake", "image/jpeg")})

    # 6th request triggers 429
    res_exceeded = client.post(
        "/api/v1/scan/parse",
        headers=mock_device["headers"],
        files={"image": ("r.jpg", b"fake", "image/jpeg")},
    )
    assert res_exceeded.status_code == 429
    assert res_exceeded.headers["X-RateLimit-Limit"] == "5"


def test_rate_limit_distinct_keys(client):
    # Different device IDs maintain isolated rate limit windows
    dev1_headers = {"X-Device-Name": "DEV-AAA-111", "X-Device-Token": "token-aaa-111"}
    dev2_headers = {"X-Device-Name": "DEV-BBB-222", "X-Device-Token": "token-bbb-222"}
    payload = {"username": "user1", "password": "pass1"}

    # Dev 1 consumes all 10 limit slots
    for _ in range(10):
        client.post("/api/v1/user/login", json=payload, headers=dev1_headers)

    # Dev 1 is throttled
    res1 = client.post("/api/v1/user/login", json=payload, headers=dev1_headers)
    assert res1.status_code == 429

    # Dev 2 is still permitted
    res2 = client.post("/api/v1/user/login", json=payload, headers=dev2_headers)
    assert res2.status_code == 401


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__]))

