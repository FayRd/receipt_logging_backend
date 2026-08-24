import base64
import json
import os
from typing import Any
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from src.Infrastructure.logger import get_logger
from src.config import get_settings

logger = get_logger("Infrastructure.crypto")


class CryptoEngine:
    """Cryptographic Engine for backend authenticated data encryption at rest (AES-256-GCM).

    Provides:
    - `encrypt_text` / `decrypt_text` for string columns (titles, chat messages)
      with the `enc:v1:<iv_b64>:<tag_b64>:<ct_b64>` envelope format.
    - `encrypt_json` / `decrypt_json` for JSONB dictionaries (receipt payloads)
      with the `{"_enc": "v1", "iv": "...", "tag": "...", "data": "..."}` JSON envelope format.
    - `encrypt_bytes` / `decrypt_bytes` for raw byte arrays.
    - Transparent backward-compatible fallback for unencrypted legacy data.
    """

    ENVELOPE_VERSION = "v1"
    TEXT_PREFIX = "enc:v1:"
    BYTES_PREFIX = b"ENC:V1:"

    def __init__(self, key: str | bytes | None = None):
        if key is None:
            settings = get_settings()
            key = settings.data_encryption_key

        self._key_bytes = self._resolve_key_bytes(key)
        self._aesgcm = AESGCM(self._key_bytes)
        logger.debug("CryptoEngine initialized successfully with 256-bit AESGCM key")

    @staticmethod
    def _resolve_key_bytes(key: str | bytes) -> bytes:
        """Decode base64 or raw 32-byte key into exact 32 bytes."""
        if not key:
            raise ValueError("Data encryption key cannot be empty.")

        if isinstance(key, str):
            # Try Base64 decoding first
            try:
                decoded = base64.b64decode(key.strip())
                if len(decoded) >= 32:
                    return decoded[:32]
            except Exception:
                pass

            # Fallback to UTF-8 encoded string if at least 32 bytes
            raw = key.strip().encode("utf-8")
            if len(raw) >= 32:
                return raw[:32]
            raise ValueError(
                f"Data encryption key must be at least 32 bytes (256 bits). "
                f"Got string of length {len(key)}."
            )

        if isinstance(key, (bytes, bytearray)):
            if len(key) >= 32:
                return bytes(key[:32])
            try:
                decoded = base64.b64decode(key)
                if len(decoded) >= 32:
                    return decoded[:32]
            except Exception:
                pass
            raise ValueError(f"Data encryption key must be at least 32 bytes. Got {len(key)} bytes.")

        raise TypeError(f"Invalid key type: {type(key)}")

    # ── TEXT ENCRYPTION (enc:v1:<iv>:<tag>:<ct>) ─────────────────────────────

    def encrypt_text(self, plaintext: str, aad: bytes | None = None) -> str:
        """Encrypt plaintext string using AES-256-GCM.

        Returns string envelope: `enc:v1:<iv_b64>:<tag_b64>:<ct_b64>`
        """
        if plaintext is None:
            return ""

        iv = os.urandom(12)
        pt_bytes = plaintext.encode("utf-8")
        ct_and_tag = self._aesgcm.encrypt(iv, pt_bytes, aad)

        # In cryptography AESGCM, the last 16 bytes are the authentication tag
        ciphertext = ct_and_tag[:-16]
        tag = ct_and_tag[-16:]

        iv_b64 = base64.b64encode(iv).decode("ascii")
        tag_b64 = base64.b64encode(tag).decode("ascii")
        ct_b64 = base64.b64encode(ciphertext).decode("ascii")

        return f"enc:v1:{iv_b64}:{tag_b64}:{ct_b64}"

    def decrypt_text(self, envelope: str, aad: bytes | None = None) -> str:
        """Decrypt string envelope.

        If `envelope` does not start with `enc:v1:`, returns it as-is for backward compatibility.
        Raises `InvalidTag` if encrypted data has been tampered with.
        """
        if not isinstance(envelope, str):
            return envelope

        if not envelope.startswith(self.TEXT_PREFIX):
            # Backward-compatible fallback for unencrypted legacy string
            return envelope

        parts = envelope.split(":")
        if len(parts) != 5 or parts[0] != "enc" or parts[1] != "v1":
            raise ValueError(f"Invalid encrypted text envelope format: {envelope}")

        try:
            iv = base64.b64decode(parts[2])
            tag = base64.b64decode(parts[3])
            ciphertext = base64.b64decode(parts[4])
        except Exception as exc:
            raise ValueError(f"Base64 decoding failed for text envelope: {exc}") from exc

        ct_and_tag = ciphertext + tag
        pt_bytes = self._aesgcm.decrypt(iv, ct_and_tag, aad)
        return pt_bytes.decode("utf-8")

    # ── JSON / DICT ENCRYPTION ({"_enc": "v1", ...}) ────────────────────────

    def encrypt_json(self, data: dict[str, Any], aad: bytes | None = None) -> dict[str, str]:
        """Encrypt dictionary payload using AES-256-GCM.

        Returns JSON envelope dict: `{"_enc": "v1", "iv": "...", "tag": "...", "data": "..."}`
        """
        if not isinstance(data, dict):
            return data

        if data.get("_enc") == self.ENVELOPE_VERSION:
            # Already encrypted
            return data

        json_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        iv = os.urandom(12)
        ct_and_tag = self._aesgcm.encrypt(iv, json_bytes, aad)

        ciphertext = ct_and_tag[:-16]
        tag = ct_and_tag[-16:]

        return {
            "_enc": self.ENVELOPE_VERSION,
            "iv": base64.b64encode(iv).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
            "data": base64.b64encode(ciphertext).decode("ascii"),
        }

    def decrypt_json(self, payload: dict | str | None, aad: bytes | None = None) -> dict[str, Any]:
        """Decrypt JSON envelope or dict payload.

        If payload is unencrypted dict or legacy JSON, returns it as-is for backward compatibility.
        Raises `InvalidTag` if encrypted data has been tampered with.
        """
        if payload is None:
            return {}

        if isinstance(payload, str):
            # Check if text envelope
            if payload.startswith(self.TEXT_PREFIX):
                decrypted_str = self.decrypt_text(payload, aad)
                return json.loads(decrypted_str)

            # Try parsing as JSON string
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    return self.decrypt_json(parsed, aad)
                return parsed
            except (json.JSONDecodeError, ValueError):
                return payload

        if isinstance(payload, dict):
            if payload.get("_enc") == self.ENVELOPE_VERSION:
                try:
                    iv = base64.b64decode(payload["iv"])
                    tag = base64.b64decode(payload["tag"])
                    ciphertext = base64.b64decode(payload["data"])
                except Exception as exc:
                    raise ValueError(f"Base64 decoding failed for JSON envelope: {exc}") from exc

                ct_and_tag = ciphertext + tag
                pt_bytes = self._aesgcm.decrypt(iv, ct_and_tag, aad)
                return json.loads(pt_bytes.decode("utf-8"))

            # Legacy unencrypted dictionary
            return payload

        return payload

    # ── BYTES ENCRYPTION ──────────────────────────────────────────────────────

    def encrypt_bytes(self, data: bytes, aad: bytes | None = None) -> bytes:
        """Encrypt raw bytes using AES-256-GCM.

        Output format: b"ENC:V1:" + 12-byte IV + 16-byte Tag + Ciphertext.
        """
        if not data:
            return data
        iv = os.urandom(12)
        ct_and_tag = self._aesgcm.encrypt(iv, data, aad)
        ciphertext = ct_and_tag[:-16]
        tag = ct_and_tag[-16:]
        return self.BYTES_PREFIX + iv + tag + ciphertext

    def decrypt_bytes(self, data: bytes, aad: bytes | None = None) -> bytes:
        """Decrypt raw bytes with backward-compatible legacy binary passthrough.

        If data does not start with b"ENC:V1:", returns data as-is (e.g. legacy JPEG).
        """
        if not data:
            return data

        if not data.startswith(self.BYTES_PREFIX):
            # Backward-compatible fallback for unencrypted legacy image binary
            return data

        prefix_len = len(self.BYTES_PREFIX)
        if len(data) < prefix_len + 28:
            raise ValueError("Encrypted byte payload too short (minimum 28 bytes for IV + Tag).")

        iv = data[prefix_len : prefix_len + 12]
        tag = data[prefix_len + 12 : prefix_len + 28]
        ciphertext = data[prefix_len + 28 :]
        ct_and_tag = ciphertext + tag
        return self._aesgcm.decrypt(iv, ct_and_tag, aad)


_crypto_engine: CryptoEngine | None = None


def get_crypto_engine() -> CryptoEngine:
    """Return the singleton CryptoEngine instance."""
    global _crypto_engine
    if _crypto_engine is None:
        _crypto_engine = CryptoEngine()
    return _crypto_engine
