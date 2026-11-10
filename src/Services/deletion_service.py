"""DeletionService: Shared account deletion cascade used by both
the authenticated API endpoint and the public web deletion flow.
"""

import hashlib
from datetime import datetime, timezone
from fastapi import BackgroundTasks
from supabase._async.client import AsyncClient
from src.Infrastructure.logger import get_logger
from src.Infrastructure.key_vault import get_key_vault
from src.config import get_settings
from src.Services.storage_scrubber import scrub_pending_storage_deletions
from src.Services.backup_scrubber import scrub_user_from_all_backups
from src.Services.r2_scrubber import purge_user_r2_backups

logger = get_logger("Services.deletion_service")
_settings = get_settings()
key_vault = get_key_vault()


async def _tombstone_user_devices(user_id: str, db: AsyncClient, kek: str) -> list[str]:
    """Tombstone user devices under Option B: null push tokens/user_id, hash device_id for anti-abuse."""
    hashed_device_ids: list[str] = []
    try:
        devices_res = await db.table("devices").select("*").eq("user_id", user_id).execute()
        device_rows = devices_res.data if devices_res and devices_res.data else []
        for dev in device_rows:
            dev_raw_id = dev.get("device_id") or dev.get("name") or ""
            hashed_id = hashlib.sha256((dev_raw_id + kek).encode("utf-8")).hexdigest()
            hashed_device_ids.append(hashed_id)

            update_dev = {"user_id": None}
            if "name" in dev and "device_id" not in dev:
                update_dev["name"] = hashed_id
            else:
                update_dev["device_id"] = hashed_id
                update_dev["fcm_token"] = None
                if "name" in dev:
                    update_dev["name"] = hashed_id
            if "fcm_token" in dev:
                update_dev["fcm_token"] = None

            dev_pk = dev.get("id")
            if dev_pk:
                await db.table("devices").update(update_dev).eq("id", dev_pk).execute()
            else:
                await db.table("devices").update(update_dev).eq("user_id", user_id).execute()
    except Exception as dev_err:
        logger.error("Error during device tombstoning for user %s: %s", user_id, dev_err)
    return hashed_device_ids


