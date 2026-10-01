import asyncio
import io
import time
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image
import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from src.API.v1.scan import parse_many_receipts, process_batch_worker
from src.Auth.identity import Identity
from src.Models.schemas import Receipt


def _create_test_image(width: int = 100, height: int = 100) -> bytes:
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _create_mock_upload_file(filename: str = "receipt.jpg", content: bytes = None) -> UploadFile:
    if content is None:
        content = _create_test_image()
    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = filename
    mock_file.content_type = "image/jpeg"
    mock_file.read = AsyncMock(return_value=content)
    return mock_file


class TestParseManyTierLimits:
    @patch("src.API.v1.scan.redis_client")
    @patch("src.Services.quota_service.get_quota_service")
    def test_parse_many_rejects_free_tier_with_more_than_5_files(self, mock_get_quota_svc, mock_redis):
        """Free tier user submitting 6 files should be rejected with HTTP 400 Bad Request."""
        mock_quota_svc = MagicMock()
        mock_get_quota_svc.return_value = mock_quota_svc
        mock_quota_svc.check_scan_quota = AsyncMock(
            return_value=(True, {"tier": "free", "seconds_to_reset": 3600}, None)
        )

        files = [_create_mock_upload_file(f"receipt_{i}.jpg") for i in range(6)]
        identity = Identity(user_id="free-user-123", is_authenticated=True)
        bg_tasks = BackgroundTasks()

        async def _run():
            with pytest.raises(HTTPException) as exc_info:
                await parse_many_receipts(
                    background_tasks=bg_tasks,
                    files=files,
                    identity=identity,
                    db=MagicMock(),
                )
            assert exc_info.value.status_code == 400
            assert "Bulk receipt parsing for free tier allows a maximum of 5 files" in exc_info.value.detail
            assert "Received 6 files" in exc_info.value.detail

        asyncio.run(_run())

    @patch("src.API.v1.scan.redis_client")
    @patch("src.Services.quota_service.get_quota_service")
    def test_parse_many_allows_free_tier_up_to_5_files(self, mock_get_quota_svc, mock_redis):
        """Free tier user submitting 5 files should be accepted with HTTP 202."""
        mock_quota_svc = MagicMock()
        mock_get_quota_svc.return_value = mock_quota_svc
        mock_quota_svc.check_scan_quota = AsyncMock(
            return_value=(True, {"tier": "free", "seconds_to_reset": 3600}, None)
        )
        mock_quota_svc.consume_scan_quota = AsyncMock()

        mock_redis.hset = AsyncMock()
        mock_redis.expire = AsyncMock()
        mock_redis.sadd = AsyncMock()

        files = [_create_mock_upload_file(f"receipt_{i}.jpg") for i in range(5)]
        identity = Identity(user_id="free-user-123", is_authenticated=True)
        bg_tasks = BackgroundTasks()

        async def _run():
            resp = await parse_many_receipts(
                background_tasks=bg_tasks,
                files=files,
                identity=identity,
                db=MagicMock(),
            )
            assert resp["total_jobs"] == 5
            assert len(resp["jobs"]) == 5
            assert mock_quota_svc.consume_scan_quota.called

        asyncio.run(_run())

    @patch("src.API.v1.scan.redis_client")
    @patch("src.Services.quota_service.get_quota_service")
    def test_parse_many_allows_premium_tier_up_to_10_files(self, mock_get_quota_svc, mock_redis):
        """Premium user submitting 10 files should be accepted with HTTP 202."""
        mock_quota_svc = MagicMock()
        mock_get_quota_svc.return_value = mock_quota_svc
        mock_quota_svc.check_scan_quota = AsyncMock(
            return_value=(True, {"tier": "premium", "seconds_to_reset": 3600}, None)
        )
        mock_quota_svc.consume_scan_quota = AsyncMock()

        mock_redis.hset = AsyncMock()
        mock_redis.expire = AsyncMock()
        mock_redis.sadd = AsyncMock()

        files = [_create_mock_upload_file(f"receipt_{i}.jpg") for i in range(10)]
        identity = Identity(user_id="prem-user-123", is_authenticated=True)
        bg_tasks = BackgroundTasks()

        async def _run():
            resp = await parse_many_receipts(
                background_tasks=bg_tasks,
                files=files,
                identity=identity,
                db=MagicMock(),
            )
            assert resp["total_jobs"] == 10
            assert len(resp["jobs"]) == 10

        asyncio.run(_run())

    @patch("src.API.v1.scan.redis_client")
    @patch("src.Services.quota_service.get_quota_service")
    def test_parse_many_rejects_more_than_10_files(self, mock_get_quota_svc, mock_redis):
        """Submitting 11 files should be rejected at the boundary check before quota verification."""
        files = [_create_mock_upload_file(f"receipt_{i}.jpg") for i in range(11)]
        identity = Identity(user_id="prem-user-123", is_authenticated=True)
        bg_tasks = BackgroundTasks()

        async def _run():
            with pytest.raises(HTTPException) as exc_info:
                await parse_many_receipts(
                    background_tasks=bg_tasks,
                    files=files,
                    identity=identity,
                    db=MagicMock(),
                )
            assert exc_info.value.status_code == 400
            assert "requires between 1 and 10 image files" in exc_info.value.detail

        asyncio.run(_run())


