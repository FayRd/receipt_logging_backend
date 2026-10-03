from datetime import datetime, timezone
from supabase import AsyncClient
from src.Infrastructure.logger import get_logger

logger = get_logger("Services.storage_scrubber")


async def _list_all_storage_objects(bucket_proxy, folder_path: str) -> list[str]:
    """Recursively list all file paths under folder_path in a Supabase Storage bucket."""
    files: list[str] = []
    clean_folder = folder_path.strip("/")
    items = await bucket_proxy.list(path=clean_folder)

    if not items or not isinstance(items, list):
        return files

    for item in items:
        name = item.get("name")
        if not name or name == ".emptyFolderPlaceholder":
            continue

        item_path = f"{clean_folder}/{name}" if clean_folder else name
        # In Supabase Storage, directory entries have id is None or metadata is None
        if item.get("id") is None or item.get("metadata") is None:
            sub_files = await _list_all_storage_objects(bucket_proxy, item_path)
            files.extend(sub_files)
        else:
            files.append(item_path)

    return files


async def scrub_pending_storage_deletions(db: AsyncClient, bucket_name: str = "user-data") -> int:
    """Process pending storage deletion jobs from storage_deletion_queue.

    1. Reads storage_deletion_queue where status IN ('pending', 'in_progress') AND attempt_count < 5.
    2. Recursively lists all objects under prefix row['storage_prefix'] in bucket 'user-data'.
    3. Deletes all objects via db.storage.from_('user-data').remove(paths).
    4. Updates storage_deletion_queue row to status = 'complete', completed_at = now().
    5. Updates deletion_audit_log with storage_scrubbed_at if a matching audit record exists.
    """
    logger.info("Starting scrub_pending_storage_deletions for bucket '%s'", bucket_name)
    try:
        res = (
            await db.table("storage_deletion_queue")
            .select("*")
            .in_("status", ["pending", "in_progress"])
            .lt("attempt_count", 5)
            .execute()
        )
        jobs = res.data if res and res.data else []
    except Exception as fetch_err:
        logger.error("Failed to query storage_deletion_queue: %s", fetch_err)
        return 0

    if not jobs:
        logger.debug("No pending storage deletion jobs to process.")
        return 0

    completed_count = 0
    bucket_proxy = db.storage.from_(bucket_name)

    for job in jobs:
        job_id = job["id"]
        user_id = str(job["user_id"])
        raw_prefix = job.get("storage_prefix") or f"{user_id}/"
        # Normalize prefix (strip bucket name prefix if included, and trailing slashes)
        clean_prefix = raw_prefix.removeprefix(f"{bucket_name}/").strip("/")
        attempt_count = job.get("attempt_count", 0) + 1
        attempted_at = datetime.now(timezone.utc).isoformat()

        # Update to in_progress
        try:
            await (
                db.table("storage_deletion_queue")
                .update({
                    "status": "in_progress",
                    "attempt_count": attempt_count,
                    "attempted_at": attempted_at,
                })
                .eq("id", job_id)
                .execute()
            )
        except Exception as upd_err:
            logger.warning("Failed to update job %s to in_progress: %s", job_id, upd_err)

        try:
            paths_to_remove = await _list_all_storage_objects(bucket_proxy, clean_prefix)
            if paths_to_remove:
                logger.info(
                    "Deleting %d objects under prefix '%s' for user %s: %s",
                    len(paths_to_remove),
                    clean_prefix,
                    user_id,
                    paths_to_remove,
                )
                await bucket_proxy.remove(paths_to_remove)
            else:
                logger.info("No storage objects found under prefix '%s' for user %s", clean_prefix, user_id)

            completed_at = datetime.now(timezone.utc).isoformat()
            await (
                db.table("storage_deletion_queue")
                .update({
                    "status": "complete",
                    "completed_at": completed_at,
                })
                .eq("id", job_id)
                .execute()
            )

            # Update deletion_audit_log timestamp
            try:
                await (
                    db.table("deletion_audit_log")
                    .update({"storage_scrubbed_at": completed_at})
                    .eq("user_id", user_id)
                    .execute()
                )
            except Exception as audit_err:
                logger.debug("Failed updating storage_scrubbed_at in deletion_audit_log: %s", audit_err)

            completed_count += 1
            logger.info("Storage scrub completed successfully for user %s (job_id=%s)", user_id, job_id)

        except Exception as scrub_err:
            logger.error("Storage scrub failed for job %s (attempt %d): %s", job_id, attempt_count, scrub_err, exc_info=True)
            new_status = "failed" if attempt_count >= 5 else "pending"
            try:
                await (
                    db.table("storage_deletion_queue")
                    .update({"status": new_status})
                    .eq("id", job_id)
                    .execute()
                )
            except Exception as fallback_err:
                logger.error("Failed to update job %s to %s: %s", job_id, new_status, fallback_err)

    return completed_count
