import asyncio
import json
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

import redis.asyncio as aioredis
from src.Auth.identity import Identity, get_scoped_identity, get_sse_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import BulkBatchStatusResponse, BulkJobCreateResponse, Receipt, ScanContext, ScanResponse
from src.Services.extraction_service import (
    ExtractionService,
    FRIENDLY_ERROR_MESSAGE,
    ProviderOverloadedError,
    is_provider_overload_error,
)
from src.config import get_settings

router = APIRouter(prefix="/scan", tags=["Scanning"])
logger = get_logger("API.scan")

# Module-level Redis reference, initialised by main lifespan
redis_client: aioredis.Redis | None = None


def init_redis_client(r_client: aioredis.Redis) -> None:
    """Bind the shared async Redis client to this module at application startup."""
    global redis_client
    redis_client = r_client


# ── DEPENDENCY HELPERS ────────────────────────────────────────────────

async def get_extraction_service() -> ExtractionService:
    return ExtractionService()


# ── BATCH BACKGROUND WORKER ──────────────────────────────────────────

async def process_batch_worker(
    batch_id: str,
    job_items: list[tuple[str, str, bytes, str]],  # [(job_id, filename, image_bytes, content_type)]
) -> None:
    """Background worker that processes a batch of receipt jobs sequentially.

    If the first job or any job encounters an unrecoverable 429/500/provider error:
    - Marks current job FAILED with friendly error.
    - Immediately halts remaining pending jobs and marks them FAILED with friendly error.
    - If it's the first job (index 0), records 'halted_on_first_job' in batch metadata.
    - If it's a subsequent job (index > 0), earlier completed jobs are preserved.
    """
    if not redis_client:
        logger.error("Redis client not initialized for batch worker task %s", batch_id)
        return

    service = ExtractionService()
    batch_meta_key = f"batch:{batch_id}:meta"
    start_time = asyncio.get_event_loop().time()
    settings = get_settings()

    for index, (job_id, filename, image_bytes, content_type) in enumerate(job_items):
        job_key = f"job:{job_id}"

        # Check overall timeout limit
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= settings.sse_batch_timeout_seconds:
            logger.warning("Batch %s worker timed out after %.2fs. Failing remaining jobs.", batch_id, elapsed)
            for rem_job_id, _, _, _ in job_items[index:]:
                await redis_client.hset(
                    f"job:{rem_job_id}",
                    mapping={
                        "status": "FAILED",
                        "error": "Batch processing timed out",
                    },
                )
            break

        # Step 1: Set status to PROCESSING
        logger.info(
            "Worker processing started for job %s (index %d/%d, batch %s, file=%s)",
            job_id,
            index + 1,
            len(job_items),
            batch_id,
            filename,
        )
        await redis_client.hset(job_key, "status", "PROCESSING")

        try:
            context = ScanContext(
                image_bytes=image_bytes,
                content_type=content_type,
                user_id=None,
                device_id=None,
            )
            receipt: Receipt = await service.extract_from_image(context)

            # Enforce document validation threshold (must be >= confidence_threshold)
            confidence = receipt.confidence_score if receipt.confidence_score is not None else 0.0
            if confidence < settings.confidence_threshold:
                error_msg = receipt.notes or (
                    f"Invalid document type. The uploaded image does not appear to be a valid receipt or "
                    f"financial statement (confidence score {confidence:.2f} is below the {settings.confidence_threshold} threshold)."
                )
                logger.warning(
                    "Worker job %s confidence score %.2f is below threshold %.2f (filename=%s): %s",
                    job_id,
                    confidence,
                    settings.confidence_threshold,
                    filename,
                    error_msg,
                )
                await redis_client.hset(
                    job_key,
                    mapping={
                        "status": "FAILED",
                        "error": error_msg,
                    },
                )
                # Continue processing remaining jobs in batch
                continue

            result_text = receipt.model_dump_json()

            await redis_client.hset(
                job_key,
                mapping={
                    "result": result_text,
                    "status": "COMPLETED",
                },
            )
            logger.info("Worker job %s COMPLETED successfully in batch %s", job_id, batch_id)

        except Exception as e:
            logger.error("Error processing receipt job %s in batch %s: %s", job_id, batch_id, e, exc_info=True)
            is_overload = is_provider_overload_error(e)

            if is_overload:
                error_msg = FRIENDLY_ERROR_MESSAGE
                await redis_client.hset(
                    job_key,
                    mapping={
                        "error": error_msg,
                        "status": "FAILED",
                    },
                )
                # If first job failed with provider overload error
                if index == 0:
                    logger.warning("First job %s failed with provider overload error. Halting entire batch %s.", job_id, batch_id)
                    await redis_client.hset(batch_meta_key, "halted_on_first_job", "true")
                else:
                    logger.warning("Job %s (index %d) failed with provider overload error. Halting remaining jobs in batch %s.", job_id, index, batch_id)
                    await redis_client.hset(batch_meta_key, "halted_on_provider_error", "true")

                # Mark all subsequent pending jobs in the batch as FAILED with friendly error
                for rem_job_id, _, _, _ in job_items[index + 1:]:
                    await redis_client.hset(
                        f"job:{rem_job_id}",
                        mapping={
                            "error": error_msg,
                            "status": "FAILED",
                        },
                    )
                # Halt batch processing immediately
                break
            else:
                # Non-overload error (e.g. invalid document or corrupted image): mark only this job FAILED
                await redis_client.hset(
                    job_key,
                    mapping={
                        "error": str(e),
                        "status": "FAILED",
                    },
                )


