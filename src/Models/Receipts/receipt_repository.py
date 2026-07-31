from datetime import datetime, timezone
from supabase import AsyncClient
from src.Models.schemas import Receipt


class ReceiptRepository:
    TABLE = "receipts"

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── READ ──────────────────────────────────────────────────────────────────

    async def get_all_by_user(self, user_id: str) -> list[dict]:
        """Return all non-deleted receipts owned by user_id, newest first."""
        response = await (
            self.db.table(self.TABLE)
            .select("*")
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .execute()
        )
        return response.data

    async def get_by_id(self, receipt_id: str, user_id: str) -> dict | None:
        """Return a single non-deleted receipt, only if owned by user_id."""
        response = await (
            self.db.table(self.TABLE)
            .select("*")
            .eq("id", receipt_id)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return response.data if response else None

    # ── CREATE ────────────────────────────────────────────────────────────────

    async def create(self, user_id: str, device_id: str, receipt: Receipt) -> dict:
        """Insert a single receipt row and return the created record."""
        row = {
            "user_id": user_id,
            "device_id": device_id,
            "receipt": receipt.model_dump(mode="json"),
        }
        response = await self.db.table(self.TABLE).insert(row).execute()
        return response.data[0]

    async def create_batch(
        self, user_id: str, device_id: str, receipts: list[Receipt]
    ) -> list[dict]:
        """Insert up to 100 receipt rows in a single Supabase call."""
        rows = [
            {
                "user_id": user_id,
                "device_id": device_id,
                "receipt": r.model_dump(mode="json"),
            }
            for r in receipts
        ]
        response = await self.db.table(self.TABLE).insert(rows).execute()
        return response.data

    # ── DELETE (SOFT) ─────────────────────────────────────────────────────────

    async def soft_delete(self, receipt_id: str, user_id: str) -> bool:
        """Set deleted_at to now(). Returns True if a row was affected.

        Double-deletes are safely rejected because the query also filters
        deleted_at IS NULL — a row already soft-deleted won't match.
        """
        now = datetime.now(timezone.utc).isoformat()
        response = await (
            self.db.table(self.TABLE)
            .update({"deleted_at": now})
            .eq("id", receipt_id)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .execute()
        )
        return len(response.data) > 0
