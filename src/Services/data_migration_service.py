from supabase import AsyncClient
from src.Infrastructure.logger import get_logger

logger = get_logger("Services.data_migration_service")


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
        """Insert guest data into Supabase tables linked to user_id."""
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
            rows = []
            for item in receipts:
                rcpt_data = item.get("receipt")
                if isinstance(rcpt_data, dict):
                    if rcpt_data.get("raw_text") is None:
                        rcpt_data["raw_text"] = ""
                    if not rcpt_data.get("merchant_name"):
                        rcpt_data["merchant_name"] = "Unknown Merchant"

                row: dict = {
                    "user_id": user_id,
                    "device_id": device_name,
                    "receipt": rcpt_data,
                }
                # Omit local 'id' so PostgreSQL DEFAULT gen_random_uuid() generates a valid UUID
                if item.get("created_at"):
                    row["created_at"] = item["created_at"]
                rows.append(row)
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
            rows = []
            orig_ids = []
            for item in conversations:
                orig_ids.append(item.get("id"))
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
                    continue  # Skip messages whose parent conversation wasn't migrated

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

        logger.info(
            "DataMigrationService: completed migration for user_id=%s device_name=%s "
            "— receipts=%d, conversations=%d, chat_messages=%d",
            user_id,
            device_name,
            migrated["receipts"],
            migrated["conversations"],
            migrated["chat_messages"],
        )
        return migrated

