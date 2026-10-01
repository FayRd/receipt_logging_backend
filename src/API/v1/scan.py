import asyncio
import json
import time
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

import redis.asyncio as aioredis
from src.Auth.identity import Identity, get_scoped_identity, get_sse_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import BulkBatchStatusResponse, BulkJobCreateResponse, Receipt, ScanContext, ScanResponse
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Models.Users.user_repository import UserRepository
from src.Services.extraction_service import (
    ExtractionService,
    FRIENDLY_ERROR_MESSAGE,
    ProviderOverloadedError,
    is_provider_overload_error,
)
from src.Services.image_service import compress_for_ai_scan
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

def _extract_job_item(item: tuple) -> tuple[str, str, bytes, str, float]:
    """Extracts job tuple elements, ensuring received_at timestamp is present."""
    if len(item) >= 5:
        return item[0], item[1], item[2], item[3], item[4]
    return item[0], item[1], item[2], item[3], time.time()


async def _record_job_failure(
    job_key: str,
    error_msg: str,
    queue_dur: float,
    prep_dur: float,
    total_dur: float,
    ai_dur: float = 0.0,
) -> None:
    """Helper to record failed job state and latency metrics in Redis."""
    mapping = {
        "status": "FAILED",
        "error": error_msg,
        "queue_duration_sec": f"{queue_dur:.3f}",
        "preprocess_duration_sec": f"{prep_dur:.3f}",
        "total_duration_sec": f"{total_dur:.3f}",
    }
    if ai_dur > 0.0:
        mapping["ai_duration_sec"] = f"{ai_dur:.3f}"
    await redis_client.hset(job_key, mapping=mapping)


async def _handle_provider_overload(
    batch_id: str,
    job_id: str,
    index: int,
    job_items: list[tuple],
    batch_meta_key: str,
    halt_event: asyncio.Event,
    queue_dur: float,
    prep_dur: float,
    total_dur: float,
) -> None:
    """Handles an upstream 429/500 provider overload by halting unstarted queue jobs."""
    halt_event.set()
    error_msg = FRIENDLY_ERROR_MESSAGE
    await _record_job_failure(f"job:{job_id}", error_msg, queue_dur, prep_dur, total_dur)

    any_completed = False
    for prev_item in job_items:
        prev_status = await redis_client.hget(f"job:{prev_item[0]}", "status")
        if prev_status == "COMPLETED":
            any_completed = True
            break

    meta_flag = "halted_on_provider_error" if any_completed else "halted_on_first_job"
    logger.warning("Job %s failed with provider overload error. Halting remaining jobs in batch %s.", job_id, batch_id)
    await redis_client.hset(batch_meta_key, meta_flag, "true")

    for rem_item in job_items:
        rem_job_id = rem_item[0]
        if rem_job_id != job_id:
            rem_status = await redis_client.hget(f"job:{rem_job_id}", "status")
            if rem_status not in ("COMPLETED", "PROCESSING"):
                await redis_client.hset(
                    f"job:{rem_job_id}",
                    mapping={"error": error_msg, "status": "FAILED"},
                )


