# File: src/Models/Users/password_reset_repository.py

import hashlib
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from supabase import AsyncClient
from postgrest.exceptions import APIError


class PasswordResetRepository:
    TABLE = "forget_password"
    USERS_TABLE = "users"
    _OTP_SALT = "RL_OTP_Salt_2026"
    _TOKEN_SALT = "RL_ResetToken_Salt_2026"

    # Memory fallback store if Supabase forget_password table is pending manual migration
    _memory_store: list[dict] = []

    def __init__(self, db: AsyncClient):
        self.db = db

    def hash_otp(self, otp: str) -> str:
        """Hash numeric OTP with static salt using SHA-256."""
        salted = f"{otp}:{self._OTP_SALT}".encode("utf-8")
        return hashlib.sha256(salted).hexdigest()

    def hash_token(self, token: str) -> str:
        """Hash single-use reset token with static salt using SHA-256."""
        salted = f"{token}:{self._TOKEN_SALT}".encode("utf-8")
        return hashlib.sha256(salted).hexdigest()

    async def create_reset_request(
        self,
        user_id: str,
        email: str | None,
        mobile_number: str | None,
        otp: str,
    ) -> dict:
        """Invalidate previous unexpired requests and insert a new hashed OTP request."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=10)
        hashed_otp = self.hash_otp(otp)

        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "email": email,
            "mobile_number": mobile_number,
            "otp_hash": hashed_otp,
            "reset_token_hash": None,
            "attempts_count": 0,
            "is_used": False,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        try:
            # Invalidate any existing unused reset requests for this user in DB
            await (
                self.db.table(self.TABLE)
                .update({"is_used": True})
                .eq("user_id", user_id)
                .eq("is_used", False)
                .execute()
            )
            res = await self.db.table(self.TABLE).insert(row).execute()
            return res.data[0] if res.data else row
        except Exception as err:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            err_msg = f"[{now_str}] ❌ [DB ERROR] Supabase insert into 'forget_password' failed: {err}\n"
            print(err_msg.strip())
            try:
                with open("otp_dev.log", "a", encoding="utf-8") as f:
                    f.write(err_msg)
            except Exception:
                pass

            # Fallback to memory store if database table is pending migration or RLS policy restricts insertion
            for item in self._memory_store:
                if item["user_id"] == user_id:
                    item["is_used"] = True
            self._memory_store.append(row)
            return row

    async def verify_otp(self, user_id: str, otp: str) -> tuple[bool, str, str | None]:
        """Validate OTP for a user_id.

        Returns (success: bool, message: str, reset_token: str | None).
        """
        now = datetime.now(timezone.utc).isoformat()
        record = None

        try:
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("user_id", user_id)
                .eq("is_used", False)
                .gt("expires_at", now)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                record = res.data[0]
        except Exception:
            for item in reversed(self._memory_store):
                if item["user_id"] == user_id and not item["is_used"] and item["expires_at"] > now:
                    record = item
                    break

        if not record:
            return False, "Invalid or expired reset code. Please request a new code.", None

        attempts = record.get("attempts_count", 0)

        if attempts >= 5:
            record["is_used"] = True
            try:
                await (
                    self.db.table(self.TABLE)
                    .update({"is_used": True})
                    .eq("id", record["id"])
                    .execute()
                )
            except Exception:
                pass
            return False, "Too many failed attempts. Please request a new code.", None

        target_hash = self.hash_otp(otp.strip())
        if not secrets.compare_digest(record["otp_hash"], target_hash):
            new_attempts = attempts + 1
            is_exhausted = new_attempts >= 5
            record["attempts_count"] = new_attempts
            record["is_used"] = is_exhausted

            try:
                await (
                    self.db.table(self.TABLE)
                    .update({
                        "attempts_count": new_attempts,
                        "is_used": is_exhausted,
                    })
                    .eq("id", record["id"])
                    .execute()
                )
            except Exception:
                pass

            if is_exhausted:
                return False, "Too many failed attempts. Please request a new code.", None
            return False, f"Invalid reset code. {5 - new_attempts} attempts remaining.", None

        # OTP match! Generate single-use reset_token
        reset_token = f"rst_{secrets.token_hex(16)}"
        hashed_token = self.hash_token(reset_token)

        record["reset_token_hash"] = hashed_token

        try:
            await (
                self.db.table(self.TABLE)
                .update({"reset_token_hash": hashed_token})
                .eq("id", record["id"])
                .execute()
            )
        except Exception:
            pass

        return True, "OTP verified successfully.", reset_token

    async def complete_reset(
        self,
        reset_token: str,
        new_password_hash: str,
    ) -> tuple[bool, str]:
        """Validate reset_token, update user password in users table, and mark reset request used.

        Returns (success: bool, message: str).
        """
        now = datetime.now(timezone.utc).isoformat()
        hashed_token = self.hash_token(reset_token.strip())

        record = None

        try:
            res = await (
                self.db.table(self.TABLE)
                .select("*")
                .eq("reset_token_hash", hashed_token)
                .eq("is_used", False)
                .gt("expires_at", now)
                .maybe_single()
                .execute()
            )
            if res and res.data:
                record = res.data
        except Exception:
            for item in self._memory_store:
                if item.get("reset_token_hash") == hashed_token and not item["is_used"] and item["expires_at"] > now:
                    record = item
                    break

        if not record:
            return False, "Invalid or expired reset token. Please request a new password reset."

        user_id = record["user_id"]

        # Update user's password in users table
        await (
            self.db.table(self.USERS_TABLE)
            .update({"password": new_password_hash})
            .eq("id", user_id)
            .execute()
        )

        record["is_used"] = True

        try:
            await (
                self.db.table(self.TABLE)
                .update({"is_used": True})
                .eq("id", record["id"])
                .execute()
            )
        except Exception:
            pass

        return True, "Password reset successfully. You can now log in."
