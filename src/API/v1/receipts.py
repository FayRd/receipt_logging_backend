from fastapi import APIRouter, Depends, HTTPException
from supabase import Client
from src.Infrastructure.database import get_supabase_client
from src.Models.schemas import (
    ReceiptRecord,
    ReceiptCreateRequest,
    ReceiptBatchCreateRequest,
)
from src.Models.Receipts.receipt_repository import ReceiptRepository

router = APIRouter(prefix="/receipts", tags=["Receipts"])


def get_repo(db: Client = Depends(get_supabase_client)) -> ReceiptRepository:
    return ReceiptRepository(db)


# ── GET all receipts for a user ───────────────────────────────────────────────
@router.get("/user/{user_id}", response_model=list[ReceiptRecord])
async def list_user_receipts(
    user_id: str,
    repo: ReceiptRepository = Depends(get_repo),
):
    """Get all non-deleted receipts owned by user_id, newest first."""
    data = repo.get_all_by_user(user_id)
    return data


# ── GET single receipt ────────────────────────────────────────────────────────
@router.get("/{receipt_id}", response_model=ReceiptRecord)
async def get_receipt(
    receipt_id: str,
    user_id: str,
    repo: ReceiptRepository = Depends(get_repo),
):
    """Get a single receipt by ID. Returns 404 if not found or not owned by user_id."""
    data = repo.get_by_id(receipt_id, user_id)
    if not data:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return data


# ── CREATE single receipt ─────────────────────────────────────────────────────
@router.post("/", response_model=ReceiptRecord, status_code=201)
async def create_receipt(
    body: ReceiptCreateRequest,
    repo: ReceiptRepository = Depends(get_repo),
):
    """Create a single receipt record associated with the owner's user_id."""
    data = repo.create(body.user_id, body.device_id, body.receipt)
    return data


# ── CREATE batch receipts ─────────────────────────────────────────────────────
@router.post("/batch", response_model=list[ReceiptRecord], status_code=201)
async def create_receipts_batch(
    body: ReceiptBatchCreateRequest,
    repo: ReceiptRepository = Depends(get_repo),
):
    """Batch-create up to 100 receipts for the same user in a single DB call."""
    data = repo.create_batch(body.user_id, body.device_id, body.receipts)
    return data


# ── SOFT DELETE receipt ───────────────────────────────────────────────────────
@router.delete("/{receipt_id}", status_code=200)
async def delete_receipt(
    receipt_id: str,
    user_id: str,
    repo: ReceiptRepository = Depends(get_repo),
):
    """Soft-delete a receipt by setting deleted_at. Only the owner can delete it."""
    deleted = repo.soft_delete(receipt_id, user_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Receipt not found or already deleted",
        )
    return {"success": True, "receipt_id": receipt_id}
