from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_device_identity, require_link_bridge_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import (
    DeviceRegisterRequest,
    DeviceLinkRequest,
    DeviceTokenRotateRequest,
    DeviceRecord,
)
from src.Models.Devices.device_repository import DeviceRepository
from src.Services.data_migration_service import DataMigrationService

router = APIRouter(prefix="/devices", tags=["Devices"])
logger = get_logger("API.devices")


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
    logger.debug("Entering register_device: device_name=%s", body.device_name)
    device = await repo.register_or_update(body)
    if not device:
        logger.warning("Device registration/refresh failed: Invalid token for device_name=%s", body.device_name)
        raise HTTPException(
            status_code=401,
            detail="Invalid device token for existing device_name.",
        )
    logger.info("Device registered/updated successfully: device_id=%s, device_name=%s", device.get("id"), body.device_name)
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
    logger.debug("Entering get_my_device: device_name=%s", identity.device_name)
    device = await repo.get_by_device_id(identity.device_name)
    if not device:
        logger.warning("Device not found for device_name=%s", identity.device_name)
        raise HTTPException(status_code=404, detail="Device not found.")
    logger.info("Retrieved device record: device_id=%s, device_name=%s", device.get("id"), identity.device_name)
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
    db: AsyncClient = Depends(get_supabase_client),
):
    """Link a device to a user account by username, or pass username=null to unlink (guest mode).

    Requires X-Device-Name and X-Device-Token headers. When linking (username provided) also
    requires X-User-Name and X-User-Token headers.
    Enforces that body.device_name matches identity.device_name to prevent cross-device hijacking.
    If migrate_data is provided on linking, bulk-migrates guest receipts/conversations/chat_messages
    into Supabase associated with the newly linked user_id.
    """
    logger.debug(
        "Entering link_device_user: device_name=%s, target_username=%s, identity (device_name=%s, user_id=%s)",
        body.device_name,
        body.username,
        identity.device_name,
        identity.user_id,
    )
    target_device = await repo.get_by_device_id(body.device_name)
    if not target_device or target_device["name"] != identity.device_name:
        logger.warning(
            "Device link forbidden: target device_name '%s' does not match identity device_name '%s'",
            body.device_name,
            identity.device_name,
        )
        raise HTTPException(
            status_code=403,
            detail="Cannot modify link status for another device_name.",
        )

    device = await repo.link_user_by_names(body.device_name, body.username)
    if not device:
        logger.warning("Device link failed: Invalid credentials or unregistered device_name '%s'", body.device_name)
        raise HTTPException(
            status_code=401,
            detail="Device not registered or invalid user credentials.",
        )

    # Migrate local guest data into Supabase when linking to a user account
    target_user_id = device.get("user_id") or identity.user_id
    if body.username and body.migrate_data and target_user_id:
        payload = (
            body.migrate_data.model_dump()
            if hasattr(body.migrate_data, "model_dump")
            else body.migrate_data
        )
        if isinstance(payload, dict):
            logger.info("Migrating guest data for user_id=%s, device_name=%s", target_user_id, body.device_name)
            await DataMigrationService.migrate_user_data(
                db=db,
                user_id=target_user_id,
                device_name=body.device_name,
                migrate_data=payload,
            )

    logger.info("Device linked successfully: device_name=%s, username=%s", body.device_name, body.username)
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
    logger.debug("Entering delete_my_device: device_name=%s", identity.device_name)
    deleted = await repo.soft_delete(identity.device_name)
    if not deleted:
        logger.warning("Soft-delete device failed: Device record not found or already deleted for device_name=%s", identity.device_name)
        raise HTTPException(
            status_code=404,
            detail="Device record not found or already deleted.",
        )
    logger.info("Device soft-deleted successfully: device_name=%s", identity.device_name)
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
    logger.debug("Entering rotate_device_token_endpoint: device_name=%s", identity.device_name)
    device = await repo.rotate_device_token(identity.device_name, body.new_device_token)
    if not device:
        logger.warning("Token rotation failed: Device record not found for device_name=%s", identity.device_name)
        raise HTTPException(status_code=404, detail="Device record not found.")
    logger.info("Device token rotated successfully for device_name=%s", identity.device_name)
    return device

