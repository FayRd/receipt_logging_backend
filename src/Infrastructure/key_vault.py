import base64
import os
import secrets
from datetime import datetime, timezone
from typing import Any
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from supabase import AsyncClient

from src.Infrastructure.logger import get_logger
from src.Infrastructure.crypto import CryptoEngine, DecryptionError
from src.config import get_settings

logger = get_logger("Infrastructure.key_vault")


class KeyVaultError(Exception):
    """Base exception for KeyVault errors."""
    pass


class KeyNotFoundError(KeyVaultError, KeyError):
    """Raised when a user's DEK is not found in user_keys."""
    pass


class KeyVault:
    """KeyVault Service managing Envelope Encryption with Per-User Data Encryption Keys (DEKs).

    The existing DATA_ENCRYPTION_KEY serves as the master Key Encryption Key (KEK).
    Per-user DEKs are 256-bit AES keys generated at user creation, wrapped
    by the KEK using AES-256-GCM, and stored in the user_keys table.
    Crypto-shredding is achieved by deleting the user's row from user_keys.
    """

    TABLE = "user_keys"
    CURRENT_KEK_VERSION = 1

    def __init__(self, kek: str | bytes | None = None):
        if kek is None:
            settings = get_settings()
            kek = settings.data_encryption_key
        self._kek_bytes = CryptoEngine._resolve_key_bytes(kek)
        self._aesgcm = AESGCM(self._kek_bytes)
        self._cache: dict[str, bytes] = {}
        logger.debug("KeyVault initialized successfully with 256-bit AESGCM KEK")

    def generate_dek(self) -> bytes:
        """Generate a cryptographically secure 256-bit (32 bytes) DEK via os.urandom(32)."""
        return os.urandom(32)

    def wrap_dek(self, dek: bytes) -> str:
        """Wrap (encrypt) a 32-byte DEK using the KEK (AES-256-GCM).

        Returns base64-encoded envelope: IV(12) + Ciphertext(32) + Tag(16) = 60 bytes.
        """
        if not isinstance(dek, (bytes, bytearray)):
            raise TypeError(f"DEK must be bytes, got {type(dek)}")
        if len(dek) != 32:
            raise ValueError(f"DEK must be exactly 32 bytes. Got {len(dek)} bytes.")
        iv = os.urandom(12)
        ct_and_tag = self._aesgcm.encrypt(iv, bytes(dek), None)
        return base64.b64encode(iv + ct_and_tag).decode("ascii")

    def unwrap_dek(self, encrypted_dek_b64: str) -> bytes:
        """Unwrap (decrypt) an encrypted DEK using the KEK."""
        if not encrypted_dek_b64 or not isinstance(encrypted_dek_b64, str):
            raise ValueError("encrypted_dek_b64 must be a non-empty string.")
        try:
            raw = base64.b64decode(encrypted_dek_b64.strip())
        except Exception as exc:
            raise ValueError(f"Base64 decoding failed for wrapped DEK: {exc}") from exc

        if len(raw) < 28:
            raise ValueError(f"Invalid encrypted DEK envelope length ({len(raw)} bytes).")
        iv = raw[:12]
        ct_and_tag = raw[12:]
        try:
            dek = self._aesgcm.decrypt(iv, ct_and_tag, None)
        except InvalidTag as exc:
            logger.error("DEK unwrapping failed: Invalid authentication tag")
            raise DecryptionError("Failed to unwrap DEK: Invalid authentication tag") from exc

        if len(dek) != 32:
            raise ValueError(f"Unwrapped DEK has invalid length ({len(dek)} bytes, expected 32).")
        return dek

    async def get_user_dek(self, user_id: str, db: AsyncClient) -> bytes:
        """Fetch and unwrap the user's DEK from the user_keys table.

        Raises KeyNotFoundError if key is not found.
        """
        uid = str(user_id).strip()
        if not uid:
            raise ValueError("user_id cannot be empty.")

        if uid in self._cache:
            return self._cache[uid]

        res = (
            await db.table(self.TABLE)
            .select("encrypted_dek, kek_version")
            .eq("user_id", uid)
            .maybe_single()
            .execute()
        )
        if not res or not res.data or not res.data.get("encrypted_dek"):
            raise KeyNotFoundError(f"No DEK found for user_id={uid}")

        dek = self.unwrap_dek(res.data["encrypted_dek"])
        self._cache[uid] = dek
        return dek

    async def get_user_dek_or_none(self, user_id: str | None, db: AsyncClient) -> bytes | None:
        """Fetch user's DEK, returning None if user_id is empty or key not found."""
        if not user_id:
            return None
        try:
            return await self.get_user_dek(user_id, db)
        except Exception as exc:
            logger.debug("get_user_dek_or_none: user %s has no DEK: %s", user_id, exc)
            return None

    async def provision_user_dek(self, user_id: str, db: AsyncClient) -> bytes:
        """Provision a new DEK for user_id in user_keys if not present (idempotent)."""
        uid = str(user_id).strip()
        if not uid:
            raise ValueError("user_id cannot be empty.")

        existing = await self.get_user_dek_or_none(uid, db)
        if existing is not None:
            return existing

        dek = self.generate_dek()
        wrapped = self.wrap_dek(dek)
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "user_id": uid,
            "encrypted_dek": wrapped,
            "kek_version": self.CURRENT_KEK_VERSION,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await db.table(self.TABLE).insert(row).execute()
            self._cache[uid] = dek
            logger.info("provision_user_dek: provisioned new DEK for user %s", uid)
        except Exception as e:
            err_str = str(e).lower()
            if "23505" in err_str or "duplicate" in err_str or "unique" in err_str:
                logger.info("provision_user_dek: user %s already provisioned (concurrent race resolved)", uid)
                existing = await self.get_user_dek_or_none(uid, db)
                if existing is not None:
                    return existing
            elif "23503" in err_str or "foreign key" in err_str:
                logger.info("provision_user_dek: user %s not in auth.users, syncing stub row", uid)
                try:
                    await db.auth.admin.create_user({"id": uid, "email": f"{uid}@app.internal"})
                    await db.table(self.TABLE).insert(row).execute()
                    self._cache[uid] = dek
                    logger.info("provision_user_dek: provisioned new DEK for user %s after auth.users sync", uid)
                    return dek
                except Exception as auth_err:
                    logger.warning("auth.users sync fallback failed for user %s: %s", uid, auth_err)
            logger.error("Insert into user_keys failed for %s: %s", uid, e)
            raise e
        return dek

    async def destroy_user_dek(self, user_id: str, db: AsyncClient) -> bool:
        """Crypto-shred: Delete the user's DEK from user_keys table.

        Renders all data encrypted with this user's DEK permanently unrecoverable.
        """
        uid = str(user_id).strip()
        if not uid:
            return False

        self._cache.pop(uid, None)
        try:
            res = await db.table(self.TABLE).delete().eq("user_id", uid).execute()
            deleted = bool(res and res.data and len(res.data) > 0)
            logger.info("Destroyed DEK for user %s from user_keys (deleted=%s)", uid, deleted)
            try:
                await db.auth.admin.delete_user(uid)
            except Exception:
                pass
            return True if deleted or (res is not None) else False
        except Exception as e:
            logger.error("Error destroying DEK for user %s: %s", uid, e)
            return False

    def clear_cache(self, user_id: str | None = None) -> None:
        """Clear the in-memory DEK cache."""
        if user_id:
            self._cache.pop(str(user_id).strip(), None)
        else:
            self._cache.clear()


_key_vault: KeyVault | None = None


def get_key_vault() -> KeyVault:
    global _key_vault
    if _key_vault is None:
        _key_vault = KeyVault()
    return _key_vault


key_vault = get_key_vault()

