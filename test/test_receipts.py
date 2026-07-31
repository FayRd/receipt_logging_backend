from datetime import datetime, timezone

def generate_receipt_payload():
    return {
        "merchant_name": "Test Store",
        "total_amount": 10.50,
        "date": datetime.now(timezone.utc).isoformat(),
        "raw_text": "Test receipt raw text"
    }

def test_get_receipts_guest(client, mock_device):
    response = client.get("/api/v1/receipts/", headers=mock_device["headers"])
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_get_receipts_user(client, mock_user_session):
    response = client.get("/api/v1/receipts/", headers=mock_user_session["headers"])
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_create_receipt(client, mock_user_session):
    payload = {"receipt": generate_receipt_payload()}
    response = client.post("/api/v1/receipts/", json=payload, headers=mock_user_session["headers"])
    assert response.status_code == 201
    assert response.json()["receipt"]["merchant_name"] == "Test Store"

def test_create_receipt_batch(client, mock_user_session):
    payload = {
        "receipts": [
            generate_receipt_payload(),
            generate_receipt_payload()
        ]
    }
    response = client.post("/api/v1/receipts/batch", json=payload, headers=mock_user_session["headers"])
    assert response.status_code == 201
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2

def test_create_receipt_missing_field(client, mock_user_session):
    # Missing total_amount
    invalid_receipt = {
        "merchant_name": "Test Store",
        "date": datetime.now(timezone.utc).isoformat(),
        "raw_text": "text"
    }
    response = client.post("/api/v1/receipts/", json={"receipt": invalid_receipt}, headers=mock_user_session["headers"])
    assert response.status_code == 422

def test_get_single_receipt(client, mock_user_session):
    payload = {"receipt": generate_receipt_payload()}
    create_res = client.post("/api/v1/receipts/", json=payload, headers=mock_user_session["headers"])
    receipt_id = create_res.json()["id"]
    
    response = client.get(f"/api/v1/receipts/{receipt_id}", headers=mock_user_session["headers"])
    assert response.status_code == 200
    assert response.json()["id"] == receipt_id

def test_delete_receipt(client, mock_user_session):
    payload = {"receipt": generate_receipt_payload()}
    create_res = client.post("/api/v1/receipts/", json=payload, headers=mock_user_session["headers"])
    receipt_id = create_res.json()["id"]
    
    # Delete
    del_res = client.delete(f"/api/v1/receipts/{receipt_id}", headers=mock_user_session["headers"])
    assert del_res.status_code == 200
    
    # Delete again
    del_res_2 = client.delete(f"/api/v1/receipts/{receipt_id}", headers=mock_user_session["headers"])
    assert del_res_2.status_code == 404
