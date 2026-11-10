import hashlib
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from fastapi import HTTPException
from postgrest.exceptions import APIError
from supabase import AsyncClient
from src.Infrastructure.crypto import get_crypto_engine, compute_mobile_hash
from src.Infrastructure.key_vault import get_key_vault
from src.Infrastructure.logger import get_logger
from src.Models.schemas import UserCreateRequest, UserUpdateRequest
from src.config import get_settings

logger = get_logger("Models.user_repository")

# Columns returned in all sanitized (non-auth) user fetches
_USER_SAFE_COLUMNS = "id, username, email, google_id, country_code, mobile_number, mobile_hash, avatar_image_path, custom_categories, preferences, email_verified_at, mobile_verified_at, tier, is_2fa_enabled, enc_version, created_at, deleted_at"



def _handle_insert_conflict(e: Exception, username: str, conflict_label: str) -> None:
    """Detect PostgreSQL 23505 unique constraint violation and raise HTTP 409 Conflict."""
    if isinstance(e, HTTPException):
        raise e
    err_code = getattr(e, "code", "") or ""
    err_msg = getattr(e, "message", "") or str(e)
    if err_code == "23505" or "23505" in str(e) or "unique constraint" in err_msg.lower():
        logger.warning(
            "Duplicate key violation (23505) in user creation for username='%s': %s",
            username, e,
        )
        raise HTTPException(status_code=409, detail=f"An account with this email or {conflict_label} already exists.")