# ── SINGLE PARSE ENDPOINT (DEPRECATED) ─────────────────────────────────

@router.post(
    "/parse",
    response_model=ScanResponse,
    summary="[DEPRECATED] Submit a single receipt image for synchronous AI parsing",
    description="[DEPRECATED] Synchronous single image parsing. Please migrate to POST /api/v1/scan/parse-many (which now supports 1 to 10 files).",
    deprecated=True,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_scan_per_minute))],
)
async def parse_receipt(
    image: UploadFile = File(..., description="Receipt or financial statement image file (JPEG, PNG, WEBP, etc.)"),
    identity: Identity = Depends(get_scoped_identity),
    service: ExtractionService = Depends(get_extraction_service),
) -> ScanResponse:
    """[DEPRECATED] Accept a multipart receipt/financial statement image upload and return AI-extracted structured data.

    Note: This endpoint is deprecated. Callers should migrate to POST /api/v1/scan/parse-many.
    """
    logger.debug(
        "Entering parse_receipt (deprecated): filename=%s, content_type=%s, identity (user_id=%s, device_id=%s)",
        image.filename,
        image.content_type,
        identity.user_id,
        identity.device_id,
    )
    settings = get_settings()

    try:
        image_bytes = await image.read()
        logger.debug("Read receipt image bytes: size=%d bytes", len(image_bytes))

        # Enforce maximum upload ceiling to prevent DoS & memory exhaustion
        if len(image_bytes) > settings.max_image_size_bytes:
            logger.warning(
                "Image file size %d exceeds max limit %d bytes (filename=%s)",
                len(image_bytes),
                settings.max_image_size_bytes,
                image.filename,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Image file size exceeds maximum limit of {settings.max_image_size_bytes // (1024 * 1024)}MB.",
            )

        content_type = image.content_type or "image/jpeg"

        context = ScanContext(
            image_bytes=image_bytes,
            content_type=content_type,
            user_id=identity.user_id,
            device_id=identity.device_id,
        )

        receipt = await service.extract_from_image(context)

        # Enforce document validation threshold (must be >= confidence_threshold for valid receipts / financial statements)
        if receipt.confidence_score < settings.confidence_threshold:
            logger.warning(
                "Document confidence score %.2f is below threshold %.2f (filename=%s)",
                receipt.confidence_score,
                settings.confidence_threshold,
                image.filename,
            )
            return ScanResponse(
                success=False,
                data=None,
                error=(
                    f"Invalid document type. The uploaded image does not appear to be a valid receipt or "
                    f"financial statement (confidence score {receipt.confidence_score:.2f} is below the {settings.confidence_threshold} threshold)."
                ),
            )

        logger.info(
            "Parse receipt successful: merchant=%s, total=%.2f, confidence=%.2f",
            receipt.merchant_name,
            receipt.total_amount,
            receipt.confidence_score,
        )
        return ScanResponse(success=True, data=receipt, error=None)

    except HTTPException as he:
        logger.warning("HTTPException in parse_receipt: status_code=%d, detail=%s", he.status_code, he.detail)
        raise he
    except Exception as e:
        # Log raw exception internally for server diagnostics without exposing tracebacks to client
        logger.error(f"Receipt extraction error: {e}", exc_info=True)
        return ScanResponse(
            success=False,
            data=None,
            error="Receipt parsing failed. Please ensure the image is clear and try again.",
        )


# ── BULK / ASYNC PARSE ENDPOINTS ────────────────────────────────────────

@router.post(
    "/parse-many",
    response_model=BulkJobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit receipt images for async background parsing (1 to 10 files)",
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
                                "description": "Array of 1 to 10 receipt image files (JPEG, PNG, WEBP, etc.)",
                            }
                        },
                        "required": ["files"],
                    }
                }
            }
        }
    },
)
async def parse_many_receipts(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(
        ...,
        description="Array of 1 to 10 receipt image files (JPEG, PNG, WEBP, etc.)",
    ),
    identity: Identity = Depends(get_scoped_identity),
) -> BulkJobCreateResponse:
    """Accept multipart/form-data receipt files (1 to 10 images), dispatch background processing jobs,
    and immediately return batch_id and job_id mappings.

    Requires scoped authentication (X-Request-Type: guest or user).
    Enforces a strict batch size of 1 to 10 images per request and per-file size ceiling.
    """
    logger.debug(
        "Entering parse_many_receipts: file_count=%d, identity (user_id=%s, device_id=%s)",
        len(files),
        identity.user_id,
        identity.device_id,
    )
    if not redis_client:
        logger.error("Redis client unavailable for parse_many_receipts")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis service unavailable. Please check Redis connection.",
        )

    # Enforce file count bounds: min 1, max 10
    if len(files) < 1 or len(files) > 10:
        logger.warning("Bulk receipt parsing invalid file count: %d (must be 1-10)", len(files))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bulk receipt parsing requires between 1 and 10 image files. Received {len(files)} files.",
        )

    # Enforce image size ceiling per file
    settings = get_settings()
    file_payloads: list[tuple[bytes, str]] = []
    for file in files:
        image_bytes = await file.read()
        if len(image_bytes) > settings.max_image_size_bytes:
            logger.warning(
                "File '%s' size %d exceeds max allowed size %d",
                file.filename,
                len(image_bytes),
                settings.max_image_size_bytes,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{file.filename}' exceeds maximum allowed size of {settings.max_image_size_bytes // (1024 * 1024)}MB.",
            )
        file_payloads.append((image_bytes, file.content_type or "image/jpeg"))

    batch_id = str(uuid.uuid4())
    batch_key = f"batch:{batch_id}"
    jobs_response = []
    job_items: list[tuple[str, str, bytes, str]] = []

    try:
        for file, (image_bytes, content_type) in zip(files, file_payloads):
            job_id = str(uuid.uuid4())
            job_key = f"job:{job_id}"
            filename = file.filename or "receipt.jpg"

            # Set initial PENDING status with configured TTL
            await redis_client.hset(
                job_key,
                mapping={
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "filename": filename,
                    "status": "PENDING",
                },
            )
            await redis_client.expire(job_key, settings.redis_job_ttl_seconds)

            # Add job_id to batch set
            await redis_client.sadd(batch_key, job_id)

            job_items.append((job_id, filename, image_bytes, content_type))
            jobs_response.append({
                "job_id": job_id,
                "filename": filename,
            })

        # Schedule batch worker to process jobs sequentially and handle provider halts
        background_tasks.add_task(process_batch_worker, batch_id, job_items)

        await redis_client.expire(batch_key, settings.redis_job_ttl_seconds)

        # Store batch ownership metadata for access control
        batch_meta_key = f"batch:{batch_id}:meta"
        await redis_client.hset(
            batch_meta_key,
            mapping={
                "device_id": identity.device_id or "",
                "user_id": identity.user_id or "",
                "request_type": "user" if identity.is_authenticated else "guest",
            },
        )
        await redis_client.expire(batch_meta_key, settings.redis_job_ttl_seconds)

        logger.info(
            "Bulk parse batch created: batch_id=%s, total_jobs=%d",
            batch_id,
            len(jobs_response),
        )

        return {
            "batch_id": batch_id,
            "total_jobs": len(jobs_response),
            "jobs": jobs_response,
        }
    except HTTPException as he:
        logger.warning("HTTPException in parse_many_receipts: status_code=%d, detail=%s", he.status_code, he.detail)
        raise he
    except Exception as e:
        logger.error(f"Bulk job creation error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit bulk receipt jobs: {str(e)}",
        )


