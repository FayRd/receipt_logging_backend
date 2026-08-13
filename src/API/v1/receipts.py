from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_user_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import (
    ReceiptRecord,
    ReceiptCreateRequest,
    ReceiptBatchCreateRequest,
)
from src.Models.Receipts.receipt_repository import ReceiptRepository

router = APIRouter(prefix="/receipts", tags=["Receipts"])
logger = get_logger("API.receipts")


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> ReceiptRepository:
    return ReceiptRepository(db)


# ── GET all receipts for calling identity ────────────────────────────────────
@router.get(
    "/",
    response_model=list[ReceiptRecord],
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def list_receipts(
    updated_after: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
):
    """Get all non-deleted receipts owned by the caller's authenticated user identity.

    Requires X-User-Name and X-User-Token headers.
    Supports optional delta syncing via `updated_after` (ISO 8601 timestamp) to fetch
    only receipts modified after a given point in time. Supports pagination via
    `limit` (number of records) and `offset` (starting position).
    Results are ordered by updated_at DESC.
    """
    logger.debug(
        "Entering list_receipts: updated_after=%s, limit=%s, offset=%s, identity (user_id=%s, username=%s)",
        updated_after,
        limit,
        offset,
        identity.user_id,
        identity.user_name,
    )
    records = await repo.get_all_by_identity(
        identity,
        updated_after=updated_after,
        limit=limit,
        offset=offset,
    )
    logger.info("list_receipts fetched %d records for user_id=%s", len(records), identity.user_id)
    return records


# ── GET single receipt ────────────────────────────────────────────────────────
@router.get(
    "/{receipt_id}",
    response_model=ReceiptRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_receipt(
    receipt_id: str,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
):
    """Get a single receipt by ID. Requires X-User-Name and X-User-Token headers."""
    logger.debug("Entering get_receipt: receipt_id=%s, identity (user_id=%s)", receipt_id, identity.user_id)
    data = await repo.get_by_id(receipt_id, identity)
    if not data:
        logger.warning("Receipt not found: receipt_id=%s, user_id=%s", receipt_id, identity.user_id)
        raise HTTPException(status_code=404, detail="Receipt not found")
    logger.info("get_receipt successful: receipt_id=%s, user_id=%s", receipt_id, identity.user_id)
    return data


# ── CREATE single receipt ─────────────────────────────────────────────────────
@router.post(
    "/create",
    response_model=ReceiptRecord,
    status_code=201,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def create_receipt(
    body: ReceiptCreateRequest,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
):
    """Create a single receipt bound to the caller's authenticated user identity."""
    logger.debug(
        "Entering create_receipt: merchant=%s, total_amount=%s, identity (user_id=%s)",
        body.receipt.merchant_name,
        body.receipt.total_amount,
        identity.user_id,
    )
    record = await repo.create(identity, body.receipt)
    logger.info(
        "Receipt created successfully: receipt_id=%s, merchant=%s, user_id=%s",
        record.id,
        record.merchant_name,
        identity.user_id,
    )
    return record


# ── CREATE batch receipts ─────────────────────────────────────────────────────
@router.post(
    "/create/batch",
    response_model=list[ReceiptRecord],
    status_code=201,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def create_receipts_batch(
    body: ReceiptBatchCreateRequest,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
):
    """Batch-create up to 100 receipts bound to the caller's authenticated user identity."""
    logger.debug(
        "Entering create_receipts_batch: batch_count=%d, identity (user_id=%s)",
        len(body.receipts),
        identity.user_id,
    )
    records = await repo.create_batch(identity, body.receipts)
    logger.info(
        "Batch receipts created successfully: created_count=%d, user_id=%s",
        len(records),
        identity.user_id,
    )
    return records


# ── SOFT DELETE receipt ───────────────────────────────────────────────────────
@router.delete(
    "/{receipt_id}",
    status_code=200,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def delete_receipt(
    receipt_id: str,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
):
    """Soft-delete a receipt owned by the caller's authenticated user identity."""
    logger.debug("Entering delete_receipt: receipt_id=%s, identity (user_id=%s)", receipt_id, identity.user_id)
    deleted = await repo.soft_delete(receipt_id, identity)
    if not deleted:
        logger.warning(
            "Delete receipt failed - not found or already deleted: receipt_id=%s, user_id=%s",
            receipt_id,
            identity.user_id,
        )
        raise HTTPException(
            status_code=404,
            detail="Receipt not found or already deleted",
        )
    logger.info("Receipt soft-deleted successfully: receipt_id=%s, user_id=%s", receipt_id, identity.user_id)
    return {"success": True, "receipt_id": receipt_id}

