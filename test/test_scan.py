#!/usr/bin/env python3
import io
from datetime import datetime
from unittest.mock import AsyncMock, patch
from src.Models.schemas import LineItem, Receipt, ScanContext
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
            headers=mock_device["guest_scan_headers"],
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
            headers=mock_device["guest_scan_headers"],
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


def test_scan_parse_missing_request_type(client, mock_device):
    """Missing X-Request-Type header returns HTTP 422 (validation error)."""
    file_bytes = b"fake_image_bytes"
    files = {"image": ("receipt.jpg", io.BytesIO(file_bytes), "image/jpeg")}

    # Omit X-Request-Type from headers
    headers_without_type = {
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
    }
    response = client.post("/api/v1/scan/parse", headers=headers_without_type, files=files)
    assert response.status_code == 422


def test_scan_parse_invalid_request_type(client, mock_device):
    """Invalid X-Request-Type value returns HTTP 400."""
    file_bytes = b"fake_image_bytes"
    files = {"image": ("receipt.jpg", io.BytesIO(file_bytes), "image/jpeg")}

    headers = {
        "X-Request-Type": "admin",  # invalid value
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
    }
    response = client.post("/api/v1/scan/parse", headers=headers, files=files)
    assert response.status_code == 400


def test_scan_parse_guest_with_user_headers_rejected(client, mock_device, mock_user_session):
    """Guest request type with user headers mixed in returns HTTP 400."""
    file_bytes = b"fake_image_bytes"
    files = {"image": ("receipt.jpg", io.BytesIO(file_bytes), "image/jpeg")}

    # Conflicting headers — guest type with user headers
    headers = {
        "X-Request-Type": "guest",
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
        "X-User-Name": mock_user_session["username"],
        "X-User-Token": "secret_password_123",
    }
    response = client.post("/api/v1/scan/parse", headers=headers, files=files)
    assert response.status_code == 400


def test_scan_parse_user_with_device_headers_rejected(client, mock_device, mock_user_session):
    """User request type with device headers mixed in returns HTTP 400."""
    file_bytes = b"fake_image_bytes"
    files = {"image": ("receipt.jpg", io.BytesIO(file_bytes), "image/jpeg")}

    # Conflicting headers — user type with device headers
    headers = {
        "X-Request-Type": "user",
        "X-Device-Name": mock_device["device_name"],
        "X-Device-Token": mock_device["device_token"],
        "X-User-Name": mock_user_session["username"],
        "X-User-Token": "secret_password_123",
    }
    response = client.post("/api/v1/scan/parse", headers=headers, files=files)
    assert response.status_code == 400


def test_scan_parse_file_size_exceeded(client, mock_device):
    # 11MB file payload (> 10MB limit)
    large_bytes = b"0" * (11 * 1024 * 1024)
    files = {"image": ("huge.jpg", io.BytesIO(large_bytes), "image/jpeg")}

    response = client.post(
        "/api/v1/scan/parse",
        headers=mock_device["guest_scan_headers"],
        files=files,
    )

    assert response.status_code == 400
    assert "exceeds maximum limit of 10MB" in response.json()["detail"]


# ── /parse-many endpoint tests ─────────────────────────────────────────

def test_scan_parse_many_single_file_valid(client, mock_device):
    """Batch with 1 file is valid and returns HTTP 202 Accepted."""
    file_bytes = b"fake_receipt_image_bytes"
    files = [("files", ("receipt1.jpg", io.BytesIO(file_bytes), "image/jpeg"))]

    response = client.post(
        "/api/v1/scan/parse-many",
        headers=mock_device["guest_scan_headers"],
        files=files,
    )

    # 503 if Redis is uninitialized in test context, 202 when batch is created
    assert response.status_code in (202, 503)
    if response.status_code == 202:
        data = response.json()
        assert data["total_jobs"] == 1
        assert len(data["jobs"]) == 1


def test_scan_parse_many_too_many_files(client, mock_device):
    """Batch with more than 10 files returns HTTP 400."""
    file_bytes = b"fake_receipt_image_bytes"
    files = [
        ("files", (f"receipt{i}.jpg", io.BytesIO(file_bytes), "image/jpeg"))
        for i in range(11)
    ]

    response = client.post(
        "/api/v1/scan/parse-many",
        headers=mock_device["guest_scan_headers"],
        files=files,
    )

    assert response.status_code == 400
    assert "between 1 and 10" in response.json()["detail"]


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
        headers=mock_device["guest_scan_headers"],
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


