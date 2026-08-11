from datetime import datetime, timezone
from supabase import AsyncClient
from src.Models.schemas import Receipt
from src.Auth.identity import Identity


class ReceiptRepository:
    TABLE = "receipts"

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── INTERNAL HELPERS ──────────────────────────────────────────────────────

    def _apply_identity_filter(self, query, identity: Identity):
        """Apply ownership filter based on caller identity.

        Authenticated users: filter by user_id.
        Guest devices: filter by device_id where user_id IS NULL.
        """
        if identity.is_authenticated:
            return query.eq("user_id", identity.user_id)
        return query.eq("device_id", identity.device_id).is_("user_id", "null")

    # ── READS ─────────────────────────────────────────────────────────────────

    async def get_all_by_identity(
        self,
        identity: Identity,
        updated_after: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        """Return all non-deleted receipts owned by the caller's identity.

        Supports optional delta sync via `updated_after` (ISO 8601 timestamp),
        and pagination via `limit` and `offset`.

        Results are ordered by updated_at DESC so the most recently modified
        receipts are returned first — this ensures edits are captured by clients
        using delta-sync patterns.
        """
        query = (
            self.db.table(self.TABLE)
            .select("*")
            .is_("deleted_at", "null")
        )
        query = self._apply_identity_filter(query, identity)

        if updated_after:
            # Filter receipts modified after the given timestamp (delta sync)
            query = query.gt("updated_at", updated_after.strip())

        query = query.order("updated_at", desc=True)

        if limit is not None:
            start = offset or 0
            query = query.range(start, start + limit - 1)

        response = await query.execute()
        return response.data if response else []

    async def get_by_id(self, receipt_id: str, identity: Identity) -> dict | None:
        """Return a single non-deleted receipt, only if owned by the caller's identity."""
        query = (
            self.db.table(self.TABLE)
            .select("*")
            .eq("id", receipt_id)
            .is_("deleted_at", "null")
        )
        query = self._apply_identity_filter(query, identity)
        response = await query.maybe_single().execute()
        return response.data if response else None

    # ── WRITES ────────────────────────────────────────────────────────────────

    async def create(self, identity: Identity, receipt: Receipt) -> dict:
        """Insert a single receipt row bound to the caller's identity."""
        row = {
            "user_id": identity.user_id,
            "device_id": identity.device_id,
            "receipt": receipt.model_dump(mode="json"),
        }
        response = await self.db.table(self.TABLE).insert(row).execute()
        return response.data[0]

    async def create_batch(self, identity: Identity, receipts: list[Receipt]) -> list[dict]:
        """Insert up to 100 receipt rows in a single Supabase call, bound to caller's identity."""
        rows = [
            {
                "user_id": identity.user_id,
                "device_id": identity.device_id,
                "receipt": r.model_dump(mode="json"),
            }
            for r in receipts
        ]
        response = await self.db.table(self.TABLE).insert(rows).execute()
        return response.data if response else []

    # ── SOFT DELETE ───────────────────────────────────────────────────────────

    async def soft_delete(self, receipt_id: str, identity: Identity) -> bool:
        """Set deleted_at to now(). Returns True if a row was affected.

        Double-deletes are safely rejected — the query also filters deleted_at IS NULL,
        so an already soft-deleted row won't match.
        """
        now = datetime.now(timezone.utc).isoformat()
        query = (
            self.db.table(self.TABLE)
            .update({"deleted_at": now})
            .eq("id", receipt_id)
            .is_("deleted_at", "null")
        )
        query = self._apply_identity_filter(query, identity)
        response = await query.execute()
        return len(response.data) > 0 if response and response.data else False
