#!/usr/bin/env python3
"""
ARQ Worker & Queue Test Script
Tests ARQ Redis pool connection, task enqueueing, and burst worker execution.
Reads REDIS_CONNECTION_STRING straight from .env with NO hardcoded fallbacks.
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Add project root and .venv site-packages to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
venv_site_packages = list((PROJECT_ROOT / ".venv" / "lib").glob("python*/site-packages"))
if venv_site_packages:
    sys.path.insert(0, str(venv_site_packages[0]))

from dotenv import load_dotenv

# Load environment variables straight from .env file
env_path = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# Read straight from .env - NO HARDCODED VALUE and NO FALLBACK
REDIS_CONNECTION_STRING = os.getenv("REDIS_CONNECTION_STRING")
if not REDIS_CONNECTION_STRING:
    print("Error: REDIS_CONNECTION_STRING is not set in .env file!", file=sys.stderr)
    sys.exit(1)

from arq import create_pool
from arq.worker import Worker
from src.Models.schemas import Receipt, LineItem
from src.Queue.queue_worker import (
    WorkerSettings,
    parse_arq_redis_settings,
)
from src.Services.extraction_service import ExtractionService


import pytest

@pytest.mark.anyio
async def test_arq_queue():
    print(f"Connecting to ARQ Redis using REDIS_CONNECTION_STRING from .env...")
    redis_settings = parse_arq_redis_settings(REDIS_CONNECTION_STRING)

    # 1. Test ARQ Connection Pool
    redis_pool = await create_pool(redis_settings)
    assert redis_pool is not None, "Failed to create ARQ Redis connection pool"
    print("✓ Successfully created ARQ Redis connection pool.")

    # Mock receipt for successful extraction test
    mock_receipt = Receipt(
        merchant_name="ARQ Test Store",
        line_items=[LineItem(description="Test Item", quantity=1.0, unit_price=10.0, total_price=10.0)],
        subtotal=10.0,
        tax_amount=1.0,
        total_amount=11.0,
        currency="USD",
        category="Testing",
        date=datetime.now(),
        raw_text="ARQ TEST STORE\nTEST ITEM $10.00\nTOTAL $11.00",
        confidence_score=0.99,
    )

    test_job_id = "test_arq_job_001"
    sample_image_bytes = b"sample_receipt_bytes"

    # 2. Test Enqueueing Single Receipt Task
    print(f"Enqueueing single receipt job '{test_job_id}'...")
    job = await redis_pool.enqueue_job(
        "process_single_receipt_task",
        test_job_id,
        sample_image_bytes,
        "image/jpeg",
        "test_user",
        "test_device",
    )
    assert job is not None, "Failed to enqueue ARQ job"
    print(f"✓ Job enqueued successfully with ARQ Job ID: {job.job_id}")

    # 3. Test Burst Worker Execution with ExtractionService mocked
    print("Starting ARQ Worker in burst mode to process enqueued task...")
    with patch("src.Queue.queue_worker.ExtractionService.extract_from_image", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = mock_receipt
        worker = Worker(
            functions=WorkerSettings.functions,
            redis_settings=redis_settings,
            on_startup=WorkerSettings.on_startup,
            on_shutdown=WorkerSettings.on_shutdown,
            burst=True,
        )
        await worker.async_run()
    print("✓ ARQ Worker completed burst processing loop.")

    # 4. Verify Task State in Redis
    job_key = f"single_job:{test_job_id}"
    raw_status = await redis_pool.hget(job_key, "status")
    raw_error = await redis_pool.hget(job_key, "error")
    job_status = raw_status.decode("utf-8") if isinstance(raw_status, bytes) else raw_status
    job_error = raw_error.decode("utf-8") if isinstance(raw_error, bytes) else raw_error
    print(f"Redis Job Key '{job_key}' Status: {job_status} | Error: {job_error}")

    # Clean up test keys
    await redis_pool.delete(job_key, f"arq_test_{test_job_id}")
    await redis_pool.aclose()

    assert job_status == "COMPLETED", f"ARQ job status unexpected: {job_status} (Error: {job_error})"
    print("✓ ARQ job queue end-to-end test passed successfully!")
    return True


def main():
    try:
        asyncio.run(test_arq_queue())
        sys.exit(0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nTest interrupted by user.")
        sys.exit(130)
    except Exception as e:
        print(f"✗ Test failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
