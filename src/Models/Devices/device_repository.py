import secrets
import time
import uuid
from datetime import datetime, timezone
from supabase import AsyncClient
from src.Infrastructure.logger import get_logger
from src.Auth.device_security import hash_device_token
from src.Models.schemas import DeviceRegisterRequest
from src.Models.Users.user_repository import UserRepository

logger = get_logger("Models.device_repository")


class DeviceRepository:
    TABLE = "devices"

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── READS ─────────────────────────────────────────────────────────────────

    async def get_by_device_id(self, device_name_or_uuid: str) -> dict | None:
        """Fetch active device record by hardware name string or table UUID id."""
        start_time = time.perf_counter()
        clean_name = device_name_or_uuid.strip()
        logger.debug("SELECT device get_by_device_id: device_name_or_uuid='%s'", clean_name)

        try:
            # 1. Search by devices.name column
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("name", clean_name)
                .is_("deleted_at", "null")
                .maybe_single()
                .execute()
            )
            if res and res.data:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "SELECT device get_by_device_id matched by name: id=%s in %.2fms",
                    res.data.get("id"),
                    duration_ms,
                )
                return res.data

            # 2. Fallback: Search by table UUID id if clean_name is a valid UUID
            try:
                uuid.UUID(clean_name)
                res_uuid = await (
                    self.db.table(self.TABLE)
                    .select("*")
                    .eq("id", clean_name)
                    .is_("deleted_at", "null")
                    .maybe_single()
                    .execute()
                )
                result = res_uuid.data if res_uuid else None
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    "SELECT device get_by_device_id fallback UUID search finished: found=%s in %.2fms",
                    result is not None,
                    duration_ms,
                )
                return result
            except ValueError:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.debug("Device name '%s' is not a valid UUID fallback; not found (%.2fms)", clean_name, duration_ms)
                return None
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Database error in SELECT device get_by_device_id '%s' after %.2fms: %s",
                clean_name,
                duration_ms,
                e,
                exc_info=True,
            )
            raise

    async def get_by_user_id(self, user_id: str) -> list[dict]:
        """Fetch all active devices linked to a specific user_id."""
        start_time = time.perf_counter()
        logger.debug("SELECT devices get_by_user_id: user_id=%s", user_id)
        try:
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("user_id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            rows = res.data if res else []
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT devices get_by_user_id succeeded: returned %d rows in %.2fms", len(rows), duration_ms)
            return rows
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT devices get_by_user_id user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    # ── WRITES ────────────────────────────────────────────────────────────────

    async def register_or_update(self, req: DeviceRegisterRequest) -> dict:
        """Register new device or update token_hash/user_id if device already exists."""
        start_time = time.perf_counter()
        clean_name = req.device_name.strip()
        incoming_hash = hash_device_token(req.device_token)
        logger.debug("register_or_update device: device_name='%s', username=%s", clean_name, req.username)

        try:
            # Resolve optional username to user_id
            resolved_user_id = None
            if req.username:
                user_repo = UserRepository(self.db)
                user_row = await user_repo.get_by_identifier(req.username)
                if user_row:
                    resolved_user_id = user_row["id"]

            existing = await self.get_by_device_id(clean_name)
            if existing:
                stored_hash = existing.get("device_token_hash", "")
                if not secrets.compare_digest(incoming_hash.encode("utf-8"), stored_hash.encode("utf-8")):
                    logger.warning("Device token mismatch during re-registration for device_name='%s'", clean_name)
                    return None

                updates: dict = {"device_token_hash": incoming_hash}
                if resolved_user_id is not None:
                    updates["user_id"] = resolved_user_id

                res = await (
                    self.db.table(self.TABLE)
                    .update(updates)
                    .eq("id", existing["id"])
                    .execute()
                )
                data = res.data[0]
                data["username"] = req.username if resolved_user_id else None
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info("UPDATE device register_or_update succeeded: id=%s in %.2fms", existing["id"], duration_ms)
                return data

            # Fresh device registration
            row = {
                "name": clean_name,
                "device_token_hash": incoming_hash,
                "user_id": resolved_user_id,
            }
            res = await self.db.table(self.TABLE).insert(row).execute()
            data = res.data[0]
            data["username"] = req.username if resolved_user_id else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("INSERT device register_or_update succeeded: id=%s in %.2fms", data.get("id"), duration_ms)
            return data
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in register_or_update device_name='%s' after %.2fms: %s", clean_name, duration_ms, e, exc_info=True)
            raise

    async def link_user_by_names(
        self, device_name: str, username: str | None
    ) -> dict | None:
        """Link or unlink a device to a user account by device_name and username."""
        start_time = time.perf_counter()
        logger.debug("link_user_by_names: device_name='%s', username=%s", device_name, username)

        try:
            existing = await self.get_by_device_id(device_name)
            if not existing:
                logger.warning("Device name '%s' not found for linking", device_name)
                return None

            canonical_name = existing["name"]

            # Resolve target username to user_id
            resolved_user_id = None
            if username:
                user_repo = UserRepository(self.db)
                user_row = await user_repo.get_by_identifier(username)
                if not user_row:
                    logger.warning("Target username '%s' not found for linking to device '%s'", username, device_name)
                    return None
                resolved_user_id = user_row["id"]

            # When linking to a user account, invoke atomic RPC for guest data migration
            if resolved_user_id:
                try:
                    logger.debug("Calling RPC link_device_and_migrate_guest_data for device='%s'", canonical_name)
                    rpc_res = await self.db.rpc(
                        "link_device_and_migrate_guest_data",
                        {
                            "p_device_name": canonical_name,
                            "p_device_token_hash": existing.get("device_token_hash", ""),
                            "p_user_id": resolved_user_id,
                        },
                    ).execute()
                    if rpc_res and rpc_res.data:
                        migrated = rpc_res.data
                        logger.info(
                            "Atomic guest migration RPC complete for device name=%s: "
                            "receipts=%s conversations=%s",
                            canonical_name,
                            migrated.get("migrated_receipts", 0),
                            migrated.get("migrated_conversations", 0),
                        )
                        updated = await self.get_by_device_id(existing["id"])
                        if updated:
                            updated["username"] = username
                        duration_ms = (time.perf_counter() - start_time) * 1000
                        logger.info("link_user_by_names RPC succeeded in %.2fms", duration_ms)
                        return updated
                except Exception as exc:
                    logger.warning(
                        "RPC link_device_and_migrate_guest_data failed for device name=%s: %s (falling back to sequential update)",
                        canonical_name,
                        exc,
                    )

            # Sequential update fallback
            res = await (
                self.db.table(self.TABLE)
                .update({"user_id": resolved_user_id})
                .eq("id", existing["id"])
                .execute()
            )
            updated_device = res.data[0] if res else None

            if resolved_user_id and updated_device:
                await (
                    self.db.table("receipts")
                    .update({"user_id": resolved_user_id})
                    .eq("device_id", canonical_name)
                    .is_("user_id", "null")
                    .execute()
                )
                await (
                    self.db.table("conversations")
                    .update({"user_id": resolved_user_id})
                    .eq("device_id", canonical_name)
                    .is_("user_id", "null")
                    .execute()
                )

            if updated_device:
                updated_device["username"] = username if resolved_user_id else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("link_user_by_names completed fallback update in %.2fms", duration_ms)
            return updated_device
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in link_user_by_names device_name='%s' after %.2fms: %s", device_name, duration_ms, e, exc_info=True)
            raise

    async def link_user(
        self, device_name: str, device_token: str, username: str | None
    ) -> dict | None:
        """Backward compatible wrapper around link_user_by_names."""
        return await self.link_user_by_names(device_name, username)

    # ── SOFT DELETE ───────────────────────────────────────────────────────────

    async def soft_delete(self, device_name_or_uuid: str) -> bool:
        """Soft-delete device record by setting deleted_at to now(). Returns True if affected."""
        start_time = time.perf_counter()
        logger.debug("UPDATE (soft_delete) device: device_name_or_uuid='%s'", device_name_or_uuid)
        try:
            existing = await self.get_by_device_id(device_name_or_uuid)
            if not existing:
                logger.warning("Device '%s' not found for soft_delete", device_name_or_uuid)
                return False

            now = datetime.now(timezone.utc).isoformat()
            res = await (
                self.db.table(self.TABLE)
                .update({"deleted_at": now})
                .eq("id", existing["id"])
                .is_("deleted_at", "null")
                .execute()
            )
            affected = len(res.data) > 0 if res and res.data else False
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("UPDATE (soft_delete) device finished: affected=%s in %.2fms", affected, duration_ms)
            return affected
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in UPDATE (soft_delete) device after %.2fms: %s", duration_ms, e, exc_info=True)
            raise

    async def rotate_device_token(self, device_name: str, new_token: str) -> dict | None:
        """Update stored device_token_hash for an authenticated device."""
        start_time = time.perf_counter()
        clean_name = device_name.strip()
        logger.debug("UPDATE device rotate_device_token: device_name='%s'", clean_name)
        try:
            new_hash = hash_device_token(new_token)
            existing = await self.get_by_device_id(clean_name)
            if not existing:
                logger.warning("Device '%s' not found for token rotation", clean_name)
                return None

            res = await (
                self.db.table(self.TABLE)
                .update({"device_token_hash": new_hash})
                .eq("id", existing["id"])
                .execute()
            )
            data = res.data[0] if res and res.data else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("UPDATE device rotate_device_token succeeded in %.2fms", duration_ms)
            return data
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in rotate_device_token for device '%s' after %.2fms: %s", clean_name, duration_ms, e, exc_info=True)
            raise

