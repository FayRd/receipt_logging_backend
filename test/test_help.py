#!/usr/bin/env python3
import pytest


def test_feedback_submit_success(client):
    payload = {
        "sender": "qa_tester_001",
        "description": "The receipt scanning speed is fantastic, but it would be great to have dark mode auto-scheduling.",
        "app_version": "1.0.0",
        "device_id": "dev_test_device_001",
        "platform": "Android",
    }
    response = client.post("/api/v1/help/feedback", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "feedback" in data["message"].lower()


def test_feedback_submit_too_short_rejected(client):
    payload = {
        "sender": "qa_tester_001",
        "description": "Too short!",  # < 25 chars
    }
    response = client.post("/api/v1/help/feedback", json=payload)
    assert response.status_code == 422


def test_feedback_submit_missing_sender_rejected(client):
    payload = {
        "description": "Valid description length that definitely exceeds twenty five characters.",
    }
    response = client.post("/api/v1/help/feedback", json=payload)
    assert response.status_code == 422


def test_feedback_submit_rate_limiting(client):
    payload = {
        "sender": "rate_limit_user",
        "description": "First feedback submission that is long enough to meet the character requirement.",
        "device_id": "dev_rate_limit_001",
    }
    headers = {"X-Device-Name": "dev_rate_limit_001"}

    # 1. First submission succeeds
    res1 = client.post("/api/v1/help/feedback", json=payload, headers=headers)
    assert res1.status_code == 200

    # 2. Second submission immediately afterwards gets 429
    res2 = client.post("/api/v1/help/feedback", json=payload, headers=headers)
    assert res2.status_code == 429
    assert "Retry-After" in res2.headers or "rate limit" in res2.json()["detail"].lower()
