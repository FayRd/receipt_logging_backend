import json
import uuid
import logging
from typing import List
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status

import redis.asyncio as aioredis
from src.Auth.identity import Identity, get_current_identity
from src.Auth.rate_limiter import rate_limit
from src.Models.schemas import BulkBatchStatusResponse, BulkJobCreateResponse, Receipt, ScanContext
from src.Services.extraction_service import ExtractionService
from src.config import get_settings

router = APIRouter(prefix="/receipts/bulk", tags=["Scanning"])
logger = logging.getLogger("receipt_bulk_processor")

# Module references set by main lifespan or helper getters
redis_client: aioredis.Redis | None = None


def init_bulk_clients(r_client: aioredis.Redis):
    global redis_client
    redis_client = r_client


# ── BACKGROUND WORKER ────────────────────────────────────────────────

async def process_receipt_worker(job_id: str, image_bytes: bytes, content_type: str):
    """
    Background worker updating Redis job status, executing Gemini vision extraction
    via ExtractionService with full Receipt schema support, and recording completion
    result or failure detail.
    """
    if not redis_client:
        logger.error("Redis client not initialized for worker task %s", job_id)
        return

    job_key = f"job:{job_id}"

    # Step 1: Set status to PROCESSING
    await redis_client.hset(job_key, "status", "PROCESSING")

    try:
        # Step 2: Use ExtractionService for full Receipt schema extraction
        service = ExtractionService()
        context = ScanContext(
            image_bytes=image_bytes,
            content_type=content_type,
            user_id=None,
            device_id=None,
        )
        receipt: Receipt = await service.extract_from_image(context)

        # Serialize the full Receipt model to JSON for storage
        result_text = receipt.model_dump_json()

        # Step 3: Write result and update status to COMPLETED
        await redis_client.hset(
            job_key,
            mapping={
                "result": result_text,
                "status": "COMPLETED",
            },
        )

    except Exception as e:
        logger.error("Error processing receipt job %s: %s", job_id, e, exc_info=True)
        # Step 4: Record error and set status to FAILED
        await redis_client.hset(
            job_key,
            mapping={
                "error": str(e),
                "status": "FAILED",
            },
        )


# ── API ENDPOINTS ────────────────────────────────────────────────────

@router.post(
    "",
    response_model=BulkJobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit bulk receipt images for async background parsing (2 to 10 files)",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_scan_per_minute))],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "format": "binary",
                                },
                                "description": "Array of 2 to 10 receipt image files (JPEG, PNG, WEBP, etc.)",
                            }
                        },
                        "required": ["files"],
                    }
                }
            }
        }
    },
)
async def create_bulk_receipt_jobs(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(
        ...,
        description="Array of 2 to 10 receipt image files (JPEG, PNG, WEBP, etc.)",
    ),
    identity: Identity = Depends(get_current_identity),
):
    """
    Accepts multipart/form-data receipt files, dispatches background processing jobs,
    and immediately returns batch_id and job_id mappings.

    Requires device authentication (X-Device-ID and X-Device-Token).
    Enforces a strict batch size of 2 to 10 images per request and per-file size ceiling.
    """
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis service unavailable. Please check Redis connection.",
        )

    # Enforce file count bounds: min 2, max 10
    if len(files) < 2 or len(files) > 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bulk receipt parsing requires between 2 and 10 image files. Received {len(files)} files.",
        )

    # Enforce image size ceiling per file
    settings = get_settings()
    file_payloads: list[tuple[bytes, str]] = []
    for file in files:
        image_bytes = await file.read()
        if len(image_bytes) > settings.max_image_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{file.filename}' exceeds maximum allowed size of {settings.max_image_size_bytes // (1024 * 1024)}MB.",
            )
        file_payloads.append((image_bytes, file.content_type or "image/jpeg"))

    batch_id = str(uuid.uuid4())
    batch_key = f"batch:{batch_id}"
    jobs_response = []

    try:
        for file, (image_bytes, content_type) in zip(files, file_payloads):
            job_id = str(uuid.uuid4())
            job_key = f"job:{job_id}"

            # Set initial PENDING status with 3600s TTL
            await redis_client.hset(
                job_key,
                mapping={
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "filename": file.filename or "receipt.jpg",
                    "status": "PENDING",
                },
            )
            await redis_client.expire(job_key, 3600)

            # Add job_id to batch set
            await redis_client.sadd(batch_key, job_id)

            # Schedule worker with pre-read bytes
            background_tasks.add_task(process_receipt_worker, job_id, image_bytes, content_type)

            jobs_response.append({
                "job_id": job_id,
                "filename": file.filename,
            })

        await redis_client.expire(batch_key, 3600)

        return {
            "batch_id": batch_id,
            "total_jobs": len(jobs_response),
            "jobs": jobs_response,
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Bulk job creation error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit bulk receipt jobs: {str(e)}",
        )


@router.get(
    "/{batch_id}",
    response_model=BulkBatchStatusResponse,
    summary="Get bulk batch parsing status and extracted receipt results",
)
async def get_bulk_batch_status(batch_id: str):
    """
    Retrieves status and extracted payload data for all jobs under batch_id.
    """
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis service unavailable. Please check Redis connection.",
        )

    try:
        batch_key = f"batch:{batch_id}"
        job_ids = await redis_client.smembers(batch_key)

        if not job_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Batch ID not found or expired",
            )

        jobs_data = []
        completed_count = 0

        for job_id in job_ids:
            job_key = f"job:{job_id}"
            job_hash = await redis_client.hgetall(job_key)
            if job_hash:
                status_val = job_hash.get("status")
                if status_val == "COMPLETED":
                    completed_count += 1

                raw_result = job_hash.get("result")
                parsed_data = None
                if raw_result:
                    try:
                        parsed_data = json.loads(raw_result)
                    except Exception:
                        parsed_data = raw_result

                job_entry = {
                    "job_id": job_hash.get("job_id", job_id),
                    "batch_id": job_hash.get("batch_id", batch_id),
                    "filename": job_hash.get("filename"),
                    "status": status_val,
                    "data": parsed_data,
                    "error": job_hash.get("error"),
                }
                jobs_data.append(job_entry)

        return {
            "batch_id": batch_id,
            "total_jobs": len(job_ids),
            "completed_jobs": completed_count,
            "jobs": jobs_data,
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error fetching batch status for {batch_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve bulk batch status: {str(e)}",
        )
