import hashlib
from supabase import AsyncClient
from src.Models.schemas import UserCreateRequest


class UserRepository:
    TABLE = "users"

    # Server-side salt applied on top of whatever the client sends.
    # Prevents pass-the-hash attacks: a leaked DB row cannot be replayed
    # directly against the login endpoint.
    _SERVER_SALT = "ReceiptLogger_Secure_Salt_2026"

    def __init__(self, db: AsyncClient):
        self.db = db

    # ── PASSWORD HASHING ──────────────────────────────────────────────────────
    # This is a pure CPU operation — intentionally kept sync (no I/O).

    def hash_password(self, password: str) -> str:
        """Apply server-side PBKDF2/SHA-256 salted hash to the incoming password string."""
        salted = f"{password}:{self._SERVER_SALT}".encode("utf-8")
        return hashlib.pbkdf2_hmac(
            "sha256",
            salted,
            b"rl_static_pepper",
            iterations=100_000,
        ).hex()

    # ── READS ─────────────────────────────────────────────────────────────────

    async def get_by_username(self, username: str) -> dict | None:
        """Fetch a raw user row (including password hash) by username (case-insensitive)."""
        res = await (
            self.db.table(self.TABLE)
            .select("*")
            .ilike("username", username.strip())
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return res.data if res else None

    async def get_by_id(self, user_id: str) -> dict | None:
        """Fetch a sanitized user row (no password) by UUID."""
        res = await (
            self.db.table(self.TABLE)
            .select("id, username, avatar_image_path, created_at, deleted_at")
            .eq("id", user_id)
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return res.data if res else None

    # ── WRITES ────────────────────────────────────────────────────────────────

    async def create(self, req: UserCreateRequest) -> dict:
        """Hash password and insert a new user row. Returns sanitized record."""
        hashed_pwd = self.hash_password(req.password)
        row = {
            "username": req.username.strip(),
            "password": hashed_pwd,
            "avatar_image_path": req.avatar_image_path,
        }
        res = await self.db.table(self.TABLE).insert(row).execute()
        user_data = res.data[0]
        user_data.pop("password", None)  # Never expose the hash
        return user_data

    # ── SOFT DELETE ───────────────────────────────────────────────────────────

    async def soft_delete(self, user_id: str) -> bool:
        """Soft-delete user account by setting deleted_at to now() and unlinking all devices.

        First attempts calling Supabase RPC 'soft_delete_user' (which runs as SECURITY DEFINER
        with postgres privileges). If RPC is not created, falls back to direct table update.
        """
        from datetime import datetime, timezone

        # 1. Try RPC first (SECURITY DEFINER bypasses RLS anon restriction)
        try:
            rpc_res = await self.db.rpc("soft_delete_user", {"target_user_id": user_id}).execute()
            if rpc_res and rpc_res.data is True:
                return True
        except Exception:
            pass  # RPC not installed in Supabase yet, fall back to direct update

        # 2. Direct table update fallback
        now = datetime.now(timezone.utc).isoformat()
        try:
            res = await (
                self.db.table(self.TABLE)
                .update({"deleted_at": now})
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            success = len(res.data) > 0 if res and res.data else False

            if success:
                # Terminate active sessions by reverting user's devices to guest mode
                try:
                    await (
                        self.db.table("devices")
                        .update({"user_id": None})
                        .eq("user_id", user_id)
                        .execute()
                    )
                except Exception:
                    pass

            return success
        except Exception as e:
            # Catch Postgrest permission denied (42501) cleanly
            if "permission denied" in str(e).lower() or "42501" in str(e):
                # Account deletion requires RPC or table grant in Supabase
                return False
            raise e
