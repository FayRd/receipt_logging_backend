import secrets
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Models.Devices.device_repository import DeviceRepository


class Identity(BaseModel):
    """Container for caller's cryptographically verified session identity."""
    user_id: str | None = None
    device_id: str

    @property
    def is_authenticated(self) -> bool:
        """True when the request carries a verified user session."""
        return self.user_id is not None


async def get_current_identity(
    x_device_id: str = Header(
        ...,
        alias="X-Device-ID",
        description="Mobile hardware device identifier string.",
    ),
    x_device_token: str = Header(
        ...,
        alias="X-Device-Token",
        description="Device secret fingerprint token generated on first app boot.",
    ),
    x_user_id: str | None = Header(
        None,
        alias="X-User-ID",
        description="Authenticated user UUID — supplied only when signed in.",
    ),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency that parses and cryptographically verifies the caller's identity.

    Verification sequence:
    1. Validate that X-Device-ID and X-Device-Token headers are non-empty.
    2. Look up the device row in Supabase.
    3. Compare X-Device-Token to the stored device_token using secrets.compare_digest
       (constant-time comparison, immune to timing attacks).
    4. Return Identity scoped to user (if X-User-ID present) or guest device.

    Raises HTTP 400 if required headers are missing/empty.
    Raises HTTP 401 if device is unregistered or token does not match.
    """
    clean_device_id = x_device_id.strip()
    clean_device_token = x_device_token.strip()

    if not clean_device_id or not clean_device_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Device-ID and X-Device-Token headers are required and cannot be empty.",
        )

    # Look up device row in DB
    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(clean_device_id)

    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device. Please call POST /api/v1/devices/register first.",
        )

    # Constant-time comparison guards against timing-based token guessing attacks
    stored_token_bytes = device["device_token"].encode("utf-8")
    incoming_token_bytes = clean_device_token.encode("utf-8")

    if not secrets.compare_digest(incoming_token_bytes, stored_token_bytes):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )

    # Prefer X-User-ID header; fall back to user_id linked at device registration
    clean_user_id = x_user_id.strip() if x_user_id and x_user_id.strip() else None
    effective_user_id = clean_user_id or device.get("user_id")

    return Identity(user_id=effective_user_id, device_id=clean_device_id)


async def require_user_identity(
    identity: Identity = Depends(get_current_identity),
) -> Identity:
    """FastAPI dependency for routes that REQUIRE a fully authenticated user session.

    Use this instead of get_current_identity on routes where guest access is not allowed.
    Raises HTTP 401 if the caller is an anonymous guest device.
    """
    if not identity.is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in to access this resource.",
        )
    return identity