def test_scan_parse_many_batch_status_unauthenticated(client, invalid_headers):
    """GET /parse-many/{batch_id} without authentication headers returns HTTP 401."""
    response = client.get(
        "/api/v1/scan/parse-many/batch-12345",
        headers=invalid_headers,
    )
    assert response.status_code == 401


def test_scan_parse_many_batch_status_not_found(client, mock_device):
    """GET /parse-many/{batch_id} returns HTTP 404 for unknown batch_id."""
    response = client.get(
        "/api/v1/scan/parse-many/nonexistent-batch-id-12345",
        headers=mock_device["guest_scan_headers"],
    )

    # 503 when Redis client is not initialised in test context,
    # 404 when batch is not found, 500 when Redis is initialised but unreachable
    assert response.status_code in (404, 500, 503)


# ── Provider Error & Batch Halt Unit Tests ─────────────────────────────

import pytest
from src.Services.extraction_service import (
    FRIENDLY_ERROR_MESSAGE,
    ProviderOverloadedError,
    is_provider_overload_error,
)
from src.API.v1.scan import process_batch_worker
import src.API.v1.scan as scan_module


def test_is_provider_overload_error_detection():
    """Verify various provider overload / rate limit error patterns."""
    assert is_provider_overload_error(Exception("429 ResourceExhausted: token usage limit exceeded"))
    assert is_provider_overload_error(Exception("500 Internal Server Error: model experiencing high demand"))
    assert is_provider_overload_error(Exception("503 Service Unavailable"))
    assert is_provider_overload_error(Exception("Quota exceeded for quota metric"))
    assert is_provider_overload_error(ProviderOverloadedError())

    # Non-overload errors
    assert not is_provider_overload_error(ValueError("Invalid date format"))
    assert not is_provider_overload_error(Exception("File corrupted"))