async def execute_account_deletion(
    user_id: str,
    user_row: dict,
    db: AsyncClient,
    background_tasks: BackgroundTasks,
) -> dict:
    """Execute the full 10-step account deletion cascade.

    1. Writes start record to deletion_audit_log (status='pending').
    2. Destroys per-user DEK in user_keys (crypto-shredding enc_version=1 data).
    3. Hard-deletes legacy enc_version=0 receipts and conversations.
    4. Enqueues storage deletion in storage_deletion_queue.
    5. Tombstones linked devices (Option B: hashed device ID, fcm_token=NULL, user_id=NULL).
    6. Tombstones user identity in users table (email=deleted_<id>@deleted.local, username=deleted_<id>, deleted_at=now()).
    7. Updates deletion_audit_log (status='complete', timestamps, counts).
    8-10. Dispatches background tasks for backup CSV scrub, storage scrub, and R2 scrub.

    Returns dict with rows_hard_deleted, rows_crypto_shredded.
    """
    logger.debug("Executing account deletion cascade for user_id=%s", user_id)
    username = user_row.get("username")
    raw_email = str(user_row.get("email") or "").strip().lower()
    email_hash = hashlib.sha256(raw_email.encode("utf-8")).hexdigest() if raw_email else None
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Audit log start: Insert pending record into deletion_audit_log
    audit_id = None
    try:
        audit_res = await db.table("deletion_audit_log").insert({
            "user_id": user_id,
            "username": username,
            "email_hash": email_hash,
            "requested_at": now_iso,
            "status": "pending",
        }).execute()
        if audit_res and audit_res.data:
            audit_id = audit_res.data[0].get("id")
    except Exception as audit_init_err:
        logger.warning("Failed to insert initial record in deletion_audit_log: %s", audit_init_err)

    # 2. Destroy user DEK: crypto-shreds all enc_version=1 records
    await key_vault.destroy_user_dek(user_id, db)
    dek_destroyed_at = datetime.now(timezone.utc).isoformat()

    # 3. Hard-delete legacy enc_version = 0 rows
    rows_hard_deleted = 0
    rows_crypto_shredded = 0
    try:
        rec_hard = await db.table("receipts").select("id").eq("user_id", user_id).eq("enc_version", 0).execute()
        conv_hard = await db.table("conversations").select("id").eq("user_id", user_id).eq("enc_version", 0).execute()
        rows_hard_deleted = len(rec_hard.data or []) + len(conv_hard.data or [])

        rec_crypto = await db.table("receipts").select("id").eq("user_id", user_id).neq("enc_version", 0).execute()
        conv_crypto = await db.table("conversations").select("id").eq("user_id", user_id).neq("enc_version", 0).execute()
        rows_crypto_shredded = len(rec_crypto.data or []) + len(conv_crypto.data or [])

        await db.table("receipts").delete().eq("user_id", user_id).eq("enc_version", 0).execute()
        await db.table("conversations").delete().eq("user_id", user_id).eq("enc_version", 0).execute()
    except Exception as del_err:
        logger.error("Error hard-deleting legacy enc_version=0 rows for user %s: %s", user_id, del_err)

    # 4. Enqueue Storage scrub in storage_deletion_queue
    try:
        await db.table("storage_deletion_queue").insert({
            "user_id": user_id,
            "storage_prefix": f"{user_id}/",
            "status": "pending",
        }).execute()
    except Exception as queue_err:
        logger.error("Failed to enqueue storage deletion for user %s: %s", user_id, queue_err)

    # 5. Device Tombstone (Option B: Anti-Abuse Pseudonymization)
    hashed_device_ids = await _tombstone_user_devices(user_id, db, _settings.data_encryption_key)

    # 6. Identity Tombstone on users table
    # Strip trial_device_id from preferences to enforce GDPR storage limitation (Art. 5(1)(e))
    current_prefs = dict(user_row.get("preferences") or {})
    current_prefs.pop("trial_device_id", None)

    identity_tombstone = {
        "email": f"deleted_{user_id}@deleted.local",
        "username": f"deleted_{user_id}",
        "country_code": None,
        "mobile_number": None,
        "mobile_hash": None,
        "avatar_image_path": None,
        "custom_categories": [],
        "preferences": {},
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.table("users").update(identity_tombstone).eq("id", user_id).execute()

    # 7. Update deletion_audit_log to complete
    audit_update_payload = {
        "status": "complete",
        "dek_destroyed_at": dek_destroyed_at,
        "rows_hard_deleted": rows_hard_deleted,
        "rows_crypto_shredded": rows_crypto_shredded,
    }
    try:
        if audit_id is not None:
            await db.table("deletion_audit_log").update(audit_update_payload).eq("id", audit_id).execute()
        else:
            await db.table("deletion_audit_log").update(audit_update_payload).eq("user_id", user_id).execute()
    except Exception as audit_upd_err:
        logger.warning("Failed to update deletion_audit_log to complete for user %s: %s", user_id, audit_upd_err)

    # 8. Background tasks: Backup CSV scrubber, Storage scrubber, and R2 backup scrubber
    background_tasks.add_task(
        scrub_user_from_all_backups,
        user_id=user_id,
        hashed_device_ids=hashed_device_ids,
    )
    background_tasks.add_task(
        scrub_pending_storage_deletions,
        db=db,
    )
    background_tasks.add_task(
        purge_user_r2_backups,
        user_id=user_id,
    )

    logger.info("User account permanently deleted and data cryptographically shredded for user_id=%s", user_id)
    return {
        "rows_hard_deleted": rows_hard_deleted,
        "rows_crypto_shredded": rows_crypto_shredded,
    }