async def _process_single_job(
    item: tuple,
    index: int,
    total_jobs: int,
    batch_id: str,
    tier: str,
    service: ExtractionService,
    settings,
    batch_meta_key: str,
    halt_event: asyncio.Event,
    job_items: list[tuple],
) -> bool:
    """Processes a single receipt job from a batch.

    Handles redis state updates, image compression, AI extraction, and graceful failure handling.
    Returns True if job completed successfully, False otherwise.
    """
    job_id, filename, image_bytes, content_type, received_at = _extract_job_item(item)
    job_key = f"job:{job_id}"
    queue_duration = max(0.0, time.time() - received_at)

    if halt_event.is_set():
        await _record_job_failure(job_key, FRIENDLY_ERROR_MESSAGE, queue_duration, 0.0, queue_duration)
        return False

    batch_status = await redis_client.hget(batch_meta_key, "status")
    if batch_status == "CANCELLED":
        logger.info("Batch %s cancelled. Job %s cancelled.", batch_id, job_id)
        await _record_job_failure(job_key, "Batch processing timed out", queue_duration, 0.0, queue_duration)
        return False

    logger.info(
        "Worker processing started for job %s (index %d/%d, batch %s, file=%s, tier=%s, queue_time=%.2fs)",
        job_id,
        index + 1,
        total_jobs,
        batch_id,
        filename,
        tier,
        queue_duration,
    )
    await redis_client.hset(job_key, "status", "PROCESSING")

    prep_start = time.perf_counter()
    raw_size_bytes = len(image_bytes)
    processed_bytes, was_recompressed = compress_for_ai_scan(image_bytes)
    preprocess_duration = time.perf_counter() - prep_start
    final_size_bytes = len(processed_bytes)

    try:
        context = ScanContext(
            image_bytes=processed_bytes,
            content_type="image/jpeg" if was_recompressed else content_type,
            user_id=None,
            device_id=None,
            tier=tier,
        )
        ai_start = time.perf_counter()
        receipt: Receipt = await service.extract_from_image(context)
        ai_duration = time.perf_counter() - ai_start
        total_duration = time.time() - received_at

        confidence = receipt.confidence_score if receipt.confidence_score is not None else 0.0
        if confidence < settings.confidence_threshold:
            error_msg = receipt.notes or (
                f"Invalid document type. The uploaded image does not appear to be a valid receipt or "
                f"financial statement (confidence score {confidence:.2f} is below the {settings.confidence_threshold} threshold)."
            )
            logger.warning("Worker job %s confidence score %.2f is below threshold: %s", job_id, confidence, error_msg)
            await _record_job_failure(job_key, error_msg, queue_duration, preprocess_duration, total_duration, ai_duration)
            return False

        await redis_client.hset(
            job_key,
            mapping={
                "result": receipt.model_dump_json(),
                "status": "COMPLETED",
                "queue_duration_sec": f"{queue_duration:.3f}",
                "preprocess_duration_sec": f"{preprocess_duration:.3f}",
                "ai_duration_sec": f"{ai_duration:.3f}",
                "total_duration_sec": f"{total_duration:.3f}",
            },
        )

        provider = settings.effective_ai_provider
        model_name = (
            settings.gemini_vision_model_free if tier == "free" and getattr(settings, "gemini_vision_model_free", "") else settings.gemini_vision_model
            if provider == "gemini"
            else (settings.openrouter_vision_model_free if tier == "free" and getattr(settings, "openrouter_vision_model_free", "") else settings.openrouter_vision_model)
        )
        logger.debug(
            "[ScanLatency] Job %s (%s): Total=%.2fs | Queue=%.2fs | Preprocess=%.2fs%s | AI (%s:%s)=%.2fs | Size=%dKB -> %dKB",
            job_id,
            filename,
            total_duration,
            queue_duration,
            preprocess_duration,
            " (recompressed)" if was_recompressed else " (fast-path)",
            provider,
            model_name,
            ai_duration,
            raw_size_bytes // 1024,
            final_size_bytes // 1024,
        )
        logger.info("Worker job %s COMPLETED successfully in batch %s (total=%.2fs)", job_id, batch_id, total_duration)
        return True

    except Exception as e:
        total_duration = time.time() - received_at
        logger.error("Error processing receipt job %s in batch %s (elapsed=%.2fs): %s", job_id, batch_id, total_duration, e, exc_info=True)
        if is_provider_overload_error(e):
            await _handle_provider_overload(batch_id, job_id, index, job_items, batch_meta_key, halt_event, queue_duration, preprocess_duration, total_duration)
        else:
            await _record_job_failure(job_key, str(e), queue_duration, preprocess_duration, total_duration)
        return False


