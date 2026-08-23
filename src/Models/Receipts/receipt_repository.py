import time
from datetime import datetime, timezone
from supabase import AsyncClient
from src.Infrastructure.logger import get_logger
from src.Models.schemas import Receipt
from src.Auth.identity import Identity

logger = get_logger("Models.receipt_repository")


class ReceiptRepository:
    TABLE = "receipts"

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── INTERNAL HELPERS ──────────────────────────────────────────────────────

    def _apply_identity_filter(self, query, identity: Identity):
        """Apply ownership filter based on caller identity."""
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
        """Return all non-deleted receipts owned by the caller's identity."""
        start_time = time.perf_counter()
        logger.debug(
            "SELECT receipts get_all_by_identity: user_id=%s, device_id=%s, updated_after=%s, limit=%s, offset=%s",
            identity.user_id,
            identity.device_id,
            updated_after,
            limit,
            offset,
        )
        try:
            query = (
                self.db.table(self.TABLE)
                .select("*")
                .is_("deleted_at", "null")
            )
            query = self._apply_identity_filter(query, identity)

            if updated_after:
                query = query.gt("updated_at", updated_after.strip())

            query = query.order("updated_at", desc=True)

            if limit is not None:
                start = offset or 0
                query = query.range(start, start + limit - 1)

            response = await query.execute()
            rows = response.data if response else []
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "SELECT receipts get_all_by_identity succeeded: returned %d rows in %.2fms",
                len(rows),
                duration_ms,
            )
            return rows
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in SELECT receipts get_all_by_identity after %.2fms: %s",
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    async def get_by_id(self, receipt_id: str, identity: Identity) -> dict | None:
        """Return a single non-deleted receipt, only if owned by the caller's identity."""
        start_time = time.perf_counter()
        logger.debug(
            "SELECT receipt get_by_id: receipt_id=%s, user_id=%s, device_id=%s",
            receipt_id,
            identity.user_id,
            identity.device_id,
        )
        try:
            query = (
                self.db.table(self.TABLE)
                .select("*")
                .eq("id", receipt_id)
                .is_("deleted_at", "null")
            )
            query = self._apply_identity_filter(query, identity)
            response = await query.maybe_single().execute()
            result = response.data if response else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "SELECT receipt get_by_id finished: found=%s in %.2fms",
                result is not None,
                duration_ms,
            )
            return result
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in SELECT receipt get_by_id receipt_id=%s after %.2fms: %s",
                receipt_id,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    # ── WRITES ────────────────────────────────────────────────────────────────

    async def create(
        self,
        identity: Identity,
        receipt: Receipt,
        receipt_image_path: str | None = None,
        receipt_id: str | None = None,
    ) -> dict:
        """Insert a single receipt row bound to the caller's identity.

        Optionally accepts a pre-generated `receipt_id` (UUID string) so that the
        image can be uploaded before the DB insert using a known receipt ID.
        `receipt_image_path` is stored alongside the receipt JSONB column.
        """
        import uuid as _uuid

        start_time = time.perf_counter()
        logger.debug(
            "INSERT receipt create: user_id=%s, device_id=%s, merchant=%s",
            identity.user_id,
            identity.device_id,
            receipt.merchant_name,
        )
        try:
            row: dict = {
                "user_id": identity.user_id,
                "device_id": identity.device_id,
                "receipt": receipt.model_dump(mode="json"),
            }
            if receipt_id:
                row["id"] = receipt_id
            if receipt_image_path is not None:
                row["receipt_image_path"] = receipt_image_path

            response = await self.db.table(self.TABLE).insert(row).execute()
            created_row = response.data[0]
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "INSERT receipt create succeeded: id=%s in %.2fms",
                created_row.get("id"),
                duration_ms,
            )
            return created_row
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in INSERT receipt create after %.2fms: %s",
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    async def create_batch(
        self,
        identity: Identity,
        receipts: list[Receipt],
        receipt_image_paths: list[str | None] | None = None,
        receipt_ids: list[str | None] | None = None,
    ) -> list[dict]:
        """Insert up to 100 receipt rows in a single Supabase call, bound to caller's identity.

        `receipt_image_paths` is an optional parallel list (same length as `receipts`)
        containing the Supabase Storage path for each receipt image or None if not uploaded.
        `receipt_ids` is an optional parallel list of pre-generated UUID strings.
        """
        start_time = time.perf_counter()
        logger.debug(
            "INSERT receipts create_batch: count=%d, user_id=%s, device_id=%s",
            len(receipts),
            identity.user_id,
            identity.device_id,
        )
        try:
            rows = []
            for i, r in enumerate(receipts):
                row: dict = {
                    "user_id": identity.user_id,
                    "device_id": identity.device_id,
                    "receipt": r.model_dump(mode="json"),
                }
                if receipt_ids and i < len(receipt_ids) and receipt_ids[i]:
                    row["id"] = receipt_ids[i]
                if receipt_image_paths and i < len(receipt_image_paths) and receipt_image_paths[i]:
                    row["receipt_image_path"] = receipt_image_paths[i]
                rows.append(row)

            response = await self.db.table(self.TABLE).insert(rows).execute()
            inserted_rows = response.data if response else []
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "INSERT receipts create_batch succeeded: inserted %d rows in %.2fms",
                len(inserted_rows),
                duration_ms,
            )
            return inserted_rows
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in INSERT receipts create_batch after %.2fms: %s",
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    # ── UPDATE ────────────────────────────────────────────────────────────────

    async def update(
        self,
        receipt_id: str,
        identity: Identity,
        receipt: Receipt | None = None,
        receipt_image_path: str | None = None,
    ) -> dict | None:
        """Update the receipt payload and/or receipt_image_path for a row owned by the caller.

        Both `receipt` and `receipt_image_path` are optional — at least one must be provided.
        Returns the updated row dict or None if no row was found/matched.
        """
        start_time = time.perf_counter()
        logger.debug(
            "UPDATE receipt: receipt_id=%s, user_id=%s, device_id=%s",
            receipt_id,
            identity.user_id,
            identity.device_id,
        )
        try:
            now = datetime.now(timezone.utc).isoformat()
            updates: dict = {"updated_at": now}
            if receipt is not None:
                updates["receipt"] = receipt.model_dump(mode="json")
                logger.debug("UPDATE receipt: updating receipt JSON for receipt_id=%s", receipt_id)
            if receipt_image_path is not None:
                updates["receipt_image_path"] = receipt_image_path
                logger.debug("UPDATE receipt: updating receipt_image_path=%s", receipt_image_path)

            query = (
                self.db.table(self.TABLE)
                .update(updates)
                .eq("id", receipt_id)
                .is_("deleted_at", "null")
            )
            query = self._apply_identity_filter(query, identity)
            response = await query.execute()
            updated_row = response.data[0] if (response and response.data) else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "UPDATE receipt finished: receipt_id=%s, found=%s in %.2fms",
                receipt_id,
                updated_row is not None,
                duration_ms,
            )
            return updated_row
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in UPDATE receipt receipt_id=%s after %.2fms: %s",
                receipt_id,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    # ── SOFT DELETE ───────────────────────────────────────────────────────────

    async def soft_delete(self, receipt_id: str, identity: Identity) -> bool:
        """Set deleted_at to now(). Returns True if a row was affected."""
        start_time = time.perf_counter()
        logger.debug(
            "UPDATE (soft_delete) receipt: receipt_id=%s, user_id=%s, device_id=%s",
            receipt_id,
            identity.user_id,
            identity.device_id,
        )
        try:
            now = datetime.now(timezone.utc).isoformat()
            query = (
                self.db.table(self.TABLE)
                .update({"deleted_at": now})
                .eq("id", receipt_id)
                .is_("deleted_at", "null")
            )
            query = self._apply_identity_filter(query, identity)
            response = await query.execute()
            affected = len(response.data) > 0 if response and response.data else False
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "UPDATE (soft_delete) receipt finished: receipt_id=%s, affected=%s in %.2fms",
                receipt_id,
                affected,
                duration_ms,
            )
            return affected
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in UPDATE (soft_delete) receipt_id=%s after %.2fms: %s",
                receipt_id,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