class TestProcessBatchWorkerConcurrency:
    @patch("src.API.v1.scan.redis_client")
    @patch("src.API.v1.scan.ExtractionService")
    def test_free_tier_runs_sequentially(self, mock_service_cls, mock_redis):
        """Free tier batch processing must run strictly sequentially (max concurrency = 1)."""
        active_concurrency = 0
        max_concurrency_observed = 0

        async def mock_extract(context):
            nonlocal active_concurrency, max_concurrency_observed
            active_concurrency += 1
            if active_concurrency > max_concurrency_observed:
                max_concurrency_observed = active_concurrency
            await asyncio.sleep(0.02)
            active_concurrency -= 1
            return Receipt(
                merchant_name="Store",
                total_amount=10.0,
                confidence_score=0.95,
                currency="USD",
            )

        mock_svc = AsyncMock()
        mock_svc.extract_from_image = mock_extract
        mock_service_cls.return_value = mock_svc

        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis.hset = AsyncMock()

        test_img = _create_test_image()
        now = time.time()
        job_items = [(f"job-{i}", f"receipt_{i}.jpg", test_img, "image/jpeg", now) for i in range(5)]

        async def _run():
            await process_batch_worker("batch-free-seq", job_items, tier="free")
            assert max_concurrency_observed == 1, f"Expected concurrency 1, got {max_concurrency_observed}"

        asyncio.run(_run())

    @patch("src.API.v1.scan.redis_client")
    @patch("src.API.v1.scan.ExtractionService")
    def test_premium_tier_runs_with_parallel_concurrency(self, mock_service_cls, mock_redis):
        """Premium tier batch processing must run concurrently with semaphore ceiling min(N, 4)."""
        active_concurrency = 0
        max_concurrency_observed = 0

        async def mock_extract(context):
            nonlocal active_concurrency, max_concurrency_observed
            active_concurrency += 1
            if active_concurrency > max_concurrency_observed:
                max_concurrency_observed = active_concurrency
            await asyncio.sleep(0.03)
            active_concurrency -= 1
            return Receipt(
                merchant_name="Store",
                total_amount=20.0,
                confidence_score=0.95,
                currency="USD",
            )

        mock_svc = AsyncMock()
        mock_svc.extract_from_image = mock_extract
        mock_service_cls.return_value = mock_svc

        mock_redis.hget = AsyncMock(return_value=None)
        mock_redis.hset = AsyncMock()

        test_img = _create_test_image()
        now = time.time()
        # 6 jobs with semaphore cap 4: should observe concurrency of 4
        job_items = [(f"job-{i}", f"receipt_{i}.jpg", test_img, "image/jpeg", now) for i in range(6)]

        async def _run():
            await process_batch_worker("batch-prem-par", job_items, tier="premium")
            assert max_concurrency_observed == 4, f"Expected concurrency 4, got {max_concurrency_observed}"

        asyncio.run(_run())

    @patch("src.API.v1.scan.redis_client")
    @patch("src.API.v1.scan.ExtractionService")
    def test_provider_429_graceful_halt(self, mock_service_cls, mock_redis):
        """When an upstream 429 provider error occurs, unstarted jobs must halt gracefully."""
        from src.API.v1.scan import FRIENDLY_ERROR_MESSAGE

        call_count = 0

        async def mock_extract(context):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First job succeeds
                return Receipt(
                    merchant_name="Store",
                    total_amount=15.0,
                    confidence_score=0.95,
                    currency="USD",
                )
            # Second job fails with 429 rate limit
            raise Exception("Rate limit exceeded 429: OpenRouter provider overloaded")

        mock_svc = AsyncMock()
        mock_svc.extract_from_image = mock_extract
        mock_service_cls.return_value = mock_svc

        # Store job statuses in a simulated dict
        redis_store = {}

        async def mock_hset(key, field=None, value=None, mapping=None):
            if mapping:
                redis_store.setdefault(key, {}).update(mapping)
            elif field and value:
                redis_store.setdefault(key, {})[field] = value

        async def mock_hget(key, field):
            return redis_store.get(key, {}).get(field)

        mock_redis.hset = mock_hset
        mock_redis.hget = mock_hget

        test_img = _create_test_image()
        now = time.time()
        job_items = [(f"job-{i}", f"receipt_{i}.jpg", test_img, "image/jpeg", now) for i in range(4)]

        async def _run():
            await process_batch_worker("batch-halt-test", job_items, tier="free")
            # First job completed
            assert redis_store.get("job:job-0", {}).get("status") == "COMPLETED"
            # Second job failed
            assert redis_store.get("job:job-1", {}).get("status") == "FAILED"
            assert redis_store.get("job:job-1", {}).get("error") == FRIENDLY_ERROR_MESSAGE
            # Subsequent jobs halted
            assert redis_store.get("job:job-2", {}).get("status") == "FAILED"
            assert redis_store.get("job:job-2", {}).get("error") == FRIENDLY_ERROR_MESSAGE
            assert redis_store.get("job:job-3", {}).get("status") == "FAILED"
            assert redis_store.get("job:job-3", {}).get("error") == FRIENDLY_ERROR_MESSAGE
            # Halted on provider error flag set on batch metadata
            assert redis_store.get("batch:batch-halt-test:meta", {}).get("halted_on_provider_error") == "true"

        asyncio.run(_run())
