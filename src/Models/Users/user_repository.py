import hashlib
import time
from datetime import datetime, timezone, timedelta
from supabase import AsyncClient
from src.Infrastructure.logger import get_logger
from src.Models.schemas import UserCreateRequest, UserUpdateRequest

logger = get_logger("Models.user_repository")

# Columns returned in all sanitized (non-auth) user fetches
_USER_SAFE_COLUMNS = "id, username, email, country_code, mobile_number, avatar_image_path, custom_categories, preferences, email_verified_at, mobile_verified_at, tier, created_at, deleted_at"



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
        """Hash password and insert a new user row with automatic 14-day Premium reverse trial. Returns sanitized record."""
        start_time = time.perf_counter()
        logger.debug("INSERT user create: username='%s', email='%s'", req.username, req.email)
        try:
            hashed_pwd = self.hash_password(req.password)
            cats = [c.model_dump(by_alias=True) if hasattr(c, "model_dump") else c for c in req.custom_categories] if req.custom_categories else []
            prefs = dict(req.preferences) if req.preferences is not None else {}

            device_id = prefs.get("trial_device_id")
            is_device_reused = False
            if device_id:
                is_device_reused = await self.check_device_trial_used(device_id)

            now = datetime.now(timezone.utc)
            if is_device_reused:
                # Device already consumed a trial
                tier = "free"
                prefs["is_in_trial"] = False
                prefs["trial_ineligible"] = True
                logger.info("Device %s already consumed trial. Account '%s' created on Free tier.", device_id, req.username)
            else:
                # Automatic 14-day Premium reverse trial
                tier = "premium"
                prefs["trial_start_at"] = now.isoformat()
                prefs["is_in_trial"] = True
                logger.info("Granting 14-day reverse Premium trial to new user '%s'", req.username)

            row = {
                "username": req.username.strip(),
                "email": req.email.strip().lower(),
                "password": hashed_pwd,
                "country_code": req.country_code,
                "mobile_number": req.mobile_number,
                "avatar_image_path": req.avatar_image_path,
                "custom_categories": cats,
                "preferences": prefs,
                "tier": tier,
            }
            res = await self.db.table(self.TABLE).insert(row).execute()
            user_data = res.data[0]
            user_data.pop("password", None)  # Never expose the hash
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("INSERT user create succeeded: id=%s, tier=%s in %.2fms", user_data.get("id"), user_data.get("tier"), duration_ms)
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

    # ── PASSWORD MANAGEMENT ───────────────────────────────────────────────────

    async def get_by_id_with_password(self, user_id: str) -> dict | None:
        """Fetch raw user row including password hash for authentication / password verification."""
        start_time = time.perf_counter()
        logger.debug("SELECT user get_by_id_with_password: user_id=%s", user_id)
        try:
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .maybe_single()
                .execute()
            )
            result = res.data if res else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT user get_by_id_with_password finished: found=%s in %.2fms", result is not None, duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_id_with_password user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    async def update_password(
        self,
        user_id: str,
        new_password_hash: str,
        updated_preferences: dict | None = None,
    ) -> bool:
        """Update password hash and optional preferences for an active user."""
        start_time = time.perf_counter()
        logger.debug("UPDATE user password: user_id=%s, has_preferences=%s", user_id, updated_preferences is not None)
        try:
            updates: dict = {"password": new_password_hash}
            if updated_preferences is not None:
                updates["preferences"] = updated_preferences

            res = await (
                self.db.table(self.TABLE)
                .update(updates)
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            success = bool(res and res.data and len(res.data) > 0)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("UPDATE user password succeeded for user_id=%s in %.2fms (success=%s)", user_id, duration_ms, success)
            return success
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in UPDATE user password user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    # ── EMAIL VERIFICATION ────────────────────────────────────────────────────

    async def set_email_verified(self, user_id: str, email: str, verified_at: datetime) -> dict | None:
        """Update users.email and users.email_verified_at upon successful OTP verification.

        Also resets email_verified_at to NULL for any other account that shares the
        same email (prevents stale verified states when email is transferred).
        """
        start_time = time.perf_counter()
        clean_email = email.strip().lower()
        iso_ts = verified_at.isoformat()
        logger.debug("UPDATE set_email_verified: user_id=%s, email=%s", user_id, clean_email)
        try:
            res = await (
                self.db.table(self.TABLE)
                .update({"email": clean_email, "email_verified_at": iso_ts})
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            if not res.data:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.warning("set_email_verified found no matching row for user_id=%s (%.2fms)", user_id, duration_ms)
                return None
            user_data = res.data[0]
            user_data.pop("password", None)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("set_email_verified succeeded for user_id=%s in %.2fms", user_id, duration_ms)
            return user_data
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in set_email_verified user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    # ── ECONOMIC MODEL: TRIALS, SUBSCRIPTIONS & AD SCANS ──────────────────────

    async def check_device_trial_used(self, device_id: str | None) -> bool:
        """Check whether a client device has already consumed a reverse trial."""
        if not device_id or not device_id.strip():
            return False
        clean_device = device_id.strip()
        try:
            # 1. Check users table preferences for trial_device_id
            res = await (
                self.db.table(self.TABLE)
                .select("id")
                .contains("preferences", {"trial_device_id": clean_device})
                .is_("deleted_at", "null")
                .limit(1)
                .execute()
            )
            if res and res.data:
                return True

            # 2. Check devices table if device was registered and linked to an account
            res_dev = await (
                self.db.table("devices")
                .select("id, user_id")
                .eq("name", clean_device)
                .not_.is_("user_id", "null")
                .is_("deleted_at", "null")
                .limit(1)
                .execute()
            )
            if res_dev and res_dev.data:
                return True

            return False
        except Exception as e:
            logger.warning("Error checking device trial consumption for device=%s: %s", clean_device, e)
            return False

    async def simulate_trial_expiry(self, user_id: str) -> dict | None:
        """Simulate 14-day trial expiration by setting trial_start_at to 15 days ago and applying downgrade."""
        user = await self.get_by_id(user_id)
        if not user:
            return None
        prefs = dict(user.get("preferences") or {})
        past_15_days = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        prefs["trial_start_at"] = past_15_days
        prefs["is_in_trial"] = True

        # Update user with past trial start time
        now_iso = datetime.now(timezone.utc).isoformat()
        await (
            self.db.table(self.TABLE)
            .update({
                "preferences": prefs,
                "tier": "premium",
                "updated_at": now_iso,
            })
            .eq("id", user_id)
            .execute()
        )

        user["preferences"] = prefs
        user["tier"] = "premium"
        updated_user = await self.check_and_apply_trial_expiration(user)
        return updated_user

    async def set_trial_start(self, user_id: str, device_id: str | None = None) -> dict | None:
        """Grant 14-day free reverse trial to user and record trial timestamp in preferences."""
        start_time = time.perf_counter()
        now = datetime.now(timezone.utc)
        iso_ts = now.isoformat()
        try:
            user = await self.get_by_id(user_id)
            if not user:
                return None

            prefs = user.get("preferences") or {}
            prefs["trial_start_at"] = iso_ts
            prefs["is_in_trial"] = True
            if device_id:
                prefs["trial_device_id"] = device_id.strip()

            res = await (
                self.db.table(self.TABLE)
                .update({
                    "tier": "premium",
                    "preferences": prefs,
                    "updated_at": iso_ts,
                })
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            if not res.data:
                return None
            user_data = res.data[0]
            user_data.pop("password", None)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("Reverse trial granted (14 days) for user_id=%s in %.2fms", user_id, duration_ms)
            return user_data
        except Exception as e:
            logger.error("Failed to set trial start for user_id=%s: %s", user_id, e, exc_info=True)
            raise

    async def set_tier(self, user_id: str, tier: str, updated_preferences: dict | None = None) -> dict | None:
        """Update the user's subscription tier ('free', 'premium', 'dev') and preferences."""
        start_time = time.perf_counter()
        clean_tier = tier.strip().lower()
        now = datetime.now(timezone.utc).isoformat()
        try:
            user = await self.get_by_id(user_id)
            if not user:
                return None

            prefs = user.get("preferences") or {}
            if updated_preferences:
                prefs.update(updated_preferences)

            if clean_tier == "premium":
                prefs["is_in_trial"] = False
                prefs["discount_offer_claimed"] = True
                prefs["discount_offer_shown_at"] = None
            else:
                prefs["is_in_trial"] = False

            res = await (
                self.db.table(self.TABLE)
                .update({
                    "tier": clean_tier,
                    "preferences": prefs,
                    "updated_at": now,
                })
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            if not res.data:
                return None
            user_data = res.data[0]
            user_data.pop("password", None)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("Updated tier to '%s' for user_id=%s in %.2fms", clean_tier, user_id, duration_ms)
            return user_data
        except Exception as e:
            logger.error("Failed to set tier for user_id=%s: %s", user_id, e, exc_info=True)
            raise

    async def set_discount_offer_shown(self, user_id: str) -> dict | None:
        """Record timestamp when the 7-day downgrade discount offer was first triggered.
        Guarded: Only applies to users who actually completed the 14-day trial.
        """
        now = datetime.now(timezone.utc)
        try:
            user = await self.get_by_id(user_id)
            if not user:
                return None

            prefs = user.get("preferences") or {}
            # Guard: User must have had a trial, must not be trial ineligible, and must not have already claimed discount
            if not prefs.get("trial_start_at") or prefs.get("trial_ineligible", False) or prefs.get("discount_offer_claimed", False):
                logger.info("Skipping discount offer for ineligible user_id=%s", user_id)
                return user

            if not prefs.get("discount_offer_shown_at"):
                prefs["discount_offer_shown_at"] = now.isoformat()
                res = await (
                    self.db.table(self.TABLE)
                    .update({"preferences": prefs, "updated_at": now.isoformat()})
                    .eq("id", user_id)
                    .is_("deleted_at", "null")
                    .execute()
                )
                if res.data:
                    user_data = res.data[0]
                    user_data.pop("password", None)
                    return user_data
            return user
        except Exception as e:
            logger.warning("Failed to record discount_offer_shown_at for user_id=%s: %s", user_id, e)
            return None

    async def grant_ad_scan(self, user_id: str) -> tuple[bool, int, str]:
        """Grant 1 additional scan after user watches a rewarded ad (max 5/day)."""
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        try:
            user = await self.get_by_id(user_id)
            if not user:
                return False, 0, "User not found"

            prefs = user.get("preferences") or {}
            saved_date = prefs.get("ad_scans_date")
            ad_scans_today = prefs.get("ad_scans_today", 0)

            if saved_date != today_str:
                ad_scans_today = 0

            if ad_scans_today >= 5:
                return False, ad_scans_today, "Daily ad scan limit reached (5/5). Resets at 00:00 UTC."

            ad_scans_today += 1
            prefs["ad_scans_today"] = ad_scans_today
            prefs["ad_scans_date"] = today_str

            await (
                self.db.table(self.TABLE)
                .update({"preferences": prefs, "updated_at": now.isoformat()})
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            logger.info("Ad scan granted for user_id=%s (now %d/5)", user_id, ad_scans_today)
            return True, ad_scans_today, ""
        except Exception as e:
            logger.error("Failed to grant ad scan for user_id=%s: %s", user_id, e, exc_info=True)
            return False, 0, "Internal error granting ad scan"

    async def get_user_stats(self, user_id: str) -> dict:
        """Fetch receipt count, estimated time saved (16.5s/scan), trial and offer status."""
        user = await self.get_by_id(user_id)
        if not user:
            return {
                "total_receipts": 0,
                "time_saved_seconds": 0.0,
                "time_saved_minutes": 0.0,
                "trial_start_at": None,
                "discount_offer_shown_at": None,
                "tier": "free",
                "is_in_trial": False,
                "ad_scans_today": 0,
                "ad_scans_remaining": 5,
            }

        # Auto-expire trial if past 14 days
        user = await self.check_and_apply_trial_expiration(user)

        prefs = user.get("preferences") or {}
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        # Query total active receipts
        total_receipts = 0
        try:
            res = await (
                self.db.table("receipts")
                .select("id", count="exact")
                .eq("user_id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            total_receipts = res.count if res.count is not None else len(res.data or [])
        except Exception as e:
            logger.warning("Error fetching receipts count for stats user_id=%s: %s", user_id, e)

        # Average time saved: 16.5 seconds per receipt (25s free vs 8.5s premium)
        time_saved_seconds = round(total_receipts * 16.5, 1)
        time_saved_minutes = round(time_saved_seconds / 60, 1)

        ad_scans_today = prefs.get("ad_scans_today", 0) if prefs.get("ad_scans_date") == today_str else 0
        ad_scans_remaining = max(0, 5 - ad_scans_today)

        return {
            "total_receipts": total_receipts,
            "time_saved_seconds": time_saved_seconds,
            "time_saved_minutes": time_saved_minutes,
            "trial_start_at": prefs.get("trial_start_at"),
            "discount_offer_shown_at": prefs.get("discount_offer_shown_at"),
            "tier": user.get("tier", "free"),
            "is_in_trial": prefs.get("is_in_trial", False),
            "ad_scans_today": ad_scans_today,
            "ad_scans_remaining": ad_scans_remaining,
        }

    async def check_and_apply_trial_expiration(self, user: dict) -> dict:
        """Evaluate whether a user's 14-day reverse trial has expired, and downgrade if so."""
        tier = (user.get("tier") or "free").lower()
        prefs = user.get("preferences") or {}
        if tier != "premium" or not prefs.get("is_in_trial"):
            return user

        trial_start_str = prefs.get("trial_start_at")
        if not trial_start_str:
            return user

        try:
            trial_start = datetime.fromisoformat(trial_start_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if now - trial_start > timedelta(days=14):
                # 14-day trial expired! Downgrade to free tier
                logger.info("Trial expired for user_id=%s (started %s). Downgrading to Free.", user["id"], trial_start_str)
                prefs["is_in_trial"] = False
                prefs["trial_expired_at"] = now.isoformat()
                if not prefs.get("discount_offer_shown_at"):
                    prefs["discount_offer_shown_at"] = now.isoformat()

                res = await (
                    self.db.table(self.TABLE)
                    .update({
                        "tier": "free",
                        "preferences": prefs,
                        "updated_at": now.isoformat(),
                    })
                    .eq("id", user["id"])
                    .is_("deleted_at", "null")
                    .execute()
                )
                if res.data:
                    updated = res.data[0]
                    updated.pop("password", None)
                    return updated
        except Exception as e:
            logger.warning("Failed evaluating trial expiration for user_id=%s: %s", user.get("id"), e)

        return user
