from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_device_identity, require_link_bridge_identity
from src.Auth.rate_limiter import rate_limit
from src.Models.schemas import (
    DeviceRegisterRequest,
    DeviceLinkRequest,
    DeviceTokenRotateRequest,
    DeviceRecord,
)
from src.Models.Devices.device_repository import DeviceRepository

router = APIRouter(prefix="/devices", tags=["Devices"])


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> DeviceRepository:
    return DeviceRepository(db)


# ── POST /devices/register ────────────────────────────────────────────────────
@router.post(
    "/register",
    response_model=DeviceRecord,
    status_code=201,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def register_device(
    body: DeviceRegisterRequest,
    repo: DeviceRepository = Depends(get_repo),
):
    """Register a new device hardware/variant name and fingerprint token, or refresh an existing one.

    Public route — no authentication headers required.
    """
    device = await repo.register_or_update(body)
    if not device:
        raise HTTPException(
            status_code=401,
            detail="Invalid device token for existing device_name.",
        )
    return device


# ── GET /devices/me ───────────────────────────────────────────────────────────
@router.get(
    "/me",
    response_model=DeviceRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_my_device(
    identity: Identity = Depends(get_device_identity),
    repo: DeviceRepository = Depends(get_repo),
):
    """Retrieve the device record for the calling device's X-Device-Name.

    Requires valid X-Device-Name and X-Device-Token headers. Omits X-User-Name and X-User-Token.
    """
    device = await repo.get_by_device_id(identity.device_name)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device


# ── POST /devices/link ────────────────────────────────────────────────────────
@router.post(
    "/link",
    response_model=DeviceRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def link_device_user(
    body: DeviceLinkRequest,
    identity: Identity = Depends(require_link_bridge_identity),
    repo: DeviceRepository = Depends(get_repo),
):
    """Link a device to a user account by username, or pass username=null to unlink (guest mode).

    Requires ALL FOUR authentication headers: X-Device-Name, X-Device-Token, X-User-Name, X-User-Token.
    Enforces that body.device_name matches identity.device_name to prevent cross-device hijacking.
    """
    target_device = await repo.get_by_device_id(body.device_name)
    if not target_device or target_device["name"] != identity.device_name:
        raise HTTPException(
            status_code=403,
            detail="Cannot modify link status for another device_name.",
        )

    device = await repo.link_user_by_names(body.device_name, body.username)
    if not device:
        raise HTTPException(
            status_code=401,
            detail="Device not registered or invalid user credentials.",
        )
    return device


# ── DELETE /devices/me ────────────────────────────────────────────────────────
@router.delete(
    "/me",
    status_code=200,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def delete_my_device(
    identity: Identity = Depends(get_device_identity),
    repo: DeviceRepository = Depends(get_repo),
):
    """Soft-delete calling device registration record.

    Requires valid X-Device-Name and X-Device-Token headers. Omits user headers.
    """
    deleted = await repo.soft_delete(identity.device_name)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Device record not found or already deleted.",
        )
    return {"success": True, "device_name": identity.device_name}


# ── POST /devices/rotate-token ────────────────────────────────────────────────
@router.post(
    "/rotate-token",
    response_model=DeviceRecord,
    status_code=200,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def rotate_device_token_endpoint(
    body: DeviceTokenRotateRequest,
    identity: Identity = Depends(get_device_identity),
    repo: DeviceRepository = Depends(get_repo),
):
    """Rotate secret device_token for the calling hardware device.

    Requires valid X-Device-Name and X-Device-Token (current token) headers.
    Updates stored device_token_hash to new_device_token and returns updated record.
    """
    device = await repo.rotate_device_token(identity.device_name, body.new_device_token)
    if not device:
        raise HTTPException(status_code=404, detail="Device record not found.")
    return device
