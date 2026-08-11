import secrets
from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Models.Devices.device_repository import DeviceRepository
from src.Models.Users.user_repository import UserRepository
from src.Auth.device_security import hash_device_token


class Identity(BaseModel):
    """Container for caller's cryptographically verified session identity."""
    user_id: str | None = None
    username: str | None = None
    device_id: str  # Device name string, e.g. MS701-A1B1
    device_name: str  # Device name string, e.g. MS701-A1B1

    @property
    def is_authenticated(self) -> bool:
        """True when the request carries a verified user session."""
        return self.user_id is not None


async def get_current_identity(
    x_device_name: str = Header(
        ...,
        alias="X-Device-Name",
        description="Mobile hardware/variant device name identifier string.",
    ),
    x_device_token: str = Header(
        ...,
        alias="X-Device-Token",
        description="Device secret fingerprint token generated on first app boot.",
    ),
    x_user_name: str | None = Header(
        None,
        alias="X-User-Name",
        description="Authenticated username string — supplied when signed in.",
    ),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency that parses and cryptographically verifies the caller's identity.

    Verification sequence:
    1. Validate that X-Device-Name and X-Device-Token headers are non-empty.
    2. Look up the device row in Supabase by name (or UUID id).
    3. Hash X-Device-Token with HMAC-SHA256 and compare against stored device_token_hash.
    4. Validate optional X-User-Name against linked DB user session.
    """
    clean_device_name = x_device_name.strip()
    clean_device_token = x_device_token.strip()

    if not clean_device_name or not clean_device_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Device-Name and X-Device-Token headers are required and cannot be empty.",
        )

    # Look up device row in DB (supports devices.name or UUID devices.id)
    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(clean_device_name)

    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device. Please call POST /api/v1/devices/register first.",
        )

    # Hash incoming plaintext token using HMAC-SHA256
    incoming_hash = hash_device_token(clean_device_token)
    stored_hash = device.get("device_token_hash", "")

    # Constant-time comparison guards against timing-based token guessing attacks
    if not secrets.compare_digest(incoming_hash.encode("utf-8"), stored_hash.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )

    # Derive user_id strictly from database mapping (ground truth)
    db_user_id = device.get("user_id")
    db_username = None

    if db_user_id:
        user_repo = UserRepository(db)
        user_row = await user_repo.get_by_id(db_user_id)
        if user_row:
            db_username = user_row.get("username")

    # If client passed X-User-Name header, verify it matches DB ground truth
    clean_user_name = x_user_name.strip() if x_user_name and x_user_name.strip() else None
    if clean_user_name and db_username and clean_user_name.lower() != db_username.lower():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header X-User-Name does not match authenticated user session for this device.",
        )

    canonical_name = device.get("name", clean_device_name)
    return Identity(
        user_id=db_user_id,
        username=db_username,
        device_id=canonical_name,
        device_name=canonical_name,
    )


async def require_user_identity(
    x_user_name: str | None = Header(
        None,
        alias="X-User-Name",
        description="Authenticated username string — required for user-scoped endpoints.",
    ),
    identity: Identity = Depends(get_current_identity),
) -> Identity:
    """FastAPI dependency for routes that REQUIRE a fully authenticated user session AND explicit X-User-Name header.

    Raises HTTP 401 if X-User-Name header is missing/empty or if caller is an anonymous guest device.
    """
    clean_user_name = x_user_name.strip() if x_user_name and x_user_name.strip() else None
    if not clean_user_name:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header X-User-Name is required for user-authenticated endpoints.",
        )

    if not identity.is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in to access this resource.",
        )
    return identity


async def require_link_identity(
    x_user_name: str = Header(
        ...,
        alias="X-User-Name",
        description="Authenticated username string — required for POST /devices/link.",
    ),
    identity: Identity = Depends(get_current_identity),
) -> Identity:
    """FastAPI dependency for POST /devices/link enforcing that X-User-Name is explicitly provided in headers."""
    clean_user_name = x_user_name.strip() if x_user_name and x_user_name.strip() else None
    if not clean_user_name:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header X-User-Name is required for device linking.",
        )
    return identity


async def get_sse_identity(
    request: Request,
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for SSE endpoints supporting both HTTP Headers and URL Query Parameters."""
    device_name = (request.headers.get("X-Device-Name") or request.headers.get("X-Device-ID") or request.query_params.get("device_name") or request.query_params.get("device_id") or "").strip()
    device_token = (request.headers.get("X-Device-Token") or request.query_params.get("device_token") or "").strip()
    username = (request.headers.get("X-User-Name") or request.headers.get("X-User-ID") or request.query_params.get("username") or request.query_params.get("user_id") or "").strip() or None

    if not device_name or not device_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device authentication required via headers (X-Device-Name, X-Device-Token) or query parameters.",
        )

    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(device_name)

    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device.",
        )

    incoming_hash = hash_device_token(device_token)
    stored_hash = device.get("device_token_hash", "")

    if not secrets.compare_digest(incoming_hash.encode("utf-8"), stored_hash.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )

    db_user_id = device.get("user_id")
    canonical_name = device.get("name", device_name)
    return Identity(user_id=db_user_id, username=username, device_id=canonical_name, device_name=canonical_name)
