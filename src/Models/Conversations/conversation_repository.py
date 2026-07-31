from supabase import AsyncClient
from src.Auth.identity import Identity


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
        q = (
            self.db.table(self.CONVERSATIONS_TABLE)
            .select("id", count="exact")
            .is_("deleted_at", "null")
        )
        q = self._apply_identity_filter(q, identity)
        res = await q.execute()
        return res.count if res and res.count is not None else 0

    async def get_conversation(self, conversation_id: str, identity: Identity) -> dict | None:
        """Fetch a conversation only if it is owned by the caller's session identity."""
        q = (
            self.db.table(self.CONVERSATIONS_TABLE)
            .select("*")
            .eq("id", conversation_id)
            .is_("deleted_at", "null")
        )
        q = self._apply_identity_filter(q, identity)
        res = await q.maybe_single().execute()
        return res.data if res else None

    async def create_conversation(self, identity: Identity, title: str | None = None) -> dict:
        """Create a new conversation record bound to the caller's session identity."""
        row = {
            "user_id": identity.user_id,
            "device_id": identity.device_id,
            "title": title.strip() if title and title.strip() else "New Conversation",
        }
        res = await self.db.table(self.CONVERSATIONS_TABLE).insert(row).execute()
        return res.data[0]

    async def list_conversations(
        self, identity: Identity, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """List conversations owned by caller identity, ordered by updated_at desc."""
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
        return res.data if res else []

    # ── CHAT MESSAGES ─────────────────────────────────────────────────────────

    async def get_messages(
        self, conversation_id: str, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        """Fetch paginated messages for a conversation ordered chronologically (asc)."""
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
        return (res.data if res else []), total_count

    async def add_message(self, conversation_id: str, sender: str, content: str) -> dict:
        """Insert a user or assistant message into the conversation.

        Note: conversations.updated_at is bumped automatically by a Postgres trigger
        (trigger: chat_messages_update_conversation) on INSERT to chat_messages.
        This avoids requiring UPDATE privilege on conversations for the anon role.
        """
        row = {
            "conversation_id": conversation_id,
            "sender": sender,
            "content": content,
        }
        res = await self.db.table(self.MESSAGES_TABLE).insert(row).execute()
        return res.data[0]