@router.get(
    "/parse-many/{batch_id}",
    response_model=BulkBatchStatusResponse,
    summary="Get bulk batch parsing status and extracted receipt results",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_scan_per_minute))],
)
async def get_parse_many_batch_status(
    batch_id: str,
    identity: Identity = Depends(get_scoped_identity),
) -> BulkBatchStatusResponse:
    """Retrieve status and extracted payload data for all jobs under a batch_id.

    Requires scoped authentication (X-Request-Type: guest or user).
    Enforces batch ownership validation (returns HTTP 403 if batch belongs to another caller).
    """
    logger.debug("Entering get_parse_many_batch_status: batch_id=%s, identity (user_id=%s, device_id=%s)", batch_id, identity.user_id, identity.device_id)
    if not redis_client:
        logger.error("Redis service unavailable when querying batch status for %s", batch_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis service unavailable. Please check Redis connection.",
        )

    try:
        # Enforce batch ownership check
        meta_hash = await redis_client.hgetall(f"batch:{batch_id}:meta")
        if meta_hash:
            owner_device = meta_hash.get("device_id")
            owner_user = meta_hash.get("user_id")
            if identity.is_authenticated:
                if owner_user and identity.user_id != owner_user:
                    logger.warning("Access denied to batch %s for user %s (owner: %s)", batch_id, identity.user_id, owner_user)
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied: batch belongs to another user.",
                    )
            else:
                if owner_device and identity.device_id != owner_device:
                    logger.warning("Access denied to batch %s for device %s (owner: %s)", batch_id, identity.device_id, owner_device)
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied: batch belongs to another device.",
                    )

        batch_key = f"batch:{batch_id}"
        job_ids = await redis_client.smembers(batch_key)

        if not job_ids:
            logger.warning("Batch ID not found or expired: batch_id=%s", batch_id)
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
                    "data": parsed_data if status_val == "COMPLETED" else None,
                    "error": job_hash.get("error"),
                }
                jobs_data.append(job_entry)

        logger.info(
            "Retrieved batch status: batch_id=%s, total_jobs=%d, completed_jobs=%d",
            batch_id,
            len(job_ids),
            completed_count,
        )

        return {
            "batch_id": batch_id,
            "total_jobs": len(job_ids),
            "completed_jobs": completed_count,
            "jobs": jobs_data,
        }
    except HTTPException as he:
        logger.warning("HTTPException in get_parse_many_batch_status: status_code=%d, detail=%s", he.status_code, he.detail)
        raise he
    except Exception as e:
        logger.error(f"Error fetching batch status for {batch_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve bulk batch status: {str(e)}",
        )


