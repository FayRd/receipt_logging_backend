import base64
import uuid
from supabase import AsyncClient
from src.Infrastructure.logger import get_logger
from src.Services.image_service import ImageStorageService

logger = get_logger("Services.data_migration_service")


def _is_valid_uuid(val: str | None) -> bool:
    if not val:
        return False
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False


class DataMigrationService:
    """Bulk-migrates guest local data (receipts, conversations, chat_messages) into Supabase
    upon user sign-up / device link, associating all records with the new user_id and device_name.
    """

    @staticmethod
    async def migrate_user_data(
        db: AsyncClient,
        user_id: str,
        device_name: str,
        migrate_data: dict,
    ) -> dict:
        """Insert guest data into Supabase tables linked to user_id (skipping existing UUIDs)."""
        receipts: list[dict] = migrate_data.get("receipts", []) or []
        conversations: list[dict] = migrate_data.get("conversations", []) or []
        chat_messages: list[dict] = migrate_data.get("chat_messages", []) or []

        logger.debug(
            "migrate_user_data called: user_id=%s, device_name=%s, input_receipts=%d, input_conversations=%d, input_messages=%d",
            user_id,
            device_name,
            len(receipts),
            len(conversations),
            len(chat_messages),
        )

        migrated: dict = {"receipts": 0, "conversations": 0, "chat_messages": 0}

        # ── 1. Receipts ────────────────────────────────────────────────────────
        if receipts:
            # Query existing receipt UUIDs owned by this user to prevent duplicate re-insertion
            existing_ids: set[str] = set()
            try:
                res_existing = await db.table("receipts").select("id").eq("user_id", user_id).execute()
                if res_existing and res_existing.data:
                    existing_ids = {r["id"] for r in res_existing.data if "id" in r}
            except Exception as exc:
                logger.warning("Could not query existing receipts for de-duplication: %s", exc)

            storage_service = ImageStorageService(db)
            rows = []
            for item in receipts:
                item_id = item.get("id")
                if _is_valid_uuid(item_id) and item_id in existing_ids:
                    logger.debug("Skipping export of already existing receipt UUID=%s", item_id)
                    continue

                rcpt_data = item.get("receipt")
                if isinstance(rcpt_data, dict):
                    if rcpt_data.get("raw_text") is None:
                        rcpt_data["raw_text"] = ""
                    if not rcpt_data.get("merchant_name"):
                        rcpt_data["merchant_name"] = "Unknown Merchant"

                receipt_uuid = str(uuid.uuid4())
                receipt_image_path = None

                # If the guest receipt has an attached base64 image, upload to Supabase Storage
                image_base64 = item.get("image_base64")
                if image_base64 and isinstance(image_base64, str):
                    try:
                        image_bytes = base64.b64decode(image_base64)
                        receipt_image_path = await storage_service.upload_receipt_image(
                            user_id=user_id,
                            receipt_id=receipt_uuid,
                            image_bytes=image_bytes,
                        )
                        logger.info(
                            "DataMigrationService: uploaded guest receipt image to storage for user_id=%s, receipt_id=%s → %s",
                            user_id,
                            receipt_uuid,
                            receipt_image_path,
                        )
                    except Exception as img_exc:
                        logger.warning(
                            "DataMigrationService: failed to upload receipt image during migration for user_id=%s: %s",
                            user_id,
                            img_exc,
                        )

                row: dict = {
                    "id": receipt_uuid,
                    "user_id": user_id,
                    "device_id": device_name,
                    "receipt": rcpt_data,
                    "receipt_image_path": receipt_image_path,
                }
                if item.get("created_at"):
                    row["created_at"] = item["created_at"]
                rows.append(row)

            if rows:
                try:
                    logger.debug("Executing receipts batch insert of %d rows for user_id=%s", len(rows), user_id)
                    res = await db.table("receipts").insert(rows).execute()
                    migrated["receipts"] = len(res.data) if res and res.data else len(rows)
                    logger.info("Migrated %d receipts for user_id=%s", migrated["receipts"], user_id)
                except Exception as exc:
                    logger.error("DataMigrationService: receipts insert failed for user_id=%s: %s", user_id, exc, exc_info=True)

        # ── 2. Conversations ───────────────────────────────────────────────────
        conv_id_map: dict[str, str] = {}
        if conversations:
            existing_conv_ids: set[str] = set()
            try:
                res_convs = await db.table("conversations").select("id").eq("user_id", user_id).execute()
                if res_convs and res_convs.data:
                    existing_conv_ids = {c["id"] for c in res_convs.data if "id" in c}
            except Exception as exc:
                logger.warning("Could not query existing conversations for de-duplication: %s", exc)

            rows = []
            orig_ids = []
            for item in conversations:
                item_id = item.get("id")
                if _is_valid_uuid(item_id) and item_id in existing_conv_ids:
                    logger.debug("Conversation UUID=%s already exists in Supabase, mapping 1:1", item_id)
                    conv_id_map[item_id] = item_id
                    continue

                orig_ids.append(item_id)
                row = {
                    "user_id": user_id,
                    "device_id": device_name,
                    "title": item.get("title", "Untitled Chat"),
                }
                if item.get("created_at"):
                    row["created_at"] = item["created_at"]
                if item.get("updated_at"):
                    row["updated_at"] = item["updated_at"]
                rows.append(row)

            if rows:
                try:
                    logger.debug("Executing conversations batch insert of %d rows for user_id=%s", len(rows), user_id)
                    res = await db.table("conversations").insert(rows).execute()
                    inserted_rows = res.data if res and res.data else []
                    migrated["conversations"] = len(inserted_rows)

                    # Map local conversation ID to newly generated Supabase UUID
                    for idx, created_row in enumerate(inserted_rows):
                        if idx < len(orig_ids) and orig_ids[idx]:
                            conv_id_map[orig_ids[idx]] = created_row["id"]
                    logger.info(
                        "Migrated %d conversations for user_id=%s (mapped %d conv IDs)",
                        migrated["conversations"],
                        user_id,
                        len(conv_id_map),
                    )
                except Exception as exc:
                    logger.error("DataMigrationService: conversations insert failed for user_id=%s: %s", user_id, exc, exc_info=True)

        # ── 3. Chat Messages ───────────────────────────────────────────────────
        if chat_messages and conv_id_map:
            rows = []
            for item in chat_messages:
                orig_conv_id = item.get("conversation_id")
                new_conv_id = conv_id_map.get(orig_conv_id)
                if not new_conv_id:
                    logger.debug("Skipping message with unmapped orig_conv_id=%s", orig_conv_id)
                    continue  # Skip messages whose parent conversation wasn't mapped

                row = {
                    "conversation_id": new_conv_id,
                    "sender": item.get("sender", "user"),
                    "content": item.get("content", ""),
                }
                if item.get("created_at"):
                    row["created_at"] = item["created_at"]
                rows.append(row)
            if rows:
                try:
                    logger.debug("Executing chat_messages batch insert of %d rows for user_id=%s", len(rows), user_id)
                    res = await db.table("chat_messages").insert(rows).execute()
                    migrated["chat_messages"] = len(res.data) if res and res.data else len(rows)
                    logger.info("Migrated %d chat_messages for user_id=%s", migrated["chat_messages"], user_id)
                except Exception as exc:
                    logger.error("DataMigrationService: chat_messages insert failed for user_id=%s: %s", user_id, exc, exc_info=True)
        elif chat_messages and not conv_id_map:
            logger.warning("chat_messages present (%d) but no conversations were mapped; skipping message migration", len(chat_messages))

        # ── 4. Custom Categories ──────────────────────────────────────────────
        custom_categories = migrate_data.get("custom_categories", []) or []
        if custom_categories:
            try:
                user_res = await db.table("users").select("custom_categories").eq("id", user_id).maybe_single().execute()
                current_cats = user_res.data.get("custom_categories") if user_res and user_res.data else None
                if not current_cats:
                    await db.table("users").update({"custom_categories": custom_categories}).eq("id", user_id).execute()
                    migrated["custom_categories"] = len(custom_categories)
                    logger.info("Migrated %d custom_categories for user_id=%s", len(custom_categories), user_id)
            except Exception as exc:
                logger.error("DataMigrationService: custom_categories update failed for user_id=%s: %s", user_id, exc, exc_info=True)

        logger.info(
            "DataMigrationService: completed migration for user_id=%s device_name=%s "
            "— receipts=%d, conversations=%d, chat_messages=%d, custom_categories=%d",
            user_id,
            device_name,
            migrated["receipts"],
            migrated["conversations"],
            migrated["chat_messages"],
            migrated.get("custom_categories", 0),
        )
        return migrated

