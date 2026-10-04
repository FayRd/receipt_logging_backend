from typing import Any, Optional
import boto3
from botocore.config import Config

from src.Infrastructure.logger import get_logger
from src.config import get_settings

logger = get_logger("Services.r2_scrubber")


def get_r2_client() -> Optional[Any]:
    """Initialize and return a boto3 S3 client configured for Cloudflare R2.

    Returns None if R2 credentials or account ID are not configured.
    """
    settings = get_settings()
    if not settings.r2_account_id or not settings.r2_access_key_id:
        logger.debug("Cloudflare R2 backup storage is not configured (missing account ID or access key).")
        return None

    try:
        return boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    except Exception as exc:
        logger.error("Failed to initialize R2 client: %s", exc)
        return None


def purge_user_r2_backups(user_id: str) -> int:
    """Purge all R2 backup objects belonging to user_id.

    Scans keys under 'objects/' prefix in settings.r2_bucket_name.
    Matches paths structured as: objects/<date>/<user_id>/...
    (where path split by '/' has <user_id> at index 2).

    Deletes matching keys in batches of up to 1000 via delete_objects.
    Catches all exceptions, logs errors, and returns the count of purged objects.
    """
    clean_uid = str(user_id).strip()
    if not clean_uid:
        return 0

    try:
        client = get_r2_client()
        if client is None:
            logger.info("R2 client not configured; skipping R2 backup purge for user_id=%s", clean_uid)
            return 0

        settings = get_settings()
        bucket = settings.r2_bucket_name
        paginator = client.get_paginator("list_objects_v2")

        matching_keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix="objects/"):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                parts = key.split("/")
                if len(parts) > 2 and parts[0] == "objects" and parts[2] == clean_uid:
                    matching_keys.append(key)

        if not matching_keys:
            logger.info("No R2 backup objects found for user_id=%s", clean_uid)
            return 0

        total_purged = 0
        batch_size = 1000
        for i in range(0, len(matching_keys), batch_size):
            batch = matching_keys[i : i + batch_size]
            delete_payload = [{"Key": k} for k in batch]
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": delete_payload},
            )
            total_purged += len(batch)

        logger.info(
            "Successfully purged %d R2 backup object(s) for user_id=%s from bucket=%s",
            total_purged,
            clean_uid,
            bucket,
        )
        return total_purged
    except Exception as exc:
        logger.error("Error purging R2 backups for user_id=%s: %s", clean_uid, exc)
        return 0
