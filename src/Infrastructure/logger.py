# src/Infrastructure/logger.py
import hashlib
import logging
import os
import re
from typing import Any
import uuid
from contextvars import ContextVar
from src.config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(req_id: str | None = None) -> str:
    """Sets a correlation request ID into ContextVar context."""
    val = req_id or f"req-{uuid.uuid4().hex[:8]}"
    request_id_var.set(val)
    return val


def get_request_id() -> str:
    """Gets the active correlation request ID from ContextVar."""
    return request_id_var.get()


class CorrelationIdFilter(logging.Filter):
    """Logging filter that injects `request_id` into log record formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


# ── COMPILED REGEX PATTERNS (PRE-WARMED ON MODULE LOAD) ──────────────────────────

# RFC 5322-compliant email pattern
_EMAIL_REGEX = re.compile(
    r"(?i)\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
)

# JWT Bearer tokens and raw compact JWTs (header.payload.signature)
_JWT_REGEX = re.compile(
    r"(?i)\b(?:Bearer\s+)?ey[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_.+/=]+\b"
)

# Passwords, secrets, and credentials in JSON, query strings, or key-value formats
_CREDENTIAL_KEY_REGEX = re.compile(
    r"""(?i)(["']?(?:password|passwd|pwd|secret|client_secret|auth_token|access_token|refresh_token)["']?\s*[:=]\s*["'])([^"',\s]+)(["']?)"""
)

# API Keys (Google AIza..., OpenAI sk-..., Supabase sbp_..., generic hex/base64 keys)
_API_KEY_REGEX = re.compile(
    r"\b(?:sk-[a-zA-Z0-9]{20,}|AIza[0-9A-Za-z-_]{35}|sbp_[a-zA-Z0-9]{40,}|key-[a-zA-Z0-9]{32,})\b"
)

# Phone Numbers: Singapore (+65 8/9xxx xxxx) and International E.164 (+1 to +999)
_PHONE_REGEX = re.compile(
    r"(?:\+?65[ -]?)?[689]\d{3}[ -]?\d{4}\b|(?<!\w)\+[1-9]\d{1,14}\b"
)

# Payment Cards (13 to 19-digit Luhn-eligible patterns)
_CREDIT_CARD_REGEX = re.compile(
    r"\b(?:\d{4}[ -]?){3}\d{4}\b"
)

# Fast-path detection heuristic: skip regex matching on purely benign messages.
# Expanded with 'phone', 'mobile', 'tel', and 'sms' (supervisor finding ADV-31-01),
# plus card, pan, pay, credit, cvv for payment data detection.
_SENSITIVE_HEURISTIC_SUBSTRINGS = (
    "@", "bearer", "eyj", "password", "secret", "token", "key", "+", "aiza", "sk-",
    "phone", "mobile", "tel", "sms", "card", "pan", "pay", "credit", "cvv"
)

# Sensitive dictionary keys for direct dictionary scrubbing
_SENSITIVE_KEY_NAMES = {
    "password", "passwd", "pwd", "secret", "token", "access_token", "refresh_token",
    "auth_token", "authorization", "auth_header", "auth", "api_key", "client_secret",
    "private_key", "jwt", "credentials", "credit_card", "card_number", "cvv"
}


def sanitize_text(text: str) -> str:
    """Applies compiled regex redaction suite to a string."""
    if not text or not isinstance(text, str):
        return text

    # Fast-path check: avoid regex overhead if no markers exist
    text_lower = text.lower()
    if not any(sub in text_lower for sub in _SENSITIVE_HEURISTIC_SUBSTRINGS):
        return text

    # 1. Redact credentials/passwords in key-value pairs
    text = _CREDENTIAL_KEY_REGEX.sub(r"\1***REDACTED***\3", text)
    # 2. Redact JWTs and Bearer tokens
    text = _JWT_REGEX.sub("Bearer ***REDACTED_JWT***", text)
    # 3. Redact API keys
    text = _API_KEY_REGEX.sub("***REDACTED_API_KEY***", text)
    # 4. Redact Email addresses
    text = _EMAIL_REGEX.sub("***REDACTED_EMAIL***", text)
    # 5. Redact Credit Card PANs
    text = _CREDIT_CARD_REGEX.sub("***REDACTED_CARD***", text)
    # 6. Redact Phone numbers
    text = _PHONE_REGEX.sub("***REDACTED_PHONE***", text)

    return text


def sanitize_object(obj: Any, seen: set[int] | None = None) -> Any:
    """Recursively sanitizes dictionary, list, and tuple objects, handling cycles."""
    if seen is None:
        seen = set()

    obj_id = id(obj)
    if obj_id in seen:
        return "[CIRCULAR_REFERENCE]"

    if isinstance(obj, str):
        return sanitize_text(obj)
    elif isinstance(obj, dict):
        seen.add(obj_id)
        try:
            sanitized_dict = {}
            for k, v in obj.items():
                k_str = str(k).lower()
                if any(sens in k_str for sens in _SENSITIVE_KEY_NAMES):
                    sanitized_dict[k] = "***REDACTED***"
                else:
                    sanitized_dict[k] = sanitize_object(v, seen)
            return sanitized_dict
        finally:
            seen.remove(obj_id)
    elif isinstance(obj, (list, tuple, set)):
        seen.add(obj_id)
        try:
            sanitized_seq = [sanitize_object(item, seen) for item in obj]
            return type(obj)(sanitized_seq) if not isinstance(obj, set) else set(sanitized_seq)
        finally:
            seen.remove(obj_id)
    elif hasattr(obj, "__dict__") and not isinstance(obj, type):
        # Handle custom dataclasses or models
        return sanitize_text(repr(obj))

    return obj


class PIIFilter(logging.Filter):
    """Logging filter that scrubs sensitive PII, credentials, and tokens from all LogRecords."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # 1. Format and redact the log message
            if record.args:
                try:
                    formatted_msg = record.getMessage()
                    record.msg = sanitize_text(formatted_msg)
                    record.args = ()  # Clear args so formatters do not re-interpolate
                except Exception:
                    # Fallback if getMessage() fails formatting
                    record.msg = sanitize_text(str(record.msg))
                    if isinstance(record.args, tuple):
                        record.args = tuple(sanitize_object(a) for a in record.args)
                    elif isinstance(record.args, dict):
                        record.args = sanitize_object(record.args)
            elif isinstance(record.msg, str):
                record.msg = sanitize_text(record.msg)
            else:
                record.msg = sanitize_object(record.msg)

            # 2. Redact exception text if present, or pre-format exception traceback
            if record.exc_text:
                record.exc_text = sanitize_text(record.exc_text)
            elif record.exc_info:
                try:
                    record.exc_text = sanitize_text(logging.Formatter().formatException(record.exc_info))
                except Exception:
                    pass

            # 3. Redact custom extra dictionary attributes on the LogRecord
            # Standard attributes defined on LogRecord are skipped
            _STANDARD_RECORD_ATTRS = {
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "message", "asctime", "request_id"
            }
            for attr_name, attr_val in list(record.__dict__.items()):
                if attr_name not in _STANDARD_RECORD_ATTRS and not attr_name.startswith("_"):
                    setattr(record, attr_name, sanitize_object(attr_val))

        except Exception as exc:
            # Defensive logging safety: never allow the filter to raise an uncaught exception
            try:
                record.msg = f"[LOG REDACTION EXCEPTION: {exc}] {record.msg}"
            except Exception:
                record.msg = f"[LOG REDACTION EXCEPTION: {exc}] [content suppressed]"

        return True


# ── AUDIT MASKING HELPERS ────────────────────────────────────────────────────────

def mask_email(email: str | None) -> str:
    """Masks email address for audit logging: 'john.doe@example.com' -> 'j***e@example.com'."""
    if not email or "@" not in email:
        return "[INVALID_EMAIL]"
    parts = email.strip().split("@", 1)
    user, domain = parts[0], parts[1]
    if len(user) <= 2:
        masked_user = user[0] + "***" if user else "***"
    else:
        masked_user = user[0] + "***" + user[-1]
    return f"{masked_user}@{domain}"


def mask_phone(phone: str | None) -> str:
    """Masks phone number: '+6591234567' -> '+65 **** 4567'."""
    if not phone:
        return "[NO_PHONE]"
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if len(cleaned) < 6:
        return "****"
    return f"{cleaned[:3]} **** {cleaned[-4:]}"


def mask_identifier(identifier: str | None) -> str:
    """Intelligently masks an arbitrary identifier (email, phone, or username)."""
    if not identifier:
        return "-"
    ident = identifier.strip()
    if "@" in ident:
        return mask_email(ident)
    if re.search(r"^\+?\d{7,15}$", ident):
        return mask_phone(ident)
    if len(ident) <= 3:
        return ident[0] + "**"
    return ident[:2] + "***" + ident[-1]


def hash_audit_id(identifier: str | None, salt: str = "") -> str:
    """Creates a deterministic, non-reversible SHA-256 audit fingerprint."""
    if not identifier:
        return "-"
    payload = f"{identifier.strip().lower()}:{salt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def setup_logging() -> None:
    """Configures centralized togglable file and console logging."""
    settings = get_settings()

    env = (settings.environment or "development").lower().strip()
    configured_level = (settings.log_level or "INFO").upper().strip()

    # Enforce strict runtime guardrail against DEBUG level in staging/production
    if env in ("production", "staging") and configured_level == "DEBUG":
        level = logging.INFO
    else:
        level = getattr(logging, configured_level, logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(milliSeconds)03d [%(levelname)s] [%(request_id)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Custom Formatter subclass to compute milliSeconds string correctly
    class PrecisionFormatter(logging.Formatter):
        def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
            ct = self.converter(record.created)
            t = logging.Formatter.formatTime(self, record, datefmt)
            msecs = int(record.msecs)
            return f"{t}.{msecs:03d}"

    precision_formatter = PrecisionFormatter(
        fmt="%(asctime)s [%(levelname)s] [%(request_id)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger("receipt_api")
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.filters.clear()

    corr_filter = CorrelationIdFilter()
    pii_filter = PIIFilter()
    root_logger.addFilter(corr_filter)
    root_logger.addFilter(pii_filter)

    # File Handler
    if settings.enable_file_logging:
        log_file = os.path.abspath(settings.log_file_path)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(precision_formatter)
        file_handler.addFilter(corr_filter)
        file_handler.addFilter(pii_filter)
        root_logger.addHandler(file_handler)

    # Console Handler
    if settings.enable_console_logging:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(precision_formatter)
        console_handler.addFilter(corr_filter)
        console_handler.addFilter(pii_filter)
        root_logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """Returns a logger instance prefixed with receipt_api."""
    return logging.getLogger(f"receipt_api.{name}")

