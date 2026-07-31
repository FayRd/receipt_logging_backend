import pytest

def test_unauthorized_wrong_device_token(client, mock_device):
    headers = {
        "X-Device-ID": mock_device["device_id"],
        "X-Device-Token": "wrong-token-123"
    }
    response = client.get("/api/v1/receipts/", headers=headers)
    assert response.status_code == 401

def test_unauthorized_unregistered_device_id(client, invalid_headers):
    response = client.get("/api/v1/receipts/", headers=invalid_headers)
    assert response.status_code == 401

def test_invalid_json_structure(client, mock_device):
    # Completely invalid structure for POST /receipts/
    response = client.post("/api/v1/receipts/create", json={"completely_wrong": "structure"}, headers=mock_device["headers"])
    assert response.status_code == 422
