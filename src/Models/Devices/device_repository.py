import logging
import secrets
import uuid
from supabase import AsyncClient
from src.Auth.device_security import hash_device_token
from src.Models.schemas import DeviceRegisterRequest
from src.Models.Users.user_repository import UserRepository

logger = logging.getLogger(__name__)


class DeviceRepository:
    TABLE = "devices"

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── READS ─────────────────────────────────────────────────────────────────

    async def get_by_device_id(self, device_name_or_uuid: str) -> dict | None:
        """Fetch active device record by hardware name string or table UUID id."""
        clean_name = device_name_or_uuid.strip()

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
            return res_uuid.data if res_uuid else None
        except ValueError:
            return None

    async def get_by_user_id(self, user_id: str) -> list[dict]:
        """Fetch all active devices linked to a specific user_id."""
        res = await (
            self.db.table(self.TABLE)
            .select("*")
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .execute()
        )
        return res.data if res else []

    # ── WRITES ────────────────────────────────────────────────────────────────

    async def register_or_update(self, req: DeviceRegisterRequest) -> dict:
        """Register new device or update token_hash/user_id if device already exists."""
        clean_name = req.device_name.strip()
        incoming_hash = hash_device_token(req.device_token)

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
        return data

    async def link_user_by_names(
        self, device_name: str, username: str | None
    ) -> dict | None:
        """Link or unlink a device to a user account by device_name and username.

        Pre-requisite: Identity headers have already been verified by require_link_bridge_identity dependency.
        """
        existing = await self.get_by_device_id(device_name)
        if not existing:
            return None

        canonical_name = existing["name"]

        # Resolve target username to user_id
        resolved_user_id = None
        if username:
            user_repo = UserRepository(self.db)
            user_row = await user_repo.get_by_identifier(username)
            if not user_row:
                return None
            resolved_user_id = user_row["id"]

        # When linking to a user account, invoke atomic RPC for guest data migration
        if resolved_user_id:
            try:
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
                        "Atomic guest migration complete for device name=%s: "
                        "receipts=%s conversations=%s",
                        canonical_name,
                        migrated.get("migrated_receipts", 0),
                        migrated.get("migrated_conversations", 0),
                    )
                    updated = await self.get_by_device_id(existing["id"])
                    if updated:
                        updated["username"] = username
                    return updated
            except Exception as exc:
                logger.warning(
                    "RPC link_device_and_migrate_guest_data failed for device name=%s: %s",
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
        return updated_device

    async def link_user(
        self, device_name: str, device_token: str, username: str | None
    ) -> dict | None:
        """Backward compatible wrapper around link_user_by_names."""
        return await self.link_user_by_names(device_name, username)

    # ── SOFT DELETE ───────────────────────────────────────────────────────────

    async def soft_delete(self, device_name_or_uuid: str) -> bool:
        """Soft-delete device record by setting deleted_at to now(). Returns True if affected."""
        from datetime import datetime, timezone

        existing = await self.get_by_device_id(device_name_or_uuid)
        if not existing:
            return False

        now = datetime.now(timezone.utc).isoformat()
        res = await (
            self.db.table(self.TABLE)
            .update({"deleted_at": now})
            .eq("id", existing["id"])
            .is_("deleted_at", "null")
            .execute()
        )
        return len(res.data) > 0 if res and res.data else False

    async def rotate_device_token(self, device_name: str, new_token: str) -> dict | None:
        """Update stored device_token_hash for an authenticated device."""
        clean_name = device_name.strip()
        new_hash = hash_device_token(new_token)
        existing = await self.get_by_device_id(clean_name)
        if not existing:
            return None

        res = await (
            self.db.table(self.TABLE)
            .update({"device_token_hash": new_hash})
            .eq("id", existing["id"])
            .execute()
        )
        return res.data[0] if res and res.data else None
