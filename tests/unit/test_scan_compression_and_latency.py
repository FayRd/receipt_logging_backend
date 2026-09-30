import io
from PIL import Image
import pytest
from unittest.mock import AsyncMock, patch

from src.Services.image_service import compress_for_ai_scan
from src.Models.schemas import Receipt


def _create_test_image(width: int, height: int, color=(200, 100, 50)) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class TestScanCompression:
    def test_compress_large_image(self):
        """Images exceeding 1,800px longest edge should be downscaled and compressed to JPEG <= 800KB."""
        # 3000 x 2000 px image
        raw_bytes = _create_test_image(3000, 2000)
        assert len(raw_bytes) > 0

        processed_bytes, was_recompressed = compress_for_ai_scan(raw_bytes)

        assert was_recompressed is True
        assert len(processed_bytes) <= 800 * 1024

        with Image.open(io.BytesIO(processed_bytes)) as out_img:
            w, h = out_img.size
            assert max(w, h) <= 1800
            assert out_img.format == "JPEG"

    def test_compliant_image_fast_path(self):
        """Images already <= 1800px and <= 800KB should take the fast-path without modification."""
        compliant_bytes = _create_test_image(1200, 1600)
        assert len(compliant_bytes) <= 800 * 1024

        processed_bytes, was_recompressed = compress_for_ai_scan(compliant_bytes)

        assert was_recompressed is False
        assert processed_bytes == compliant_bytes

    def test_corrupt_bytes_fallback(self):
        """Corrupted image bytes should fall back gracefully to raw bytes without throwing."""
        garbage = b"not-a-valid-image-stream-data"
        processed_bytes, was_recompressed = compress_for_ai_scan(garbage)

        assert was_recompressed is False
        assert processed_bytes == garbage


class TestWorkerLatencyTracking:
    @patch("src.API.v1.scan.redis_client")
    @patch("src.API.v1.scan.ExtractionService")
    def test_process_batch_worker_records_latency_metrics(self, mock_service_cls, mock_redis):
        import asyncio
        from src.API.v1.scan import process_batch_worker
        import time

        async def _run():
            mock_redis.hget = AsyncMock(return_value=None)
            mock_redis.hset = AsyncMock()

            mock_extraction_svc = AsyncMock()
            mock_service_cls.return_value = mock_extraction_svc
            mock_extraction_svc.extract_from_image = AsyncMock(
                return_value=Receipt(
                    merchant_name="Target Store",
                    total_amount=45.50,
                    confidence_score=0.95,
                    currency="USD",
                )
            )

            test_img = _create_test_image(1200, 1600)
            received_at = time.time() - 0.5  # 500ms simulated queue wait
            job_items = [
                ("job-test-123", "receipt.jpg", test_img, "image/jpeg", received_at),
            ]

            await process_batch_worker("batch-test-123", job_items, tier="free")

            # Verify that redis_client.hset was called with timing fields for COMPLETED status
            hset_calls = mock_redis.hset.call_args_list
            completed_call = None
            for call in hset_calls:
                mapping = call.kwargs.get("mapping", {})
                if mapping.get("status") == "COMPLETED":
                    completed_call = mapping
                    break

            assert completed_call is not None, "COMPLETED status mapping was not set in Redis"
            assert "queue_duration_sec" in completed_call
            assert "preprocess_duration_sec" in completed_call
            assert "ai_duration_sec" in completed_call
            assert "total_duration_sec" in completed_call

            queue_dur = float(completed_call["queue_duration_sec"])
            total_dur = float(completed_call["total_duration_sec"])
            assert queue_dur >= 0.45
            assert total_dur >= queue_dur

        asyncio.run(_run())
