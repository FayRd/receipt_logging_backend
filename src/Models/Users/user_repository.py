import hashlib
from supabase import AsyncClient
from src.Models.schemas import UserCreateRequest, UserUpdateRequest

# Columns returned in all sanitized (non-auth) user fetches
_USER_SAFE_COLUMNS = "id, username, email, country_code, mobile_number, avatar_image_path, created_at, deleted_at"


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

    async def get_by_email(self, email: str) -> dict | None:
        """Fetch a raw user row (including password hash) by email (case-insensitive)."""
        clean_email = email.strip().lower()
        res = await (
            self.db.table(self.TABLE)
            .select("*")
            .eq("email", clean_email)
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        if res and res.data:
            return res.data

        # Fallback to ilike if stored with mixed casing
        res_ilike = await (
            self.db.table(self.TABLE)
            .select("*")
            .ilike("email", email.strip())
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return res_ilike.data if res_ilike else None

    async def get_by_identifier(self, identifier: str) -> dict | None:
        """Fetch a raw user row by username or email (case-insensitive).

        Tries username first; if not found, attempts email lookup.
        Used by the login endpoint to support both login modes.
        """
        user = await self.get_by_username(identifier)
        if user:
            return user
        return await self.get_by_email(identifier)

    async def get_by_email_or_mobile(self, identifier: str) -> dict | None:
        """Fetch user row by username, email, or mobile_number (case-insensitive)."""
        clean = identifier.strip()
        user = await self.get_by_identifier(clean)
        if user:
            return user

        res = await (
            self.db.table(self.TABLE)
            .select("*")
            .eq("mobile_number", clean)
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        if res and res.data:
            return res.data

        clean_digits = "".join(c for c in clean if c.isdigit() or c == "+")
        if clean_digits and clean_digits != clean:
            res_digits = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("mobile_number", clean_digits)
                .is_("deleted_at", "null")
                .maybe_single()
                .execute()
            )
            if res_digits and res_digits.data:
                return res_digits.data

        return None

    async def get_by_id(self, user_id: str) -> dict | None:
        """Fetch a sanitized user row (no password) by UUID."""
        res = await (
            self.db.table(self.TABLE)
            .select(_USER_SAFE_COLUMNS)
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
            "email": req.email.strip().lower(),
            "password": hashed_pwd,
            "country_code": req.country_code,
            "mobile_number": req.mobile_number,
            "avatar_image_path": req.avatar_image_path,
        }
        res = await self.db.table(self.TABLE).insert(row).execute()
        user_data = res.data[0]
        user_data.pop("password", None)  # Never expose the hash
        return user_data

    async def update_profile(self, user_id: str, req: UserUpdateRequest) -> dict | None:
        """Patch mutable profile fields for a user. Only non-None fields are written.

        Returns the updated sanitized user row, or None if the user was not found.
        """
        updates: dict = {}
        if req.email is not None:
            updates["email"] = req.email.strip().lower()
        if req.country_code is not None:
            updates["country_code"] = req.country_code
        if req.mobile_number is not None:
            updates["mobile_number"] = req.mobile_number
        if req.avatar_image_path is not None:
            updates["avatar_image_path"] = req.avatar_image_path

        if not updates:
            # No-op: return current profile without touching DB
            return await self.get_by_id(user_id)

        res = await (
            self.db.table(self.TABLE)
            .update(updates)
            .eq("id", user_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if not res.data:
            return None
        user_data = res.data[0]
        user_data.pop("password", None)
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
