import hashlib
import time
from datetime import datetime, timezone
from supabase import AsyncClient
from src.Infrastructure.logger import get_logger
from src.Models.schemas import UserCreateRequest, UserUpdateRequest

logger = get_logger("Models.user_repository")

# Columns returned in all sanitized (non-auth) user fetches
_USER_SAFE_COLUMNS = "id, username, email, country_code, mobile_number, avatar_image_path, custom_categories, preferences, created_at, deleted_at"



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

    @staticmethod
    def hash_password(password: str) -> str:
        """Apply server-side PBKDF2/SHA-256 salted hash to the incoming password string."""
        salted = f"{password}:{UserRepository._SERVER_SALT}".encode("utf-8")
        return hashlib.pbkdf2_hmac(
            "sha256",
            salted,
            b"rl_static_pepper",
            iterations=100_000,
        ).hex()

    # ── READS ─────────────────────────────────────────────────────────────────

    async def get_by_username(self, username: str) -> dict | None:
        """Fetch a raw user row (including password hash) by username (case-insensitive)."""
        start_time = time.perf_counter()
        clean_user = username.strip()
        logger.debug("SELECT user get_by_username: username='%s'", clean_user)
        try:
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .ilike("username", clean_user)
                .is_("deleted_at", "null")
                .maybe_single()
                .execute()
            )
            result = res.data if res else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT user get_by_username finished: found=%s in %.2fms", result is not None, duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_username '%s' after %.2fms: %s", clean_user, duration_ms, e, exc_info=True)
            raise

    async def get_by_email(self, email: str) -> dict | None:
        """Fetch a raw user row (including password hash) by email (case-insensitive)."""
        start_time = time.perf_counter()
        clean_email = email.strip().lower()
        logger.debug("SELECT user get_by_email: email='%s'", clean_email)
        try:
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("email", clean_email)
                .is_("deleted_at", "null")
                .maybe_single()
                .execute()
            )
            if res and res.data:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info("SELECT user get_by_email matched: id=%s in %.2fms", res.data.get("id"), duration_ms)
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
            result = res_ilike.data if res_ilike else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT user get_by_email ilike search finished: found=%s in %.2fms", result is not None, duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_email '%s' after %.2fms: %s", clean_email, duration_ms, e, exc_info=True)
            raise

    async def get_by_identifier(self, identifier: str) -> dict | None:
        """Fetch a raw user row by username or email (case-insensitive)."""
        logger.debug("SELECT user get_by_identifier: identifier='%s'", identifier)
        user = await self.get_by_username(identifier)
        if user:
            return user
        return await self.get_by_email(identifier)

    async def get_by_email_or_mobile(self, identifier: str) -> dict | None:
        """Fetch user row by username, email, or mobile_number (case-insensitive)."""
        start_time = time.perf_counter()
        clean = identifier.strip()
        logger.debug("SELECT user get_by_email_or_mobile: clean_identifier='%s'", clean)
        try:
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
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info("SELECT user get_by_email_or_mobile matched mobile: id=%s in %.2fms", res.data.get("id"), duration_ms)
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
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    logger.info("SELECT user get_by_email_or_mobile matched cleaned mobile: id=%s in %.2fms", res_digits.data.get("id"), duration_ms)
                    return res_digits.data

            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT user get_by_email_or_mobile: not found in %.2fms", duration_ms)
            return None
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_email_or_mobile after %.2fms: %s", duration_ms, e, exc_info=True)
            raise

    async def get_by_id(self, user_id: str) -> dict | None:
        """Fetch a sanitized user row (no password) by UUID."""
        start_time = time.perf_counter()
        logger.debug("SELECT user get_by_id: user_id=%s", user_id)
        try:
            res = await (
                self.db.table(self.TABLE)
                .select(_USER_SAFE_COLUMNS)
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .maybe_single()
                .execute()
            )
            result = res.data if res else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT user get_by_id finished: found=%s in %.2fms", result is not None, duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_id user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    # ── WRITES ────────────────────────────────────────────────────────────────

    async def create(self, req: UserCreateRequest) -> dict:
        """Hash password and insert a new user row. Returns sanitized record."""
        start_time = time.perf_counter()
        logger.debug("INSERT user create: username='%s', email='%s'", req.username, req.email)
        try:
            hashed_pwd = self.hash_password(req.password)
            cats = [c.model_dump(by_alias=True) if hasattr(c, "model_dump") else c for c in req.custom_categories] if req.custom_categories else []
            row = {
                "username": req.username.strip(),
                "email": req.email.strip().lower(),
                "password": hashed_pwd,
                "country_code": req.country_code,
                "mobile_number": req.mobile_number,
                "avatar_image_path": req.avatar_image_path,
                "custom_categories": cats,
                "preferences": req.preferences if req.preferences is not None else {},
            }
            res = await self.db.table(self.TABLE).insert(row).execute()
            user_data = res.data[0]
            user_data.pop("password", None)  # Never expose the hash
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("INSERT user create succeeded: id=%s in %.2fms", user_data.get("id"), duration_ms)
            return user_data
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in INSERT user create username='%s' after %.2fms: %s", req.username, duration_ms, e, exc_info=True)
            raise

    async def update_profile(self, user_id: str, req: UserUpdateRequest) -> dict | None:
        """Patch mutable profile fields for a user. Only non-None fields are written."""
        start_time = time.perf_counter()
        logger.debug("UPDATE user profile: user_id=%s", user_id)
        try:
            updates: dict = {}
            if req.email is not None:
                updates["email"] = req.email.strip().lower()
            if req.country_code is not None:
                updates["country_code"] = req.country_code
            if req.mobile_number is not None:
                updates["mobile_number"] = req.mobile_number
            if req.avatar_image_path is not None:
                updates["avatar_image_path"] = req.avatar_image_path
            if req.custom_categories is not None:
                updates["custom_categories"] = [
                    c.model_dump(by_alias=True) if hasattr(c, "model_dump") else c
                    for c in req.custom_categories
                ]
            if req.preferences is not None:
                updates["preferences"] = req.preferences

            if not updates:
                logger.debug("No fields provided to update_profile for user_id=%s; fetching profile", user_id)
                return await self.get_by_id(user_id)

            res = await (
                self.db.table(self.TABLE)
                .update(updates)
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            if not res.data:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.warning("UPDATE user profile found no matching row for user_id=%s (%.2fms)", user_id, duration_ms)
                return None
            user_data = res.data[0]
            user_data.pop("password", None)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("UPDATE user profile succeeded for user_id=%s in %.2fms", user_id, duration_ms)
            return user_data
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in UPDATE user profile user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    # ── SOFT DELETE ───────────────────────────────────────────────────────────

    async def soft_delete(self, user_id: str) -> bool:
        """Soft-delete user account by setting deleted_at to now() and unlinking all devices."""
        start_time = time.perf_counter()
        logger.debug("UPDATE (soft_delete) user: user_id=%s", user_id)

        # 1. Try RPC first (SECURITY DEFINER bypasses RLS anon restriction)
        try:
            rpc_res = await self.db.rpc("soft_delete_user", {"target_user_id": user_id}).execute()
            if rpc_res and rpc_res.data is True:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info("RPC soft_delete_user succeeded for user_id=%s in %.2fms", user_id, duration_ms)
                return True
        except Exception as exc:
            logger.debug("RPC soft_delete_user not available or failed for user_id=%s: %s (trying direct update)", user_id, exc)

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
                    logger.info("Reverted user's devices to guest mode after soft_delete user_id=%s", user_id)
                except Exception as dev_exc:
                    logger.warning("Failed to unbind devices during user soft_delete user_id=%s: %s", user_id, dev_exc)

            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("Direct UPDATE (soft_delete) user_id=%s finished: success=%s in %.2fms", user_id, success, duration_ms)
            return success
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            if "permission denied" in str(e).lower() or "42501" in str(e):
                logger.warning("Permission denied for direct soft_delete user_id=%s (42501) after %.2fms", user_id, duration_ms)
                return False
            logger.error("Database error in soft_delete user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise e

