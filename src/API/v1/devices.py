from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_current_identity
from src.Models.schemas import (
    DeviceRegisterRequest,
    DeviceLinkRequest,
    DeviceRecord,
)
from src.Models.Devices.device_repository import DeviceRepository

router = APIRouter(prefix="/devices", tags=["Devices"])


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> DeviceRepository:
    return DeviceRepository(db)


# ── POST /devices/register ────────────────────────────────────────────────────
# NOTE: This is an unauthenticated bootstrap endpoint — the device cannot send
# X-Device-Token before registration, so we do not apply get_current_identity here.
@router.post("/register", response_model=DeviceRecord, status_code=201)
async def register_device(
    body: DeviceRegisterRequest,
    repo: DeviceRepository = Depends(get_repo),
):
    """Register a new device hardware ID and fingerprint token, or refresh an existing one.

    Idempotent — calling with the same device_id updates the token and user association.
    This endpoint is intentionally public (no auth dependency) because the device
    needs to bootstrap itself before it can present a valid X-Device-Token.
    """
    device = await repo.register_or_update(body)
    if not device:
        raise HTTPException(
            status_code=401,
            detail="Invalid device token for existing device_id.",
        )
    return device


# ── GET /devices/me ───────────────────────────────────────────────────────────
# NOTE: Must be declared BEFORE /{device_id} to prevent FastAPI treating
# the literal string "me" as a device_id path parameter.
@router.get("/me", response_model=DeviceRecord)
async def get_my_device(
    identity: Identity = Depends(get_current_identity),
    repo: DeviceRepository = Depends(get_repo),
):
    """Retrieve the device record for the calling device's X-Device-ID.

    Requires valid X-Device-ID and X-Device-Token headers.
    """
    device = await repo.get_by_device_id(identity.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device


# ── POST /devices/link ────────────────────────────────────────────────────────
@router.post("/link", response_model=DeviceRecord)
async def link_device_user(
    body: DeviceLinkRequest,
    identity: Identity = Depends(get_current_identity),
    repo: DeviceRepository = Depends(get_repo),
):
    """Link a device to a user account, or pass user_id=null to unlink (guest mode).

    Requires valid X-Device-ID and X-Device-Token headers.
    Enforces that body.device_id matches identity.device_id to prevent cross-device hijacking.
    """
    if body.device_id.strip() != identity.device_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot modify link status for another device_id.",
        )

    device = await repo.link_user(body.device_id, body.device_token, body.user_id)
    if not device:
        raise HTTPException(
            status_code=401,
            detail="Device not registered or invalid device token.",
        )
    return device


# ── DELETE /devices/me ────────────────────────────────────────────────────────
@router.delete("/me", status_code=200)
async def delete_my_device(
    identity: Identity = Depends(get_current_identity),
    repo: DeviceRepository = Depends(get_repo),
):
    """Soft-delete calling device registration record.

    Requires valid X-Device-ID and X-Device-Token headers.
    """
    deleted = await repo.soft_delete(identity.device_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Device record not found or already deleted.",
        )
    return {"success": True, "device_id": identity.device_id}
