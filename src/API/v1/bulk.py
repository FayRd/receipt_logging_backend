import uuid
import logging
from typing import List
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, status

import redis.asyncio as aioredis
from google import genai
from google.genai import types

router = APIRouter(prefix="/receipts/bulk", tags=["Bulk Receipts"])
logger = logging.getLogger("receipt_bulk_processor")

# Module references set by main lifespan or helper getters
redis_client: aioredis.Redis | None = None
genai_client: genai.Client | None = None


def init_bulk_clients(r_client: aioredis.Redis, g_client: genai.Client):
    global redis_client, genai_client
    redis_client = r_client
    genai_client = g_client


# ── PHASE 3: BACKGROUND WORKER ────────────────────────────────────────────────

async def process_receipt_worker(job_id: str, image_bytes: bytes):
    """
    Background worker updating Redis job status, executing Gemini 3.5 Flash vision extraction,
    and recording completion result or failure detail.
    """
    if not redis_client or not genai_client:
        logger.error("Redis or GenAI client not initialized for worker task %s", job_id)
        return

    job_key = f"job:{job_id}"

    # Step 1: Set status to PROCESSING
    await redis_client.hset(job_key, "status", "PROCESSING")

    try:
        # Step 2: Asynchronous call to gemini-3.5-flash
        prompt = (
            "Extract the merchant name, total amount, and date from this receipt. "
            "Return ONLY a valid JSON object with keys 'merchant_name', 'total_amount', and 'date'."
        )

        response = await genai_client.aio.models.generate_content(
            model="gemini-3.5-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt,
            ],
        )

        result_text = response.text.strip() if response and response.text else "{}"

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


# ── PHASE 4: API ENDPOINTS ────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_202_ACCEPTED)
@router.post("/", status_code=status.HTTP_202_ACCEPTED)
async def create_bulk_receipt_jobs(
    files: List[UploadFile] = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    """
    Accepts multipart/form-data receipt files, dispatches background processing jobs,
    and immediately returns batch_id and job_id mappings.
    """
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis client not initialized",
        )

    batch_id = str(uuid.uuid4())
    batch_key = f"batch:{batch_id}"
    jobs_response = []

    for file in files:
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

        # Read image bytes and schedule worker
        image_bytes = await file.read()
        background_tasks.add_task(process_receipt_worker, job_id, image_bytes)

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


@router.get("/{batch_id}")
async def get_bulk_batch_status(batch_id: str):
    """
    Retrieves status and extracted payload data for all jobs under batch_id.
    """
    if not redis_client:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Redis client not initialized",
        )

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
            if job_hash.get("status") == "COMPLETED":
                completed_count += 1
            jobs_data.append(job_hash)

    return {
        "batch_id": batch_id,
        "total_jobs": len(job_ids),
        "completed_jobs": completed_count,
        "jobs": jobs_data,
    }
