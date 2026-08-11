#!/usr/bin/env python3
import pytest

def test_unauthorized_wrong_device_token(client, mock_device):
    headers = {
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": "wrong-token-123"
    }
    response = client.get("/api/v1/devices/me", headers=headers)
    assert response.status_code == 401

def test_unauthorized_unregistered_device_id(client, invalid_headers):
    response = client.get("/api/v1/devices/me", headers=invalid_headers)
    assert response.status_code == 401

def test_invalid_json_structure(client, mock_device):
    # Completely invalid structure for POST /receipts/
    response = client.post("/api/v1/receipts/create", json={"completely_wrong": "structure"}, headers=mock_device["headers"])
    assert response.status_code == 422


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__]))

