from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
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
@router.post("/register", response_model=DeviceRecord, status_code=201)
async def register_device(
    body: DeviceRegisterRequest,
    repo: DeviceRepository = Depends(get_repo),
):
    """Register a new device hardware ID or update its user association.

    Idempotent — calling with the same device_id a second time updates the
    user_id if it changed, or returns the existing record unchanged.
    """
    device = await repo.register_or_update(body)
    return device


# ── GET /devices/user/{user_id} ───────────────────────────────────────────────
# NOTE: This route must be declared BEFORE /{device_id} to avoid FastAPI
# interpreting the literal string "user" as a device_id path parameter.
@router.get("/user/{user_id}", response_model=list[DeviceRecord])
async def list_user_devices(
    user_id: str,
    repo: DeviceRepository = Depends(get_repo),
):
    """List all active devices associated with a specific user UUID."""
    devices = await repo.get_by_user_id(user_id)
    return devices


# ── POST /devices/link ────────────────────────────────────────────────────────
@router.post("/link", response_model=DeviceRecord)
async def link_device_user(
    body: DeviceLinkRequest,
    repo: DeviceRepository = Depends(get_repo),
):
    """Link a device to a user account, or pass user_id=null to unlink (guest mode)."""
    device = await repo.link_user(body.device_id, body.user_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not registered.")
    return device


# ── GET /devices/{device_id} ──────────────────────────────────────────────────
@router.get("/{device_id}", response_model=DeviceRecord)
async def get_device(
    device_id: str,
    repo: DeviceRepository = Depends(get_repo),
):
    """Retrieve device registration record by hardware device_id string."""
    device = await repo.get_by_device_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device
