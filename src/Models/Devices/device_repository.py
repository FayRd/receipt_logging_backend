from supabase import AsyncClient
from src.Models.schemas import DeviceRegisterRequest


class DeviceRepository:
    TABLE = "devices"

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── READS ─────────────────────────────────────────────────────────────────

    async def get_by_device_id(self, device_id: str) -> dict | None:
        """Fetch active device record by hardware device_id string."""
        res = await (
            self.db.table(self.TABLE)
            .select("*")
            .eq("device_id", device_id.strip())
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return res.data if res else None

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
        """Register new device or update user_id association if device already exists.

        Upserts by hardware device_id — a device that reconnects after a reinstall
        or signs into a different account will have its user_id updated in-place.
        """
        existing = await self.get_by_device_id(req.device_id)
        if existing:
            # Only update if user_id has actually changed
            if existing.get("user_id") != req.user_id:
                res = await (
                    self.db.table(self.TABLE)
                    .update({"user_id": req.user_id})
                    .eq("id", existing["id"])
                    .execute()
                )
                return res.data[0]
            return existing

        row = {
            "device_id": req.device_id.strip(),
            "user_id": req.user_id,
        }
        res = await self.db.table(self.TABLE).insert(row).execute()
        return res.data[0]

    async def link_user(self, device_id: str, user_id: str | None) -> dict | None:
        """Link or unlink a device to a user account.

        Passing user_id=None effectively sets the device to guest/anonymous mode.
        Returns None if device_id was never registered.
        """
        existing = await self.get_by_device_id(device_id)
        if not existing:
            return None

        res = await (
            self.db.table(self.TABLE)
            .update({"user_id": user_id})
            .eq("id", existing["id"])
            .execute()
        )
        return res.data[0] if res else None