class UserRepository:
    TABLE = "users"

    # Server-side salt applied on top of whatever the client sends.
    # Prevents pass-the-hash attacks: a leaked DB row cannot be replayed
    # directly against the login endpoint.
    _SERVER_SALT = "ReceiptLogger_Secure_Salt_2026"

    def __init__(self, db: AsyncClient):
        self.db = db
        self.crypto = get_crypto_engine()
        self.key_vault = get_key_vault()

    def _decrypt_json_field(self, val: Any, dek: bytes | None, context: str, fallback: Any) -> Any:
        if isinstance(val, dict) and val.get("_enc") in self.crypto.SUPPORTED_VERSIONS:
            if dek:
                return self.crypto.safe_decrypt_json_with_dek(val, dek, context=context, fallback=fallback)
            return self.crypto.safe_decrypt_json(val, context=context, fallback=fallback)
        return val if val is not None else fallback

    def _decrypt_text_field(self, val: Any, dek: bytes | None, context: str) -> str | None:
        if isinstance(val, str) and (val.startswith(self.crypto.TEXT_PREFIX) or val.startswith("enc:")):
            if dek:
                return self.crypto.safe_decrypt_text_with_dek(val, dek, context=context, fallback=None)
            return self.crypto.safe_decrypt_text(val, context=context, fallback=None)
        return val

    def _encrypt_json_field(self, val: Any, dek: bytes | None) -> Any:
        if val is None:
            return None
        return self.crypto.encrypt_json_with_dek(val, dek) if dek else self.crypto.encrypt_json(val)

    def _encrypt_text_field(self, val: str | None, dek: bytes | None) -> str | None:
        if val is None:
            return None
        return self.crypto.encrypt_text_with_dek(val, dek) if dek else self.crypto.encrypt_text(val)

    async def _decrypt_user_row(self, user: dict | None) -> dict | None:
        """Transparently decrypts encrypted user columns (custom_categories, preferences, country_code, mobile_number).
        Uses per-user DEK when enc_version=1, or falls back to global KEK when enc_version=0.
        Strips internal blind index mobile_hash from the returned payload.
        """
        if not user:
            return None
        res = dict(user)
        user_id = str(res.get("id") or "").strip()
        enc_version = res.get("enc_version") or 0

        dek = None
        if enc_version == 1 and user_id:
            dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)

        cats = self._decrypt_json_field(res.get("custom_categories"), dek, "users.custom_categories", [])
        res["custom_categories"] = cats if isinstance(cats, list) else []

        prefs = self._decrypt_json_field(res.get("preferences"), dek, "users.preferences", {})
        res["preferences"] = prefs if isinstance(prefs, dict) else {}

        res["country_code"] = self._decrypt_text_field(res.get("country_code"), dek, "users.country_code")
        res["mobile_number"] = self._decrypt_text_field(res.get("mobile_number"), dek, "users.mobile_number")

        res.pop("mobile_hash", None)
        return res

    def _encrypt_user_fields(self, data: dict, dek: bytes | None) -> tuple[dict, int]:
        """Encrypts custom_categories, preferences, country_code, mobile_number using DEK if available (else global key).
        Computes mobile_hash if mobile_number in data. Sets enc_version = 1 if dek else 0.
        """
        payload = dict(data)
        enc_version = 1 if dek else 0
        settings = get_settings()

        if "custom_categories" in payload and payload["custom_categories"] is not None:
            payload["custom_categories"] = self._encrypt_json_field(payload["custom_categories"], dek)

        if "preferences" in payload and payload["preferences"] is not None:
            payload["preferences"] = self._encrypt_json_field(payload["preferences"], dek)

        if "country_code" in payload and payload["country_code"] is not None:
            payload["country_code"] = self._encrypt_text_field(payload["country_code"], dek)

        if "mobile_number" in payload:
            mn = payload["mobile_number"]
            if mn is not None:
                payload["mobile_number"] = self._encrypt_text_field(mn, dek)
                payload["mobile_hash"] = compute_mobile_hash(mn, settings.data_encryption_key)
            else:
                payload["mobile_number"] = None
                payload["mobile_hash"] = None

        payload["enc_version"] = enc_version
        return payload, enc_version


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
            return await self._decrypt_user_row(result)
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_username '%s' after %.2fms: %s", clean_user, duration_ms, e, exc_info=True)
            raise

    async def get_by_google_id(self, google_id: str) -> dict | None:
        """Fetch an active user row by Google ID."""
        start_time = time.perf_counter()
        clean_gid = google_id.strip()
        logger.debug("SELECT user get_by_google_id: google_id='%s'", clean_gid)
        try:
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("google_id", clean_gid)
                .is_("deleted_at", "null")
                .maybe_single()
                .execute()
            )
            result = res.data if res else None
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("SELECT user get_by_google_id finished: found=%s in %.2fms", result is not None, duration_ms)
            return await self._decrypt_user_row(result)
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_google_id '%s' after %.2fms: %s", clean_gid, duration_ms, e, exc_info=True)
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
                return await self._decrypt_user_row(res.data)

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
            return await self._decrypt_user_row(result)
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

            settings = get_settings()
            target_hash = compute_mobile_hash(clean, settings.data_encryption_key)
            if target_hash:
                res = await (
                    self.db.table(self.TABLE)
                    .select("*")
                    .eq("mobile_hash", target_hash)
                    .is_("deleted_at", "null")
                    .maybe_single()
                    .execute()
                )
                if res and res.data:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    logger.info("SELECT user get_by_email_or_mobile matched mobile_hash: id=%s in %.2fms", res.data.get("id"), duration_ms)
                    return await self._decrypt_user_row(res.data)

            # Legacy fallback: match unencrypted mobile_number
            return await self._lookup_legacy_mobile(clean, start_time)
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in SELECT user get_by_email_or_mobile after %.2fms: %s", duration_ms, e, exc_info=True)
            raise

    async def _lookup_legacy_mobile(self, clean: str, start_time: float) -> dict | None:
        """Fallback lookup for legacy unencrypted mobile_number rows."""
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
            return await self._decrypt_user_row(res.data)

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
                return await self._decrypt_user_row(res_digits.data)

        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info("SELECT user get_by_email_or_mobile: not found in %.2fms", duration_ms)
        return None

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
            return await self._decrypt_user_row(result)
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

            email_reused = await self.check_email_trial_or_purchase_used(req.email)

            if is_device_reused or email_reused:
                # Device or email already consumed a trial or purchase
                tier = "free"
                prefs["is_in_trial"] = False
                prefs["trial_ineligible"] = True
                logger.info(
                    "Account '%s' ineligible for trial (device_used=%s, email_used=%s). Created on Free tier.",
                    req.username, is_device_reused, email_reused
                )
            else:
                # Gate 3: Trial deferred pending email verification.
                # Do NOT mark device consumed yet; keep device eligible if unverified.
                tier = "free"
                prefs["is_in_trial"] = False
                prefs["trial_pending_verification"] = True
                if device_id:
                    prefs["trial_device_id"] = device_id
                logger.info("Account '%s' created on Free tier; trial deferred pending email verification.", req.username)

            user_id = str(uuid.uuid4())
            dek = self.key_vault.generate_dek()
            enc_fields, enc_ver = self._encrypt_user_fields({
                "custom_categories": cats,
                "preferences": prefs,
                "country_code": req.country_code,
                "mobile_number": req.mobile_number,
            }, dek)

            row = {
                "id": user_id,
                "username": req.username.strip(),
                "email": req.email.strip().lower(),
                "password": hashed_pwd,
                "avatar_image_path": req.avatar_image_path,
                "tier": tier,
                **enc_fields,
            }
            res = await self.db.table(self.TABLE).insert(row).execute()
            await self.key_vault.provision_user_dek(user_id, self.db, dek=dek)

            user_data = res.data[0]
            user_data.pop("password", None)  # Never expose the hash
            decrypted = await self._decrypt_user_row(user_data)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("INSERT user create succeeded: id=%s, tier=%s in %.2fms", user_id, decrypted.get("tier"), duration_ms)
            return decrypted
        except Exception as e:
            _handle_insert_conflict(e, req.username, "username")
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in INSERT user create username='%s' after %.2fms: %s", req.username, duration_ms, e, exc_info=True)
            raise

    async def create_google_user(
        self,
        google_id: str,
        email: str,
        username: str,
        avatar_image_path: str | None = None,
        preferences: dict | None = None,
    ) -> dict:
        """Insert a new user authenticated via Google OAuth with email_verified_at set and password null."""
        start_time = time.perf_counter()
        logger.debug("INSERT google user create: username='%s', email='%s', google_id='%s'", username, email, google_id)
        try:
            prefs = dict(preferences) if preferences is not None else {}
            device_id = prefs.get("trial_device_id")
            is_device_reused = False
            if device_id:
                is_device_reused = await self.check_device_trial_used(device_id)

            email_reused = await self.check_email_trial_or_purchase_used(email)

            now = datetime.now(timezone.utc)
            if is_device_reused or email_reused:
                tier = "free"
                prefs["is_in_trial"] = False
                prefs["trial_ineligible"] = True
                logger.info(
                    "Google account '%s' ineligible for trial (device_used=%s, email_used=%s). Created on Free tier.",
                    username, is_device_reused, email_reused
                )
            else:
                # Google email is pre-verified — grant 14-day trial and mark device consumed
                tier = "premium"
                prefs["trial_start_at"] = now.isoformat()
                prefs["is_in_trial"] = True
                if device_id:
                    prefs["trial_device_id"] = device_id
                    await self.mark_device_trial_consumed(device_id)
                logger.info("Granting 14-day reverse Premium trial to new Google user '%s'", username)

            user_id = str(uuid.uuid4())
            dek = self.key_vault.generate_dek()
            enc_fields, enc_ver = self._encrypt_user_fields({
                "custom_categories": [],
                "preferences": prefs,
                "country_code": None,
                "mobile_number": None,
            }, dek)

            row = {
                "id": user_id,
                "username": username.strip(),
                "email": email.strip().lower(),
                "password": None,
                "google_id": google_id.strip(),
                "email_verified_at": now.isoformat(),
                "avatar_image_path": avatar_image_path,
                "tier": tier,
                **enc_fields,
            }
            res = await self.db.table(self.TABLE).insert(row).execute()
            await self.key_vault.provision_user_dek(user_id, self.db, dek=dek)

            user_data = res.data[0]
            user_data.pop("password", None)
            decrypted = await self._decrypt_user_row(user_data)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("INSERT google user create succeeded: id=%s, tier=%s in %.2fms", user_id, decrypted.get("tier"), duration_ms)
            return decrypted
        except Exception as e:
            _handle_insert_conflict(e, username, "Google ID")
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in INSERT google user create username='%s' after %.2fms: %s", username, duration_ms, e, exc_info=True)
            raise

    async def claim_unverified_account_with_google(self, user_id: str, google_id: str) -> dict:
        """Link Google ID to an unverified user account, verify email, and purge existing password stub (OWASP pre-takeover defense)."""
        start_time = time.perf_counter()
        logger.debug("CLAIM unverified user: user_id='%s', google_id='%s'", user_id, google_id)
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            payload = {
                "google_id": google_id.strip(),
                "email_verified_at": now_iso,
                "password": None,  # Purge password to block attacker's stub backdoor
                "updated_at": now_iso,
            }
            res = await (
                self.db.table(self.TABLE)
                .update(payload)
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            updated = (res.data[0] if res and res.data else await self.get_by_id(user_id))
            if updated and "password" in updated:
                updated.pop("password", None)
            decrypted = await self._decrypt_user_row(updated)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("CLAIM user completed: user_id=%s in %.2fms", user_id, duration_ms)
            return decrypted
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in claim_unverified_account_with_google user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    async def update_profile(self, user_id: str, req: UserUpdateRequest) -> dict | None:
        """Patch mutable profile fields for a user. Only non-None fields are written."""
        start_time = time.perf_counter()
        logger.debug("UPDATE user profile: user_id=%s", user_id)
        try:
            updates: dict = {}
            current = None
            if req.email is not None or req.country_code is not None or req.mobile_number is not None:
                current = await self.get_by_id(user_id)

            if req.email is not None:
                new_email = req.email.strip().lower()
                updates["email"] = new_email
                current_email = (current.get("email") or "").strip().lower() if current else ""
                if new_email != current_email:
                    updates["email_verified_at"] = None

            dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
            if not dek:
                dek = await self.key_vault.provision_user_dek(user_id, self.db)

            to_encrypt = {}
            if req.country_code is not None:
                to_encrypt["country_code"] = req.country_code
            if req.mobile_number is not None:
                to_encrypt["mobile_number"] = req.mobile_number

            if current and (req.country_code is not None or req.mobile_number is not None):
                curr_code = current.get("country_code")
                curr_num = current.get("mobile_number")
                if (req.country_code is not None and req.country_code != curr_code) or (
                    req.mobile_number is not None and req.mobile_number != curr_num
                ):
                    updates["mobile_verified_at"] = None

            if req.avatar_image_path is not None:
                updates["avatar_image_path"] = req.avatar_image_path
            if req.custom_categories is not None:
                to_encrypt["custom_categories"] = [
                    c.model_dump(by_alias=True) if hasattr(c, "model_dump") else c
                    for c in req.custom_categories
                ]
            if req.preferences is not None:
                to_encrypt["preferences"] = req.preferences

            if to_encrypt:
                enc_fields, _ = self._encrypt_user_fields(to_encrypt, dek)
                updates.update(enc_fields)

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
            decrypted = await self._decrypt_user_row(user_data)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("UPDATE user profile succeeded for user_id=%s in %.2fms", user_id, duration_ms)
            return decrypted
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
            return await self._decrypt_user_row(result)
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
                dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
                if not dek:
                    dek = await self.key_vault.provision_user_dek(user_id, self.db)
                if dek:
                    updates["preferences"] = self.crypto.encrypt_json_with_dek(updated_preferences, dek)
                    updates["enc_version"] = 1
                else:
                    updates["preferences"] = self.crypto.encrypt_json(updated_preferences)

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

    async def update_2fa_status(self, user_id: str, enabled: bool) -> bool:
        """Enable or disable Two-Factor Authentication (2FA) for a user."""
        start_time = time.perf_counter()
        logger.debug("UPDATE user 2FA status: user_id='%s', enabled=%s", user_id, enabled)
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            res = await (
                self.db.table(self.TABLE)
                .update({"is_2fa_enabled": enabled, "updated_at": now_iso})
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            success = bool(res and res.data and len(res.data) > 0)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("UPDATE user 2FA status finished: user_id=%s, enabled=%s in %.2fms", user_id, enabled, duration_ms)
            return success
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in UPDATE user 2FA status for user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
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
            decrypted = await self._decrypt_user_row(user_data)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("set_email_verified succeeded for user_id=%s in %.2fms", user_id, duration_ms)
            return decrypted
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Database error in set_email_verified user_id=%s after %.2fms: %s", user_id, duration_ms, e, exc_info=True)
            raise

    # ── ECONOMIC MODEL: TRIALS, SUBSCRIPTIONS & AD SCANS ──────────────────────

    async def check_device_trial_used(self, device_id: str | None) -> bool:
        """Check whether a client device has actually consumed a reverse trial."""
        if not device_id or not device_id.strip():
            return False
        clean_device = device_id.strip()
        try:
            from src.config import get_settings
            settings = get_settings()
            hashed_device = hashlib.sha256((clean_device + settings.data_encryption_key).encode("utf-8")).hexdigest()

            # 1. Check devices table if device was flagged with trial_consumed_at (raw or tombstoned hash)
            res_dev_trial = await (
                self.db.table("devices")
                .select("id")
                .in_("name", [clean_device, hashed_device])
                .not_.is_("trial_consumed_at", "null")
                .limit(1)
                .execute()
            )
            if res_dev_trial and res_dev_trial.data:
                logger.info("check_device_trial_used: matched devices.trial_consumed_at for device=%s", clean_device)
                return True

            # 2. Legacy fallback: Check active users table preferences for trial_device_id where trial was actually granted.
            #    This covers records created before TODO-19. For all deleted/tombstoned users,
            #    trial_device_id is stripped from preferences during the deletion pipeline.
            #    Pass 1 (devices table) is the authoritative source for tombstoned devices.
            res = await (
                self.db.table(self.TABLE)
                .select("id, tier, preferences")
                .contains("preferences", {"trial_device_id": clean_device})
                .is_("deleted_at", "null")
                .execute()
            )
            if res and res.data:
                for row in res.data:
                    prefs = row.get("preferences") or {}
                    tier = (row.get("tier") or "").lower()
                    if prefs.get("trial_start_at") is not None or prefs.get("is_in_trial") or tier in ("premium", "dev"):
                        logger.info("check_device_trial_used: matched user with consumed trial for device=%s in user_id=%s", clean_device, row.get("id"))
                        return True

            return False
        except Exception as e:
            logger.warning("Error checking device trial consumption for device=%s: %s", clean_device, e)
            return False

    async def check_email_trial_or_purchase_used(self, email: str) -> bool:
        """Check if email has ever been used for a trial or paid tier across all accounts (including soft-deleted)."""
        if not email or not email.strip():
            return False
        clean_email = email.strip().lower()
        try:
            res = await (
                self.db.table(self.TABLE)
                .select("id, tier, preferences")
                .eq("email", clean_email)
                .execute()
            )
            if not res or not res.data:
                # Also check deletion_audit_log for tombstoned/soft-deleted accounts
                email_hash = hashlib.sha256(clean_email.encode("utf-8")).hexdigest()
                audit_res = await (
                    self.db.table("deletion_audit_log")
                    .select("id")
                    .eq("email_hash", email_hash)
                    .limit(1)
                    .execute()
                )
                if audit_res and audit_res.data:
                    logger.info(
                        "check_email_trial_or_purchase_used: matched deletion_audit_log for email_hash=%s",
                        email_hash
                    )
                    return True
                return False
            for row in res.data:
                tier = (row.get("tier") or "").lower()
                prefs = row.get("preferences") or {}
                if tier in ("premium", "dev") or prefs.get("trial_start_at") is not None or prefs.get("is_in_trial"):
                    logger.info(
                        "check_email_trial_or_purchase_used: matched previous trial/purchase for email=%s in user_id=%s",
                        clean_email, row.get("id")
                    )
                    return True
                sub = prefs.get("subscription") or {}
                if sub.get("is_active"):
                    logger.info(
                        "check_email_trial_or_purchase_used: matched active subscription for email=%s in user_id=%s",
                        clean_email, row.get("id")
                    )
                    return True
            return False
        except Exception as e:
            logger.warning("Error checking email trial/purchase history for email=%s: %s", clean_email, e)
            return False

    async def mark_device_trial_consumed(self, device_id: str | None) -> None:
        """Mark a device as having consumed its 14-day trial in the devices table."""
        if not device_id or not device_id.strip():
            return
        clean_device = device_id.strip()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            # Attempt to update existing device record
            res = await (
                self.db.table("devices")
                .update({"trial_consumed_at": now_iso})
                .eq("name", clean_device)
                .execute()
            )
            if not res or not res.data:
                # If device record does not yet exist in devices table, insert it
                await (
                    self.db.table("devices")
                    .insert({
                        "name": clean_device,
                        "device_token_hash": "unregistered_trial_holder",
                        "trial_consumed_at": now_iso,
                    })
                    .execute()
                )
            logger.info("Marked device '%s' as trial consumed at %s", clean_device, now_iso)
        except Exception as e:
            logger.warning("Failed to record trial_consumed_at for device '%s': %s", clean_device, e)

    async def simulate_trial_expiry(self, user_id: str) -> dict | None:
        """Simulate 14-day trial expiration by setting trial_start_at to 15 days ago and applying downgrade."""
        user = await self.get_by_id(user_id)
        if not user:
            return None
        prefs = dict(user.get("preferences") or {})
        past_15_days = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        prefs["trial_start_at"] = past_15_days
        prefs["is_in_trial"] = True

        dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
        if not dek:
            dek = await self.key_vault.provision_user_dek(user_id, self.db)
        enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

        # Update user with past trial start time
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "preferences": enc_prefs,
            "tier": "premium",
            "updated_at": now_iso,
        }
        if dek:
            payload["enc_version"] = 1
        await (
            self.db.table(self.TABLE)
            .update(payload)
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

            prefs = dict(user.get("preferences") or {})
            prefs["trial_start_at"] = iso_ts
            prefs["is_in_trial"] = True
            if device_id:
                clean_dev = device_id.strip()
                prefs["trial_device_id"] = clean_dev
                await self.mark_device_trial_consumed(clean_dev)

            dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
            if not dek:
                dek = await self.key_vault.provision_user_dek(user_id, self.db)
            enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

            payload = {
                "tier": "premium",
                "preferences": enc_prefs,
                "updated_at": iso_ts,
            }
            if dek:
                payload["enc_version"] = 1

            res = await (
                self.db.table(self.TABLE)
                .update(payload)
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            if not res.data:
                return None
            user_data = res.data[0]
            user_data.pop("password", None)
            decrypted = await self._decrypt_user_row(user_data)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("Reverse trial granted (14 days) for user_id=%s in %.2fms", user_id, duration_ms)
            return decrypted
        except Exception as e:
            logger.error("Failed to set trial start for user_id=%s: %s", user_id, e, exc_info=True)
            raise

    async def grant_free_trial(self, user_id: str, device_id: str | None = None) -> dict | None:
        """Grant free reverse trial to user (alias for set_trial_start)."""
        return await self.set_trial_start(user_id, device_id=device_id)

    async def consume_device_trial_in_preferences(self, user_id: str, device_id: str) -> dict | None:
        """Record device trial consumption in user preferences."""
        user = await self.get_by_id(user_id)
        if not user:
            return None
        prefs = dict(user.get("preferences") or {})
        clean_dev = device_id.strip()
        prefs["trial_device_id"] = clean_dev
        prefs["trial_consumed"] = True

        dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
        if not dek:
            dek = await self.key_vault.provision_user_dek(user_id, self.db)
        enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

        payload = {"preferences": enc_prefs, "updated_at": datetime.now(timezone.utc).isoformat()}
        if dek:
            payload["enc_version"] = 1

        res = await self.db.table(self.TABLE).update(payload).eq("id", user_id).is_("deleted_at", "null").execute()
        if not res.data:
            return None
        user_data = res.data[0]
        user_data.pop("password", None)
        return await self._decrypt_user_row(user_data)

    async def record_email_verification_trial_consumed(self, user_id: str, email: str | None = None) -> dict | None:
        """Record trial consumption upon email verification in user preferences."""
        user = await self.get_by_id(user_id)
        if not user:
            return None
        prefs = dict(user.get("preferences") or {})
        prefs["email_trial_consumed"] = True
        prefs["trial_granted"] = True
        prefs.pop("trial_pending_verification", None)

        dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
        if not dek:
            dek = await self.key_vault.provision_user_dek(user_id, self.db)
        enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

        payload = {"preferences": enc_prefs, "updated_at": datetime.now(timezone.utc).isoformat()}
        if dek:
            payload["enc_version"] = 1

        res = await self.db.table(self.TABLE).update(payload).eq("id", user_id).is_("deleted_at", "null").execute()
        if not res.data:
            return None
        user_data = res.data[0]
        user_data.pop("password", None)
        return await self._decrypt_user_row(user_data)

    async def record_trial_device_ineligible(self, user_id: str, reason: str = "device_or_email_already_used") -> dict | None:
        """Mark user preferences as ineligible for trial."""
        user = await self.get_by_id(user_id)
        if not user:
            return None
        prefs = dict(user.get("preferences") or {})
        prefs["trial_pending_verification"] = False
        prefs["trial_ineligible"] = True
        prefs["trial_ineligible_reason"] = reason

        dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
        if not dek:
            dek = await self.key_vault.provision_user_dek(user_id, self.db)
        enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

        payload = {"preferences": enc_prefs, "updated_at": datetime.now(timezone.utc).isoformat()}
        if dek:
            payload["enc_version"] = 1

        res = await self.db.table(self.TABLE).update(payload).eq("id", user_id).is_("deleted_at", "null").execute()
        if not res.data:
            return None
        user_data = res.data[0]
        user_data.pop("password", None)
        return await self._decrypt_user_row(user_data)

    async def evaluate_and_apply_deferred_trial(self, user_id: str, email: str, user_data: dict) -> dict:
        """Evaluate trial eligibility upon email verification and either grant reverse trial or mark ineligible."""
        prefs = user_data.get("preferences") or {}
        if not prefs.get("trial_pending_verification") or prefs.get("trial_ineligible") or prefs.get("trial_start_at"):
            return user_data

        device_id = prefs.get("trial_device_id")
        device_used = await self.check_device_trial_used(device_id) if device_id else False
        email_used = await self.check_email_trial_or_purchase_used(email)

        if not device_used and not email_used:
            logger.info("evaluate_and_apply_deferred_trial: Granting 14-day trial for user_id=%s", user_id)
            updated = await self.set_trial_start(user_id, device_id=device_id)
            if updated:
                prefs2 = dict(updated.get("preferences") or {})
                prefs2.pop("trial_pending_verification", None)
                prefs2["trial_granted"] = True
                dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
                if not dek:
                    dek = await self.key_vault.provision_user_dek(user_id, self.db)
                enc_prefs2 = self.crypto.encrypt_json_with_dek(prefs2, dek) if dek else self.crypto.encrypt_json(prefs2)
                await self.db.table(self.TABLE).update({"preferences": enc_prefs2, "tier": "premium"}).eq("id", user_id).execute()
                return await self.get_by_id(user_id) or updated
            return user_data

        logger.info("evaluate_and_apply_deferred_trial: User %s ineligible (device_used=%s, email_used=%s)", user_id, device_used, email_used)
        prefs_fail = dict(prefs)
        prefs_fail["trial_pending_verification"] = False
        prefs_fail["trial_ineligible"] = True
        prefs_fail["trial_ineligible_reason"] = "device_or_email_already_used"
        dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
        if not dek:
            dek = await self.key_vault.provision_user_dek(user_id, self.db)
        enc_prefs_fail = self.crypto.encrypt_json_with_dek(prefs_fail, dek) if dek else self.crypto.encrypt_json(prefs_fail)
        await self.db.table(self.TABLE).update({"preferences": enc_prefs_fail, "tier": "free"}).eq("id", user_id).execute()
        return await self.get_by_id(user_id) or user_data

    async def set_tier(self, user_id: str, tier: str, updated_preferences: dict | None = None) -> dict | None:
        """Update the user's subscription tier ('free', 'premium', 'dev') and preferences."""
        start_time = time.perf_counter()
        clean_tier = tier.strip().lower()
        now = datetime.now(timezone.utc).isoformat()
        try:
            user = await self.get_by_id(user_id)
            if not user:
                return None

            prefs = dict(user.get("preferences") or {})
            if updated_preferences:
                prefs.update(updated_preferences)

            if clean_tier == "premium":
                prefs["is_in_trial"] = False
                prefs["discount_offer_claimed"] = True
                prefs["discount_offer_shown_at"] = None
            else:
                prefs["is_in_trial"] = False

            dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
            if not dek:
                dek = await self.key_vault.provision_user_dek(user_id, self.db)

            enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

            payload = {
                "tier": clean_tier,
                "preferences": enc_prefs,
                "updated_at": now,
            }
            if dek:
                payload["enc_version"] = 1

            res = await (
                self.db.table(self.TABLE)
                .update(payload)
                .eq("id", user_id)
                .is_("deleted_at", "null")
                .execute()
            )
            if not res.data:
                return None
            user_data = res.data[0]
            user_data.pop("password", None)
            decrypted = await self._decrypt_user_row(user_data)
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info("Updated tier to '%s' for user_id=%s in %.2fms", clean_tier, user_id, duration_ms)
            return decrypted
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

            prefs = dict(user.get("preferences") or {})
            # Guard: User must have had a trial, must not be trial ineligible, and must not have already claimed discount
            if not prefs.get("trial_start_at") or prefs.get("trial_ineligible", False) or prefs.get("discount_offer_claimed", False):
                logger.info("Skipping discount offer for ineligible user_id=%s", user_id)
                return user

            if not prefs.get("discount_offer_shown_at"):
                prefs["discount_offer_shown_at"] = now.isoformat()
                dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
                if not dek:
                    dek = await self.key_vault.provision_user_dek(user_id, self.db)
                enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

                res = await (
                    self.db.table(self.TABLE)
                    .update({"preferences": enc_prefs, "updated_at": now.isoformat()})
                    .eq("id", user_id)
                    .is_("deleted_at", "null")
                    .execute()
                )
                if res.data:
                    user_data = res.data[0]
                    user_data.pop("password", None)
                    return await self._decrypt_user_row(user_data)
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

            prefs = dict(user.get("preferences") or {})
            saved_date = prefs.get("ad_scans_date")
            ad_scans_today = prefs.get("ad_scans_today", 0)

            if saved_date != today_str:
                ad_scans_today = 0

            if ad_scans_today >= 5:
                return False, ad_scans_today, "Daily ad scan limit reached (5/5). Resets at 00:00 UTC."

            ad_scans_today += 1
            prefs["ad_scans_today"] = ad_scans_today
            prefs["ad_scans_date"] = today_str

            dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
            if not dek:
                dek = await self.key_vault.provision_user_dek(user_id, self.db)
            enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

            await (
                self.db.table(self.TABLE)
                .update({"preferences": enc_prefs, "updated_at": now.isoformat()})
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
        prefs = dict(user.get("preferences") or {})
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
                user_id = str(user.get("id") or "")
                logger.info("Trial expired for user_id=%s (started %s). Downgrading to Free.", user_id, trial_start_str)
                prefs["is_in_trial"] = False
                prefs["trial_expired_at"] = now.isoformat()
                if not prefs.get("discount_offer_shown_at"):
                    prefs["discount_offer_shown_at"] = now.isoformat()

                dek = await self.key_vault.get_user_dek_or_none(user_id, self.db)
                if not dek:
                    dek = await self.key_vault.provision_user_dek(user_id, self.db)
                enc_prefs = self.crypto.encrypt_json_with_dek(prefs, dek) if dek else self.crypto.encrypt_json(prefs)

                res = await (
                    self.db.table(self.TABLE)
                    .update({
                        "tier": "free",
                        "preferences": enc_prefs,
                        "updated_at": now.isoformat(),
                    })
                    .eq("id", user_id)
                    .is_("deleted_at", "null")
                    .execute()
                )
                if res.data:
                    updated = res.data[0]
                    updated.pop("password", None)
                    return await self._decrypt_user_row(updated)
        except Exception as e:
            logger.warning("Failed evaluating trial expiration for user_id=%s: %s", user.get("id"), e)

        return user
