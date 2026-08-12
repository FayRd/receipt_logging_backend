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
    device_id: str = ""      # DB UUID or device name
    device_name: str = ""    # e.g. MS701-A1B1

    @property
    def is_authenticated(self) -> bool:
        """True when the request carries a verified user session."""
        return self.user_id is not None


# ── 1. DEVICE SCOPED IDENTITY (/devices/me, /scan/*) ──────────────────────────
async def get_device_identity(
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
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for device-scoped routes (/devices/me, /scan/*).

    Requires X-Device-Name and X-Device-Token headers. Omits X-User-Name and X-User-Token.
    """
    clean_device_name = x_device_name.strip()
    clean_device_token = x_device_token.strip()

    if not clean_device_name or not clean_device_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Device-Name and X-Device-Token headers are required and cannot be empty.",
        )

    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(clean_device_name)

    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device. Please call POST /api/v1/devices/register first.",
        )

    incoming_hash = hash_device_token(clean_device_token)
    stored_hash = device.get("device_token_hash", "")

    if not secrets.compare_digest(incoming_hash.encode("utf-8"), stored_hash.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )

    db_user_id = device.get("user_id")
    db_username = None
    if db_user_id:
        user_repo = UserRepository(db)
        user_row = await user_repo.get_by_id(db_user_id)
        if user_row:
            db_username = user_row.get("username")

    canonical_name = device.get("name", clean_device_name)
    return Identity(
        user_id=db_user_id,
        username=db_username,
        device_id=device["id"],
        device_name=canonical_name,
    )


# Alias for backward compatibility on device endpoints
get_current_identity = get_device_identity


# ── 2. USER AUTHENTICATED IDENTITY (/user/*, /receipts/*, /chat/*) ─────────────
async def get_user_identity(
    x_user_name: str = Header(
        ...,
        alias="X-User-Name",
        description="Authenticated username string.",
    ),
    x_user_token: str = Header(
        ...,
        alias="X-User-Token",
        description="Authenticated user password hash token string.",
    ),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for user-scoped routes (/user/*, /receipts/*, /chat/*).

    Requires X-User-Name and X-User-Token headers. Omits X-Device-Name and X-Device-Token.
    Verifies user credentials against stored password hash in constant time.
    """
    clean_username = x_user_name.strip()
    clean_user_token = x_user_token.strip()

    if not clean_username or not clean_user_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-User-Name and X-User-Token headers are required and cannot be empty.",
        )

    user_repo = UserRepository(db)
    user = await user_repo.get_by_identifier(clean_username)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found or invalid credentials.",
        )

    stored_password_hash = user.get("password", "")
    incoming_user_hash = UserRepository.hash_password(clean_user_token)

    if not secrets.compare_digest(incoming_user_hash.encode("utf-8"), stored_password_hash.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user authentication token.",
        )

    return Identity(
        user_id=user["id"],
        username=user["username"],
        device_id="",
        device_name="",
    )


# Alias for backward compatibility on user-authenticated routes
require_user_identity = get_user_identity


# ── 3. LINK BRIDGE IDENTITY (POST /devices/link) ──────────────────────────────
async def require_link_bridge_identity(
    x_device_name: str = Header(..., alias="X-Device-Name"),
    x_device_token: str = Header(..., alias="X-Device-Token"),
    x_user_name: str = Header(..., alias="X-User-Name"),
    x_user_token: str = Header(..., alias="X-User-Token"),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for POST /devices/link enforcing ALL FOUR headers:

    X-Device-Name, X-Device-Token, X-User-Name, X-User-Token.
    Verifies both device token and user credentials before allowing link modification.
    """
    clean_device_name = x_device_name.strip()
    clean_device_token = x_device_token.strip()
    clean_username = x_user_name.strip()
    clean_user_token = x_user_token.strip()

    if not clean_device_name or not clean_device_token or not clean_username or not clean_user_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="All four headers (X-Device-Name, X-Device-Token, X-User-Name, X-User-Token) are required for POST /devices/link.",
        )

    # 1. Verify Device Identity
    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(clean_device_name)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device.",
        )

    incoming_device_hash = hash_device_token(clean_device_token)
    stored_device_hash = device.get("device_token_hash", "")
    if not secrets.compare_digest(incoming_device_hash.encode("utf-8"), stored_device_hash.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )

    # 2. Verify User Identity
    user_repo = UserRepository(db)
    user = await user_repo.get_by_identifier(clean_username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found or invalid credentials.",
        )

    stored_password_hash = user.get("password", "")
    incoming_user_hash = UserRepository.hash_password(clean_user_token)
    if not secrets.compare_digest(incoming_user_hash.encode("utf-8"), stored_password_hash.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user authentication token.",
        )

    canonical_name = device.get("name", clean_device_name)
    return Identity(
        user_id=user["id"],
        username=user["username"],
        device_id=device["id"],
        device_name=canonical_name,
    )


# Alias for device link route dependency
require_link_identity = require_link_bridge_identity


# ── 4. SSE STREAMING IDENTITY ──────────────────────────────────────────────────
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
    return Identity(user_id=db_user_id, username=username, device_id=device["id"], device_name=canonical_name)
