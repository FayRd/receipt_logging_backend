import asyncio
import json
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.benchmark_ai_models import (
    resolve_image_path,
    compute_model_stats,
    run_single_try,
)
from src.Models.schemas import Receipt


class TestResolveImagePath:
    def test_finds_file_in_cwd(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            temp_path = f.name
        try:
            resolved = resolve_image_path(temp_path)
            assert os.path.isabs(resolved)
            assert os.path.exists(resolved)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_finds_file_in_scripts_dir(self):
        scripts_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
        )
        test_file = os.path.join(scripts_dir, "temp_test_image.jpg")
        with open(test_file, "wb") as f:
            f.write(b"fake image bytes")

        try:
            resolved = resolve_image_path("temp_test_image.jpg")
            assert resolved == test_file
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_raises_when_not_found(self):
        with pytest.raises(FileNotFoundError):
            resolve_image_path("non_existent_image_12345.jpg")


class TestComputeModelStats:
    def test_all_successful_trials(self):
        receipt = Receipt(
            merchant_name="Trader Joe's",
            total_amount=24.50,
            currency="USD",
            confidence_score=0.92,
        )
        results = [
            {"try": 1, "success": True, "latency_sec": 1.2, "receipt": receipt, "error": None},
            {"try": 2, "success": True, "latency_sec": 1.4, "receipt": receipt, "error": None},
            {"try": 3, "success": True, "latency_sec": 1.6, "receipt": receipt, "error": None},
        ]

        stats = compute_model_stats("google/gemini-2.5-flash-lite", results)

        assert stats["total_tries"] == 3
        assert stats["successful_tries"] == 3
        assert stats["failed_tries"] == 0
        assert stats["success_rate_pct"] == 100.0
        assert stats["avg_latency_sec"] == pytest.approx(1.4, 0.01)
        assert stats["min_latency_sec"] == 1.2
        assert stats["max_latency_sec"] == 1.6
        assert stats["median_latency_sec"] == 1.4
        assert stats["sample_merchant"] == "Trader Joe's"
        assert stats["sample_total"] == 24.50

    def test_partial_failures(self):
        receipt = Receipt(
            merchant_name="Whole Foods",
            total_amount=50.00,
            currency="USD",
            confidence_score=0.88,
        )
        results = [
            {"try": 1, "success": True, "latency_sec": 2.0, "receipt": receipt, "error": None},
            {"try": 2, "success": False, "latency_sec": 0.5, "receipt": None, "error": "HTTP 429: Rate limit"},
        ]

        stats = compute_model_stats("google/gemini-2.5-flash", results)

        assert stats["total_tries"] == 2
        assert stats["successful_tries"] == 1
        assert stats["failed_tries"] == 1
        assert stats["success_rate_pct"] == 50.0
        assert stats["avg_latency_sec"] == 2.0
        assert len(stats["failures"]) == 1
        assert stats["failures"][0]["error"] == "HTTP 429: Rate limit"

    def test_all_failed_trials(self):
        results = [
            {"try": 1, "success": False, "latency_sec": 0.2, "receipt": None, "error": "HTTP 500"},
            {"try": 2, "success": False, "latency_sec": 0.2, "receipt": None, "error": "HTTP 500"},
        ]

        stats = compute_model_stats("broken-model", results)

        assert stats["total_tries"] == 2
        assert stats["successful_tries"] == 0
        assert stats["failed_tries"] == 2
        assert stats["success_rate_pct"] == 0.0
        assert stats["avg_latency_sec"] is None
        assert stats["sample_merchant"] == "N/A"


class TestRunSingleTry:
    def test_successful_receipt_parse(self):
        async def _run():
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            receipt_payload = {
                "merchant_name": "Costco",
                "total_amount": 128.45,
                "currency": "USD",
                "confidence_score": 0.95,
                "date": "2026-10-01T12:00:00Z",
                "line_items": [{"description": "Item 1", "total_price": 128.45}],
            }
            mock_resp.json.return_value = {
                "choices": [{"message": {"content": json.dumps(receipt_payload)}}]
            }
            mock_client.post = AsyncMock(return_value=mock_resp)

            res = await run_single_try(
                mock_client,
                model="google/gemini-2.5-flash-lite",
                data_url="data:image/jpeg;base64,abc",
                try_num=1,
                total_tries=3,
            )

            assert res["success"] is True
            assert res["receipt"] is not None
            assert res["receipt"].merchant_name == "Costco"
            assert res["receipt"].total_amount == 128.45
            assert res["latency_sec"] > 0

        asyncio.run(_run())

    def test_http_error_handling(self):
        async def _run():
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_resp.text = "Rate limit exceeded"
            mock_client.post = AsyncMock(return_value=mock_resp)

            res = await run_single_try(
                mock_client,
                model="google/gemini-2.5-flash-lite",
                data_url="data:image/jpeg;base64,abc",
                try_num=1,
                total_tries=3,
            )

            assert res["success"] is False
            assert res["receipt"] is None
            assert "HTTP 429" in res["error"]

        asyncio.run(_run())