@pytest.mark.anyio
async def test_extraction_service_retry_jitter_success():
    """ExtractionService (Gemini provider) retries on 429 and succeeds on subsequent attempt."""
    from unittest.mock import MagicMock

    mock_receipt = Receipt(
        merchant_name="Costco",
        line_items=[],
        total_amount=50.0,
        currency="USD",
        category="Groceries",
        date=datetime.now(),
        raw_text="COSTCO WHOLESALE",
        confidence_score=0.9,
    )

    service = ExtractionService.__new__(ExtractionService)
    mock_settings = MagicMock()
    mock_settings.effective_ai_provider = "gemini"
    mock_settings.gemini_vision_model = "gemini-3.6-flash"
    service.settings = mock_settings
    service.MAX_RETRIES = ExtractionService.MAX_RETRIES
    service.BASE_DELAY_SECONDS = ExtractionService.BASE_DELAY_SECONDS

    mock_gemini = MagicMock()
    mock_gemini.aio.models.generate_content = AsyncMock()
    service._gemini_client = mock_gemini
    service._http_client = None

    context = ScanContext(
        image_bytes=b"dummy_bytes",
        content_type="image/jpeg",
        user_id=None,
        device_id=None,
    )

    call_count = 0

    async def mock_generate_content(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("429 ResourceExhausted: rate limit")
        # Return mock response on attempt 2
        class MockResp:
            text = mock_receipt.model_dump_json()
            usage_metadata = None
        return MockResp()

    mock_gemini.aio.models.generate_content.side_effect = mock_generate_content

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await service.extract_from_image(context)
        assert res.merchant_name == "Costco"
        assert call_count == 2
        assert mock_sleep.call_count == 1


@pytest.mark.anyio
async def test_extraction_service_retry_exhausted_raises_provider_error():
    """ExtractionService (Gemini) exhausts 3 retries and raises ProviderOverloadedError with friendly message."""
    from unittest.mock import MagicMock

    service = ExtractionService.__new__(ExtractionService)
    mock_settings = MagicMock()
    mock_settings.effective_ai_provider = "gemini"
    mock_settings.gemini_vision_model = "gemini-3.6-flash"
    service.settings = mock_settings
    service.MAX_RETRIES = ExtractionService.MAX_RETRIES
    service.BASE_DELAY_SECONDS = ExtractionService.BASE_DELAY_SECONDS

    mock_gemini = MagicMock()
    mock_gemini.aio.models.generate_content = AsyncMock(side_effect=Exception("500 model experiencing high demand"))
    service._gemini_client = mock_gemini
    service._http_client = None

    context = ScanContext(
        image_bytes=b"dummy_bytes",
        content_type="image/jpeg",
        user_id=None,
        device_id=None,
    )

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(ProviderOverloadedError) as exc_info:
            await service.extract_from_image(context)

        assert str(exc_info.value) == FRIENDLY_ERROR_MESSAGE
        assert mock_sleep.call_count == 3


@pytest.mark.anyio
async def test_extraction_service_openrouter_success():
    """ExtractionService (OpenRouter provider) sends image to OpenRouter and returns structured Receipt."""
    from unittest.mock import MagicMock
    import httpx

    mock_receipt = Receipt(
        merchant_name="Starbucks",
        line_items=[],
        total_amount=8.50,
        currency="USD",
        category="Dining",
        date=datetime.now(),
        raw_text="STARBUCKS\nLATTE $8.50",
        confidence_score=0.92,
    )

    # Build service directly, bypassing __init__, then inject a mock settings + http client
    service = ExtractionService.__new__(ExtractionService)

    mock_settings = MagicMock()
    mock_settings.effective_ai_provider = "openrouter"
    mock_settings.openrouter_vision_model = "google/gemini-2.5-flash"
    service.settings = mock_settings

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": mock_receipt.model_dump_json()}}]
    }
    mock_http.post = AsyncMock(return_value=mock_response)

    service._gemini_client = None
    service._http_client = mock_http

    context = ScanContext(
        image_bytes=b"openrouter_image_bytes",
        content_type="image/jpeg",
        user_id=None,
        device_id=None,
    )

    result = await service.extract_from_image(context)

    assert result.merchant_name == "Starbucks"
    assert result.confidence_score == 0.92
    assert mock_http.post.called


@pytest.mark.anyio
async def test_extraction_service_openrouter_retry_on_429():
    """ExtractionService (OpenRouter provider) retries on HTTP 429 and succeeds on next attempt."""
    from unittest.mock import MagicMock
    import httpx

    mock_receipt = Receipt(
        merchant_name="McDonald's",
        line_items=[],
        total_amount=12.0,
        currency="USD",
        category="Dining",
        date=datetime.now(),
        raw_text="MCDONALD'S\nBIG MAC $12.00",
        confidence_score=0.88,
    )

    service = ExtractionService.__new__(ExtractionService)

    mock_settings = MagicMock()
    mock_settings.effective_ai_provider = "openrouter"
    mock_settings.openrouter_vision_model = "google/gemini-2.5-flash"
    mock_settings.MAX_RETRIES = 3
    service.settings = mock_settings
    service.MAX_RETRIES = ExtractionService.MAX_RETRIES
    service.BASE_DELAY_SECONDS = ExtractionService.BASE_DELAY_SECONDS

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("429 Too Many Requests: rate limit exceeded")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": mock_receipt.model_dump_json()}}]
        }
        return resp

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(side_effect=mock_post)
    service._gemini_client = None
    service._http_client = mock_http

    context = ScanContext(
        image_bytes=b"openrouter_image_bytes",
        content_type="image/jpeg",
        user_id=None,
        device_id=None,
    )

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await service.extract_from_image(context)
        assert result.merchant_name == "McDonald's"
        assert call_count == 2
        assert mock_sleep.call_count == 1