@router.get(
    "/parse-many/{batch_id}/stream",
    summary="SSE stream — emits batch_complete event with full extracted JSON data payload when finished",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_scan_per_minute))],
)
async def stream_parse_many_batch(
    batch_id: str,
    identity: Identity = Depends(get_sse_identity),
):
    """Open an SSE connection and poll Redis until every job in the batch reaches a terminal state
    (COMPLETED or FAILED).

    Supports authentication via Headers (X-Device-ID/Token) or Query Parameters (device_id/token).

    Emits full JSON results payload directly over SSE::

        event: batch_complete
        data: {"batch_id": "...", "total_jobs": 2, "completed_jobs": 2, "jobs": [...]}

    On timeout::

        event: timeout
        data: {"error": "Batch polling timed out"}

    On error::

        event: error
        data: {"error": "Invalid batch or service unavailable"}
    """
    logger.debug(
        "Entering stream_parse_many_batch: batch_id=%s, identity (user_id=%s, device_id=%s)",
        batch_id,
        identity.user_id,
        identity.device_id,
    )
    settings = get_settings()
    poll_interval = settings.sse_poll_interval_seconds
    timeout = settings.sse_batch_timeout_seconds

    async def event_generator():
        logger.info("SSE stream opened for batch %s", batch_id)
        if not redis_client:
            logger.error("SSE stream error: Redis client unavailable for batch %s", batch_id)
            yield f"event: error\ndata: {json.dumps({'error': 'Redis service unavailable'})}\n\n"
            return

        batch_key = f"batch:{batch_id}"
        job_ids = await redis_client.smembers(batch_key)

        if not job_ids:
            logger.warning("SSE stream error: Batch %s not found or expired", batch_id)
            yield f"event: error\ndata: {json.dumps({'error': 'Batch ID not found or expired'})}\n\n"
            return

        # Enforce batch ownership check
        meta_hash = await redis_client.hgetall(f"batch:{batch_id}:meta")
        if meta_hash:
            owner_device = meta_hash.get("device_id")
            owner_user = meta_hash.get("user_id")
            is_owner = (
                (identity.is_authenticated and owner_user and identity.user_id == owner_user)
                or (owner_device and identity.device_id == owner_device)
            )
            if not is_owner:
                logger.warning("SSE access denied to batch %s for device %s", batch_id, identity.device_id)
                yield f"event: error\ndata: {json.dumps({'error': 'Access denied: batch belongs to another identity'})}\n\n"
                return

        elapsed = 0.0
        terminal = {"COMPLETED", "FAILED"}
        last_reported_completed = 0

        while elapsed < timeout:
            statuses = []
            for job_id in job_ids:
                job_hash = await redis_client.hgetall(f"job:{job_id}")
                statuses.append(job_hash.get("status", "PENDING"))

            completed_count = sum(1 for s in statuses if s in terminal)

            if completed_count > last_reported_completed and not all(s in terminal for s in statuses):
                last_reported_completed = completed_count
                progress_payload = {
                    "batch_id": batch_id,
                    "total_jobs": len(job_ids),
                    "completed_jobs": completed_count,
                }
                logger.info(
                    "Batch %s progress: %d/%d jobs completed. Emitting progress SSE event.",
                    batch_id,
                    completed_count,
                    len(job_ids),
                )
                yield f"event: progress\ndata: {json.dumps(progress_payload)}\n\n"

            if all(s in terminal for s in statuses):
                meta_hash = await redis_client.hgetall(f"batch:{batch_id}:meta")
                halted_on_first = (meta_hash.get("halted_on_first_job") == "true") if meta_hash else False

                # If batch halted on first job due to 429/500 provider error with 0 completed receipts
                if halted_on_first and completed_count == 0:
                    logger.warning(
                        "Batch %s halted on first job due to provider error. Emitting error SSE event.",
                        batch_id,
                    )
                    yield f"event: error\ndata: {json.dumps({'error': FRIENDLY_ERROR_MESSAGE})}\n\n"
                    return

                # Fetch complete batch data object and send directly in SSE data field
                batch_data = await get_parse_many_batch_status(batch_id, identity=identity)
                logger.info(
                    "Batch %s complete (%d completed, %d failed). Emitting batch_complete SSE event.",
                    batch_id,
                    completed_count,
                    len(job_ids) - completed_count,
                )
                yield f"event: batch_complete\ndata: {json.dumps(batch_data)}\n\n"
                return

            # Keep-alive comment to prevent proxy/nginx from closing idle connection
            yield ": keep-alive\n\n"
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout
        logger.warning("Batch %s SSE stream timed out after %ds", batch_id, timeout)
        yield f"event: timeout\ndata: {json.dumps({'error': 'Batch processing timed out'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

