import secrets
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
        token updates on an existing device are only allowed if the caller presents
        the correct stored token (verified via secrets.compare_digest in constant time)
        to prevent unauthenticated device takeover attacks.
        """
        clean_id = req.device_id.strip()
        clean_token = req.device_token.strip()

        existing = await self.get_by_device_id(clean_id)
        if existing:
            # Constant-time comparison prevents token guessing & unauthorized device takeover
            stored_bytes = existing["device_token"].encode("utf-8")
            incoming_bytes = clean_token.encode("utf-8")

            if not secrets.compare_digest(incoming_bytes, stored_bytes):
                # Fail closed — return None to prevent user_id enumeration & fake 201s
                return None

            updates: dict = {"device_token": clean_token}
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
            "device_id": clean_id,
            "device_token": clean_token,
            "user_id": req.user_id,
        }
        res = await self.db.table(self.TABLE).insert(row).execute()
        return res.data[0]

    async def link_user(
        self, device_id: str, device_token: str, user_id: str | None
    ) -> dict | None:
        """Link or unlink a device to a user account.

        When user_id is non-null (linking/login):
        1. Verifies device token in constant time (secrets.compare_digest).
        2. Updates devices table user_id.
        3. Atomically migrates all guest receipts (user_id IS NULL) for this device_id to user_id.
        4. Atomically migrates all guest conversations (user_id IS NULL) for this device_id to user_id.

        When user_id is null (unlinking/logout):
        1. Updates devices table user_id = NULL.
        2. Receipts and conversations remain owned by their respective user_id, ensuring
           complete data isolation and privacy for guest sessions.
        """
        clean_device_id = device_id.strip()
        clean_device_token = device_token.strip()

        existing = await self.get_by_device_id(clean_device_id)
        if not existing:
            return None

        # Constant-time token verification prevents timing attacks and guest data hijacking
        stored_bytes = existing.get("device_token", "").encode("utf-8")
        incoming_bytes = clean_device_token.encode("utf-8")

        if not secrets.compare_digest(incoming_bytes, stored_bytes):
            return None

        res = await (
            self.db.table(self.TABLE)
            .update({"user_id": user_id})
            .eq("id", existing["id"])
            .execute()
        )
        updated_device = res.data[0] if res else None

        # If linking to a user account, claim all orphan guest receipts and conversations
        if user_id and updated_device:
            # Migrate guest receipts
            await (
                self.db.table("receipts")
                .update({"user_id": user_id})
                .eq("device_id", clean_device_id)
                .is_("user_id", "null")
                .execute()
            )
            # Migrate guest conversations
            await (
                self.db.table("conversations")
                .update({"user_id": user_id})
                .eq("device_id", clean_device_id)
                .is_("user_id", "null")
                .execute()
            )

        return updated_device