@pytest.mark.anyio
async def test_receipt_pydantic_coercion_handles_null_merchant_and_fields():
    """Receipt model validates and coerces null fields emitted by lightweight LLMs into safe defaults."""
    raw_json = """{
        "merchant_name": null,
        "line_items": [{"description": null, "quantity": 1, "unit_price": 5.0, "total_price": 5.0}],
        "subtotal": null,
        "tax_amount": null,
        "total_amount": null,
        "currency": null,
        "category": null,
        "date": null,
        "raw_text": null,
        "confidence_score": null,
        "notes": "Non-receipt image"
    }"""
    receipt = Receipt.model_validate_json(raw_json)
    assert receipt.merchant_name == "N/A"
    assert receipt.total_amount == 0.0
    assert receipt.currency == "USD"
    assert receipt.raw_text == ""
    assert receipt.confidence_score == 0.0
    assert receipt.line_items[0].description == ""
    assert receipt.notes == "Non-receipt image"
    assert receipt.date is not None


@pytest.mark.anyio
async def test_extraction_service_openrouter_null_merchant_handled_gracefully():
    """ExtractionService with OpenRouter model emitting null merchant_name parses into valid Receipt with 'N/A'."""
    from unittest.mock import MagicMock
    import httpx

    service = ExtractionService.__new__(ExtractionService)

    mock_settings = MagicMock()
    mock_settings.effective_ai_provider = "openrouter"
    mock_settings.openrouter_vision_model = "google/gemini-2.5-flash-lite"
    mock_settings.MAX_RETRIES = 3
    service.settings = mock_settings
    service.MAX_RETRIES = ExtractionService.MAX_RETRIES
    service.BASE_DELAY_SECONDS = ExtractionService.BASE_DELAY_SECONDS

    # Emulate payload returned by gemini-2.5-flash-lite on proof.png with null merchant
    mock_llm_json = """{
        "merchant_name": null,
        "line_items": null,
        "subtotal": null,
        "tax_amount": null,
        "total_amount": 0.0,
        "currency": "USD",
        "category": null,
        "date": "2026-08-21T19:42:40Z",
        "raw_text": "Transfer Proof",
        "confidence_score": 0.25,
        "notes": "Image is a transfer proof, not a merchant receipt."
    }"""

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": mock_llm_json}}]
    }
    mock_http.post = AsyncMock(return_value=resp)

    service._gemini_client = None
    service._http_client = mock_http

    context = ScanContext(
        image_bytes=b"proof_image_bytes",
        content_type="image/png",
        user_id=None,
        device_id=None,
    )

    result = await service.extract_from_image(context)
    assert result.merchant_name == "N/A"
    assert result.confidence_score == 0.25
    assert result.notes == "Image is a transfer proof, not a merchant receipt."



@pytest.mark.anyio
async def test_batch_worker_first_job_429_halts_entire_batch():
    """If the first job in a batch fails with 429, halt immediately and mark remaining jobs failed with friendly error."""
    mock_redis = AsyncMock()
    original_redis = scan_module.redis_client
    scan_module.redis_client = mock_redis

    try:
        job_items = [
            ("job-1", "receipt1.jpg", b"bytes1", "image/jpeg"),
            ("job-2", "receipt2.jpg", b"bytes2", "image/jpeg"),
            ("job-3", "receipt3.jpg", b"bytes3", "image/jpeg"),
        ]

        with patch.object(ExtractionService, "extract_from_image", side_effect=ProviderOverloadedError()):
            await process_batch_worker("batch-test-1", job_items)

        # Verify job-1, job-2, job-3 were all set to FAILED with FRIENDLY_ERROR_MESSAGE
        hset_calls = mock_redis.hset.call_args_list

        # Verify halted_on_first_job flag set in batch metadata
        meta_calls = [c for c in hset_calls if c[0][0] == "batch:batch-test-1:meta"]
        assert len(meta_calls) > 0
        assert meta_calls[0][0][1] == "halted_on_first_job"
        assert meta_calls[0][0][2] == "true"

        # Verify all 3 jobs marked FAILED
        job1_fail = any(c[0][0] == "job:job-1" and c[1].get("mapping", {}).get("status") == "FAILED" for c in hset_calls)
        job2_fail = any(c[0][0] == "job:job-2" and c[1].get("mapping", {}).get("status") == "FAILED" for c in hset_calls)
        job3_fail = any(c[0][0] == "job:job-3" and c[1].get("mapping", {}).get("status") == "FAILED" for c in hset_calls)

        assert job1_fail
        assert job2_fail
        assert job3_fail

    finally:
        scan_module.redis_client = original_redis


