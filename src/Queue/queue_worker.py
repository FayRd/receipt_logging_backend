import logging
import json
from arq.connections import RedisSettings
from dotenv import load_dotenv
import redis.asyncio as aioredis

load_dotenv()

from src.config import get_settings
from src.Models.schemas import ScanContext, Receipt
from src.Services.extraction_service import ExtractionService

logger = logging.getLogger("queue_worker")


async def startup(ctx: dict):
    """Initialize Redis and shared context on worker startup."""
    settings = get_settings()
    ctx["settings"] = settings
    ctx["redis"] = aioredis.from_url(settings.redis_connection_string, decode_responses=True)
    logger.info("ARQ Worker started successfully")


async def shutdown(ctx: dict):
    """Clean up resources on worker shutdown."""
    redis_client: aioredis.Redis = ctx.get("redis")
    if redis_client:
        await redis_client.aclose()
    logger.info("ARQ Worker shut down")


async def process_single_receipt_task(
    ctx: dict,
    job_id: str,
    image_bytes: bytes,
    content_type: str,
    user_id: str | None,
    device_id: str,
):
    """Background ARQ task processing single receipt extraction."""
    redis_client: aioredis.Redis = ctx["redis"]
    settings = ctx["settings"]
    job_key = f"single_job:{job_id}"

    await redis_client.hset(job_key, "status", "PROCESSING")

    try:
        service = ExtractionService()
        context = ScanContext(
            image_bytes=image_bytes,
            content_type=content_type,
            user_id=user_id,
            device_id=device_id,
        )
        receipt: Receipt = await service.extract_from_image(context)

        if receipt.confidence_score < settings.confidence_threshold:
            err_msg = f"Invalid document type (confidence score {receipt.confidence_score:.2f} below threshold)."
            await redis_client.hset(job_key, mapping={"status": "FAILED", "error": err_msg})
            await redis_client.expire(job_key, settings.redis_job_ttl_seconds)
            return

        await redis_client.hset(
            job_key,
            mapping={
                "result": receipt.model_dump_json(),
                "status": "COMPLETED",
            },
        )
        await redis_client.expire(job_key, settings.redis_job_ttl_seconds)
        logger.info("Job %s completed successfully", job_id)

    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        await redis_client.hset(
            job_key,
            mapping={
                "status": "FAILED",
                "error": "Receipt parsing failed. Please try again with a clearer image.",
            },
        )
        await redis_client.expire(job_key, settings.redis_job_ttl_seconds)


async def process_bulk_receipt_task(
    ctx: dict,
    job_id: str,
    batch_id: str,
    image_bytes: bytes,
    content_type: str,
    filename: str,
):
    """Background ARQ task processing bulk batch receipt extraction."""
    redis_client: aioredis.Redis = ctx["redis"]
    settings = ctx["settings"]
    job_key = f"job:{job_id}"

    await redis_client.hset(job_key, "status", "PROCESSING")

    try:
        service = ExtractionService()
        context = ScanContext(
            image_bytes=image_bytes,
            content_type=content_type,
            user_id=None,
            device_id="",
        )
        receipt: Receipt = await service.extract_from_image(context)

        await redis_client.hset(
            job_key,
            mapping={
                "result": receipt.model_dump_json(),
                "status": "COMPLETED",
            },
        )
        await redis_client.expire(job_key, settings.redis_job_ttl_seconds)
        logger.info("Bulk job %s in batch %s completed", job_id, batch_id)

    except Exception as e:
        logger.error("Bulk job %s in batch %s failed: %s", job_id, batch_id, e, exc_info=True)
        await redis_client.hset(
            job_key,
            mapping={
                "status": "FAILED",
                "error": str(e),
            },
        )
        await redis_client.expire(job_key, settings.redis_job_ttl_seconds)


import urllib.parse

settings = get_settings()


def parse_arq_redis_settings(dsn: str) -> RedisSettings:
    """Parse DSN string into RedisSettings, ensuring URL-encoded special characters (%25, etc.) in passwords are properly unquoted."""
    rs = RedisSettings.from_dsn(dsn)
    if rs.password:
        rs.password = urllib.parse.unquote(rs.password)
    if rs.username:
        rs.username = urllib.parse.unquote(rs.username)
    rs.conn_timeout = 10
    return rs


class WorkerSettings:
    """ARQ worker configuration."""
    functions = [process_single_receipt_task, process_bulk_receipt_task]
    redis_settings = parse_arq_redis_settings(settings.redis_connection_string)
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    max_tries = 3
