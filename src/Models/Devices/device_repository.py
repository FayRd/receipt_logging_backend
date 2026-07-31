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
        """Register new device or update token/user_id if device already exists.

        Idempotent by hardware device_id. On re-registration (e.g. app reinstall),
        the device_token is refreshed and user_id updated if provided.
        """
        existing = await self.get_by_device_id(req.device_id)
        if existing:
            updates: dict = {"device_token": req.device_token}
            if req.user_id is not None:
                updates["user_id"] = req.user_id

            res = await (
                self.db.table(self.TABLE)
                .update(updates)
                .eq("id", existing["id"])
                .execute()
            )
            return res.data[0]

        row = {
            "device_id": req.device_id.strip(),
            "device_token": req.device_token.strip(),
            "user_id": req.user_id,
        }
        res = await self.db.table(self.TABLE).insert(row).execute()
        return res.data[0]

    async def link_user(
        self, device_id: str, device_token: str, user_id: str | None
    ) -> dict | None:
        """Link or unlink a device to a user account.

        Requires device_token for ownership verification before modifying the link.
        Returns None if device_id was never registered or token is invalid.
        Passing user_id=None effectively sets the device to guest/anonymous mode.

        Note: Token correctness is enforced by get_current_identity dependency upstream.
        This method does a final DB-level token check for defence-in-depth.
        """
        existing = await self.get_by_device_id(device_id)
        if not existing:
            return None

        # Defence-in-depth token check (upstream dependency already verified this)
        if existing.get("device_token") != device_token:
            return None

        res = await (
            self.db.table(self.TABLE)
            .update({"user_id": user_id})
            .eq("id", existing["id"])
            .execute()
        )
        return res.data[0] if res else None
