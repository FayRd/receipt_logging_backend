import time
from datetime import datetime, timezone
from supabase import AsyncClient
from src.Infrastructure.logger import get_logger
from src.Auth.identity import Identity

logger = get_logger("Models.conversation_repository")


class ConversationRepository:
    CONVERSATIONS_TABLE = "conversations"
    MESSAGES_TABLE = "chat_messages"
    MAX_CONVERSATIONS_PER_IDENTITY = 10

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── IDENTITY FILTER ───────────────────────────────────────────────────────

    def _apply_identity_filter(self, query, identity: Identity):
        """Scope query to user_id if authenticated, else device_id with user_id IS NULL."""
        if identity.is_authenticated:
            return query.eq("user_id", identity.user_id)
        return query.eq("device_id", identity.device_id).is_("user_id", "null")

    # ── CONVERSATIONS ─────────────────────────────────────────────────────────

    async def count_conversations(self, identity: Identity) -> int:
        """Count active non-deleted conversations owned by this session identity."""
        start_time = time.perf_counter()
        logger.debug(
            "SELECT COUNT conversations: user_id=%s, device_id=%s",
            identity.user_id,
            identity.device_id,
        )
        try:
            q = (
                self.db.table(self.CONVERSATIONS_TABLE)
                .select("id", count="exact")
                .is_("deleted_at", "null")
            )
            q = self._apply_identity_filter(q, identity)
            res = await q.execute()
            count = res.count if res and res.count is not None else 0
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT COUNT conversations succeeded: count=%d in %.2fms", count, duration_ms)
            return count
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT COUNT conversations after %.2fms: %s", duration_ms, e, exc_info=True)
            raise

    async def get_conversation(self, conversation_id: str, identity: Identity) -> dict | None:
        """Fetch a conversation only if it is owned by the caller's session identity."""
        start_time = time.perf_counter()
        logger.debug(
            "SELECT conversation get_conversation: conversation_id=%s, user_id=%s, device_id=%s",
            conversation_id,
            identity.user_id,
            identity.device_id,
        )
        try:
            q = (
                self.db.table(self.CONVERSATIONS_TABLE)
                .select("*")
                .eq("id", conversation_id)
                .is_("deleted_at", "null")
            )
            q = self._apply_identity_filter(q, identity)
            res = await q.maybe_single().execute()
            result = res.data if res else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "SELECT conversation get_conversation finished: found=%s in %.2fms",
                result is not None,
                duration_ms,
            )
            return result
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in SELECT conversation conversation_id=%s after %.2fms: %s",
                conversation_id,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    async def create_conversation(self, identity: Identity, title: str | None = None) -> dict:
        """Create a new conversation record bound to the caller's session identity."""
        start_time = time.perf_counter()
        clean_title = title.strip() if title and title.strip() else "New Conversation"
        logger.debug(
            "INSERT conversation create_conversation: title='%s', user_id=%s, device_id=%s",
            clean_title,
            identity.user_id,
            identity.device_id,
        )
        try:
            row = {
                "user_id": identity.user_id,
                "device_id": identity.device_id,
                "title": clean_title,
            }
            res = await self.db.table(self.CONVERSATIONS_TABLE).insert(row).execute()
            created_row = res.data[0]
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "INSERT conversation create_conversation succeeded: id=%s in %.2fms",
                created_row.get("id"),
                duration_ms,
            )
            return created_row
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in INSERT conversation after %.2fms: %s", duration_ms, e, exc_info=True)
            raise

    async def list_conversations(
        self, identity: Identity, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """List conversations owned by caller identity, ordered by updated_at desc."""
        start_time = time.perf_counter()
        logger.debug(
            "SELECT conversations list_conversations: user_id=%s, device_id=%s, limit=%d, offset=%d",
            identity.user_id,
            identity.device_id,
            limit,
            offset,
        )
        try:
            q = (
                self.db.table(self.CONVERSATIONS_TABLE)
                .select("*")
                .is_("deleted_at", "null")
            )
            q = self._apply_identity_filter(q, identity)
            res = await (
                q.order("updated_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            rows = res.data if res else []
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "SELECT conversations list_conversations succeeded: returned %d rows in %.2fms",
                len(rows),
                duration_ms,
            )
            return rows
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in SELECT conversations list_conversations after %.2fms: %s",
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    # ── CHAT MESSAGES ─────────────────────────────────────────────────────────

    async def get_messages(
        self, conversation_id: str, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        """Fetch paginated messages for a conversation ordered chronologically (asc)."""
        start_time = time.perf_counter()
        logger.debug(
            "SELECT chat_messages get_messages: conversation_id=%s, limit=%d, offset=%d",
            conversation_id,
            limit,
            offset,
        )
        try:
            count_res = await (
                self.db.table(self.MESSAGES_TABLE)
                .select("id", count="exact")
                .eq("conversation_id", conversation_id)
                .execute()
            )
            total_count = count_res.count if count_res and count_res.count is not None else 0

            res = await (
                self.db.table(self.MESSAGES_TABLE)
                .select("*")
                .eq("conversation_id", conversation_id)
                .order("created_at", desc=False)
                .range(offset, offset + limit - 1)
                .execute()
            )
            rows = res.data if res else []
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "SELECT chat_messages get_messages succeeded: returned %d/%d rows in %.2fms",
                len(rows),
                total_count,
                duration_ms,
            )
            return rows, total_count
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in SELECT chat_messages get_messages conversation_id=%s after %.2fms: %s",
                conversation_id,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    async def add_message(self, conversation_id: str, sender: str, content: str) -> dict:
        """Insert a user or assistant message into the conversation."""
        start_time = time.perf_counter()
        logger.debug(
            "INSERT chat_messages add_message: conversation_id=%s, sender=%s, content_len=%d",
            conversation_id,
            sender,
            len(content),
        )
        try:
            row = {
                "conversation_id": conversation_id,
                "sender": sender,
                "content": content,
            }
            res = await self.db.table(self.MESSAGES_TABLE).insert(row).execute()
            created_row = res.data[0]
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "INSERT chat_messages add_message succeeded: id=%s in %.2fms",
                created_row.get("id"),
                duration_ms,
            )
            return created_row
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in INSERT chat_messages add_message conversation_id=%s after %.2fms: %s",
                conversation_id,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    # ── SOFT DELETE ───────────────────────────────────────────────────────────

    async def soft_delete(self, conversation_id: str, identity: Identity) -> bool:
        """Soft-delete a conversation owned by identity. Returns True if row affected."""
        start_time = time.perf_counter()
        logger.debug(
            "UPDATE (soft_delete) conversation: conversation_id=%s, user_id=%s, device_id=%s",
            conversation_id,
            identity.user_id,
            identity.device_id,
        )
        try:
            now = datetime.now(timezone.utc).isoformat()
            q = (
                self.db.table(self.CONVERSATIONS_TABLE)
                .update({"deleted_at": now})
                .eq("id", conversation_id)
                .is_("deleted_at", "null")
            )
            q = self._apply_identity_filter(q, identity)
            res = await q.execute()
            affected = len(res.data) > 0 if res and res.data else False
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "UPDATE (soft_delete) conversation finished: conversation_id=%s, affected=%s in %.2fms",
                conversation_id,
                affected,
                duration_ms,
            )
            return affected
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in UPDATE (soft_delete) conversation conversation_id=%s after %.2fms: %s",
                conversation_id,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