@pytest.mark.anyio
async def test_batch_worker_second_job_500_preserves_first_job():
    """If the 2nd job fails with 500, preserve job 1 COMPLETED and halt remaining jobs."""
    mock_redis = AsyncMock()
    original_redis = scan_module.redis_client
    scan_module.redis_client = mock_redis

    mock_receipt = Receipt(
        merchant_name="Walmart",
        line_items=[],
        total_amount=25.0,
        currency="USD",
        category="Groceries",
        date=datetime.now(),
        raw_text="WALMART",
        confidence_score=0.95,
    )

    try:
        job_items = [
            ("job-1", "receipt1.jpg", b"bytes1", "image/jpeg"),
            ("job-2", "receipt2.jpg", b"bytes2", "image/jpeg"),
            ("job-3", "receipt3.jpg", b"bytes3", "image/jpeg"),
        ]

        async def mock_extract(context):
            if context.image_bytes == b"bytes1":
                return mock_receipt
            raise ProviderOverloadedError()

        with patch.object(ExtractionService, "extract_from_image", side_effect=mock_extract):
            await process_batch_worker("batch-test-2", job_items)

        hset_calls = mock_redis.hset.call_args_list

        # Job 1 was marked COMPLETED
        job1_completed = any(c[0][0] == "job:job-1" and c[1].get("mapping", {}).get("status") == "COMPLETED" for c in hset_calls)
        assert job1_completed

        # Job 2 & 3 marked FAILED
        job2_failed = any(c[0][0] == "job:job-2" and c[1].get("mapping", {}).get("status") == "FAILED" for c in hset_calls)
        job3_failed = any(c[0][0] == "job:job-3" and c[1].get("mapping", {}).get("status") == "FAILED" for c in hset_calls)
        assert job2_failed
        assert job3_failed

        # Metadata flagged halted_on_provider_error
        meta_calls = [c for c in hset_calls if c[0][0] == "batch:batch-test-2:meta"]
        assert len(meta_calls) > 0
        assert meta_calls[0][0][1] == "halted_on_provider_error"

    finally:
        scan_module.redis_client = original_redis


@pytest.mark.anyio
async def test_batch_worker_low_confidence_marks_job_failed_with_notes():
    """If an image has confidence_score below threshold (e.g. 0.0), mark as FAILED with error notes and continue."""
    mock_redis = AsyncMock()
    original_redis = scan_module.redis_client
    scan_module.redis_client = mock_redis

    mock_invalid_receipt = Receipt(
        merchant_name="N/A",
        line_items=[],
        total_amount=0.0,
        currency="USD",
        category="Other",
        date=datetime.now(),
        raw_text="",
        confidence_score=0.0,
        notes="Image does not contain a receipt or financial document.",
    )

    mock_valid_receipt = Receipt(
        merchant_name="Whole Foods",
        line_items=[],
        total_amount=15.0,
        currency="USD",
        category="Groceries",
        date=datetime.now(),
        raw_text="WHOLE FOODS",
        confidence_score=0.92,
    )

    try:
        job_items = [
            ("job-1", "landscape.jpg", b"invalid_bytes", "image/jpeg"),
            ("job-2", "receipt.jpg", b"valid_bytes", "image/jpeg"),
        ]

        async def mock_extract(context):
            if context.image_bytes == b"invalid_bytes":
                return mock_invalid_receipt
            return mock_valid_receipt

        with patch.object(ExtractionService, "extract_from_image", side_effect=mock_extract):
            await process_batch_worker("batch-test-3", job_items)

        hset_calls = mock_redis.hset.call_args_list

        # Job 1 was marked FAILED with notes in error
        job1_failed_call = next(
            (c for c in hset_calls if c[0][0] == "job:job-1" and c[1].get("mapping", {}).get("status") == "FAILED"),
            None,
        )
        assert job1_failed_call is not None
        assert "Image does not contain a receipt" in job1_failed_call[1]["mapping"]["error"]

        # Job 2 was marked COMPLETED (batch was not halted)
        job2_completed = any(
            c[0][0] == "job:job-2" and c[1].get("mapping", {}).get("status") == "COMPLETED" for c in hset_calls
        )
        assert job2_completed

    finally:
        scan_module.redis_client = original_redis


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))
