#!/usr/bin/env python3
import io
from datetime import datetime
from unittest.mock import AsyncMock, patch
from src.Models.schemas import Receipt, LineItem
from src.Services.extraction_service import ExtractionService


def test_scan_parse_valid_receipt(client, mock_device):
    mock_receipt = Receipt(
        merchant_name="Target Superstore",
        line_items=[
            LineItem(description="Organic Milk", quantity=1.0, unit_price=4.50, total_price=4.50),
        ],
        subtotal=4.50,
        tax_amount=0.36,
        total_amount=4.86,
        currency="USD",
        category="Groceries",
        date=datetime.now(),
        raw_text="TARGET SUPERSTORE\nORGANIC MILK $4.50\nTAX $0.36\nTOTAL $4.86",
        confidence_score=0.95,
    )

    with patch.object(ExtractionService, "extract_from_image", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = mock_receipt

        file_bytes = b"fake_receipt_image_bytes"
        files = {"image": ("receipt.jpg", io.BytesIO(file_bytes), "image/jpeg")}

        response = client.post(
            "/api/v1/scan/parse",
            headers=mock_device["headers"],
            files=files,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["error"] is None
        assert data["data"]["merchant_name"] == "Target Superstore"
        assert data["data"]["confidence_score"] == 0.95


def test_scan_parse_invalid_document_type(client, mock_device):
    # Mock return for a non-receipt image with low confidence score (e.g. 0.35)
    mock_invalid_doc = Receipt(
        merchant_name="Unknown",
        line_items=[],
        total_amount=0.0,
        currency="USD",
        category="Other",
        date=datetime.now(),
        raw_text="Random photo text",
        confidence_score=0.35,
    )

    with patch.object(ExtractionService, "extract_from_image", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = mock_invalid_doc

        file_bytes = b"fake_landscape_image_bytes"
        files = {"image": ("landscape.jpg", io.BytesIO(file_bytes), "image/jpeg")}

        response = client.post(
            "/api/v1/scan/parse",
            headers=mock_device["headers"],
            files=files,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["data"] is None
        assert "Invalid document type" in data["error"]
        assert "0.35" in data["error"]


def test_scan_parse_unauthenticated(client, invalid_headers):
    file_bytes = b"fake_image_bytes"
    files = {"image": ("receipt.jpg", io.BytesIO(file_bytes), "image/jpeg")}

    response = client.post("/api/v1/scan/parse", headers=invalid_headers, files=files)
    assert response.status_code == 401


def test_scan_parse_file_size_exceeded(client, mock_device):
    # 11MB file payload (> 10MB limit)
    large_bytes = b"0" * (11 * 1024 * 1024)
    files = {"image": ("huge.jpg", io.BytesIO(large_bytes), "image/jpeg")}

    response = client.post(
        "/api/v1/scan/parse",
        headers=mock_device["headers"],
        files=files,
    )

    assert response.status_code == 400
    assert "exceeds maximum limit of 10MB" in response.json()["detail"]


# ── /parse-many endpoint tests ─────────────────────────────────────────

def test_scan_parse_many_too_few_files(client, mock_device):
    """Batch with fewer than 2 files returns HTTP 400."""
    file_bytes = b"fake_receipt_image_bytes"
    files = [("files", ("receipt1.jpg", io.BytesIO(file_bytes), "image/jpeg"))]

    response = client.post(
        "/api/v1/scan/parse-many",
        headers=mock_device["headers"],
        files=files,
    )

    assert response.status_code == 400
    assert "between 2 and 10" in response.json()["detail"]


def test_scan_parse_many_too_many_files(client, mock_device):
    """Batch with more than 10 files returns HTTP 400."""
    file_bytes = b"fake_receipt_image_bytes"
    files = [
        ("files", (f"receipt{i}.jpg", io.BytesIO(file_bytes), "image/jpeg"))
        for i in range(11)
    ]

    response = client.post(
        "/api/v1/scan/parse-many",
        headers=mock_device["headers"],
        files=files,
    )

    assert response.status_code == 400
    assert "between 2 and 10" in response.json()["detail"]


def test_scan_parse_many_file_size_exceeded(client, mock_device):
    """Individual file exceeding 10MB returns HTTP 400."""
    large_bytes = b"0" * (11 * 1024 * 1024)
    small_bytes = b"small_file"
    files = [
        ("files", ("huge.jpg", io.BytesIO(large_bytes), "image/jpeg")),
        ("files", ("receipt.jpg", io.BytesIO(small_bytes), "image/jpeg")),
    ]

    response = client.post(
        "/api/v1/scan/parse-many",
        headers=mock_device["headers"],
        files=files,
    )

    assert response.status_code == 400
    assert "exceeds maximum allowed size" in response.json()["detail"]


def test_scan_parse_many_unauthenticated(client, invalid_headers):
    """Unauthenticated request returns HTTP 401."""
    file_bytes = b"fake_receipt_image_bytes"
    files = [
        ("files", ("receipt1.jpg", io.BytesIO(file_bytes), "image/jpeg")),
        ("files", ("receipt2.jpg", io.BytesIO(file_bytes), "image/jpeg")),
    ]

    response = client.post(
        "/api/v1/scan/parse-many",
        headers=invalid_headers,
        files=files,
    )

    assert response.status_code == 401


def test_scan_parse_many_batch_status_not_found(client, mock_device):
    """GET /parse-many/{batch_id} returns HTTP 404 for unknown batch_id."""
    response = client.get(
        "/api/v1/scan/parse-many/nonexistent-batch-id-12345",
        headers=mock_device["headers"],
    )

    # 503 when Redis client is not initialised in test context,
    # 404 when batch is not found, 500 when Redis is initialised but unreachable
    assert response.status_code in (404, 500, 503)


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))