async def process_batch_worker(
    batch_id: str,
    job_items: list[tuple],  # [(job_id, filename, image_bytes, content_type[, received_at])]
    tier: str = "free",
) -> None:
    """Background worker that processes a batch of receipt jobs.

    - Free tier: Sequential execution (concurrency = 1).
    - Premium / Dev tier: Parallel execution bounded by min(len(job_items), 4) workers.
    - Upstream AI 429/overload halts further queue execution while preserving in-flight completions.
    """
    if not redis_client:
        logger.error("Redis client not initialized for batch worker task %s", batch_id)
        return

    service = ExtractionService()
    settings = get_settings()
    batch_meta_key = f"batch:{batch_id}:meta"
    halt_event = asyncio.Event()

    is_parallel = tier in ("premium", "dev") and len(job_items) > 1

    if not is_parallel:
        # Sequential processing for free tier or single-job batches
        for index, item in enumerate(job_items):
            if halt_event.is_set():
                break
            await _process_single_job(
                item=item,
                index=index,
                total_jobs=len(job_items),
                batch_id=batch_id,
                tier=tier,
                service=service,
                settings=settings,
                batch_meta_key=batch_meta_key,
                halt_event=halt_event,
                job_items=job_items,
            )
    else:
        # Parallel processing bounded by min(N, 4) for premium / dev tier
        max_concurrency = min(len(job_items), 4)
        sem = asyncio.Semaphore(max_concurrency)

        async def worker(index: int, item: tuple):
            if halt_event.is_set():
                job_id = item[0]
                await redis_client.hset(
                    f"job:{job_id}",
                    mapping={
                        "error": FRIENDLY_ERROR_MESSAGE,
                        "status": "FAILED",
                    },
                )
                return

            async with sem:
                if halt_event.is_set():
                    job_id = item[0]
                    await redis_client.hset(
                        f"job:{job_id}",
                        mapping={
                            "error": FRIENDLY_ERROR_MESSAGE,
                            "status": "FAILED",
                        },
                    )
                    return
                await _process_single_job(
                    item=item,
                    index=index,
                    total_jobs=len(job_items),
                    batch_id=batch_id,
                    tier=tier,
                    service=service,
                    settings=settings,
                    batch_meta_key=batch_meta_key,
                    halt_event=halt_event,
                    job_items=job_items,
                )

        await asyncio.gather(*(worker(i, it) for i, it in enumerate(job_items)))


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
    db: AsyncClient = Depends(get_supabase_client),
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
    user_repo = UserRepository(db)
    from src.Services.quota_service import get_quota_service
    quota_svc = get_quota_service()
    allowed, q_status, err_msg = await quota_svc.check_scan_quota(identity, count=1, user_repo=user_repo)
    if not allowed:
        logger.warning("Scan quota exceeded for identity (%s): %s", identity.user_id or identity.device_id, err_msg)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=err_msg,
            headers={"Retry-After": str(q_status["seconds_to_reset"])},
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
        prep_bytes, was_recompressed = compress_for_ai_scan(image_bytes)

        context = ScanContext(
            image_bytes=prep_bytes,
            content_type="image/jpeg" if was_recompressed else content_type,
            user_id=identity.user_id,
            device_id=identity.device_id,
            tier=q_status.get("tier", "free"),
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
        await quota_svc.consume_scan_quota(identity, count=1)
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

def _validate_batch_file_count(count: int, tier: str | None = None) -> None:
    """Enforces min/max file count bounds and tier batch limit (OWASP A01 & A04)."""
    if count < 1 or count > 10:
        logger.warning("Bulk receipt parsing invalid file count: %d (must be 1-10)", count)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bulk receipt parsing requires between 1 and 10 image files. Received {count} files.",
        )
    if tier:
        max_files = 10 if tier in ("premium", "dev") else 5
        if count > max_files:
            logger.warning("Bulk receipt parsing file count %d exceeds %s tier limit of %d", count, tier, max_files)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Bulk receipt parsing for {tier} tier allows a maximum of {max_files} files. Received {count} files.",
            )


async def _read_and_validate_file_payloads(files: list[UploadFile], settings) -> list[tuple[bytes, str]]:
    """Reads image bytes and validates per-file size limits."""
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
    return file_payloads


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
    db: AsyncClient = Depends(get_supabase_client),
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

    _validate_batch_file_count(len(files))

    # Enforce daily scan quota
    user_repo = UserRepository(db)
    from src.Services.quota_service import get_quota_service
    quota_svc = get_quota_service()
    allowed, q_status, err_msg = await quota_svc.check_scan_quota(identity, count=len(files), user_repo=user_repo)
    if not allowed:
        logger.warning(
            "Bulk scan quota exceeded for identity (%s): requested=%d, error=%s",
            identity.user_id or identity.device_id,
            len(files),
            err_msg,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=err_msg,
            headers={"Retry-After": str(q_status["seconds_to_reset"])},
        )

    # Enforce tier-based batch size limit (OWASP A01: Broken Access Control)
    tier = q_status.get("tier", "free")
    _validate_batch_file_count(len(files), tier=tier)

    # Enforce image size ceiling per file
    settings = get_settings()
    file_payloads = await _read_and_validate_file_payloads(files, settings)

    t_received = time.time()
    batch_id = str(uuid.uuid4())
    batch_key = f"batch:{batch_id}"
    jobs_response = []
    job_items: list[tuple[str, str, bytes, str, float]] = []

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
                    "received_at": f"{t_received:.3f}",
                },
            )
            await redis_client.expire(job_key, settings.redis_job_ttl_seconds)

            # Add job_id to batch set
            await redis_client.sadd(batch_key, job_id)

            job_items.append((job_id, filename, image_bytes, content_type, t_received))
            jobs_response.append({
                "job_id": job_id,
                "filename": filename,
            })

        # Schedule batch worker to process jobs sequentially and handle provider halts
        background_tasks.add_task(process_batch_worker, batch_id, job_items, q_status.get("tier", "free"))
        await quota_svc.consume_scan_quota(identity, count=len(files), user_repo=user_repo)

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
                payload = batch_data.model_dump() if hasattr(batch_data, "model_dump") else batch_data
                yield f"event: batch_complete\ndata: {json.dumps(payload, default=str)}\n\n"
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

