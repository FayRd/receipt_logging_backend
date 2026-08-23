import json
import re
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_user_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import (
    Receipt,
    ReceiptRecord,
    ReceiptCreateRequest,
    ReceiptBatchCreateRequest,
    ReceiptUpdateRequest,
)
from src.Models.Receipts.receipt_repository import ReceiptRepository
from src.Services.image_service import ImageStorageService, validate_image_size
from src.config import get_settings

router = APIRouter(prefix="/receipts", tags=["Receipts"])
logger = get_logger("API.receipts")

_settings = get_settings()


def _clean_str(val: object | None) -> str | None:
    """Normalize empty string or null string representations to None."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == "null" or s.lower() == "undefined":
        return None
    return s


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> ReceiptRepository:
    return ReceiptRepository(db)


async def get_image_storage(db: AsyncClient = Depends(get_supabase_client)) -> ImageStorageService:
    return ImageStorageService(db, bucket=_settings.supabase_user_data_bucket)


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
        identity.username,
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


# ── GET receipt image ─────────────────────────────────────────────────────────
@router.get(
    "/{receipt_id}/image",
    summary="Download a receipt's image JPEG binary",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_receipt_image(
    receipt_id: str,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
    image_storage: ImageStorageService = Depends(get_image_storage),
):
    """Retrieve the JPEG image binary for a receipt owned by the caller."""
    logger.debug("Entering get_receipt_image: receipt_id=%s, identity (user_id=%s)", receipt_id, identity.user_id)
    record = await repo.get_by_id(receipt_id, identity)
    if not record:
        raise HTTPException(status_code=404, detail="Receipt not found.")

    storage_path = record.get("receipt_image_path")
    if not storage_path:
        raise HTTPException(status_code=404, detail="Receipt has no attached image.")

    data = await image_storage.download_receipt_image(storage_path)
    if not data:
        raise HTTPException(status_code=404, detail="Receipt image file not found in storage.")

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ── CREATE single receipt ─────────────────────────────────────────────────────
@router.post(
    "/create",
    response_model=ReceiptRecord,
    status_code=201,
    summary="Create a single receipt (supports JSON or multipart image upload)",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "receipt_image": {
                                "type": "string",
                                "format": "binary",
                                "description": "Optional receipt image file (JPEG, PNG, WEBP, max 20MB raw). Compressed to <= 5MB.",
                            },
                            "receipt_json": {
                                "type": "string",
                                "description": "Receipt JSON object string, e.g. {\"merchant_name\":\"Starbucks\",\"total_amount\":15.50}",
                            },
                        },
                    }
                },
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/ReceiptCreateRequest"
                    }
                },
            }
        }
    },
)
@router.post(
    "/",
    response_model=ReceiptRecord,
    status_code=201,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
    include_in_schema=False,
)
async def create_receipt(
    request: Request,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
    image_storage: ImageStorageService = Depends(get_image_storage),
):
    """Create a single receipt bound to the caller's authenticated user identity.

    Accepts both:
    - `application/json` body (`ReceiptCreateRequest` or raw `Receipt`).
    - `multipart/form-data` with optional `receipt_image` file and `receipt_json` (or form fields).

    When an image is provided, it is compressed to <= 5MB and stored in Supabase Storage
    at `{user_id}/receipt_images/{receipt_id}.jpg`. The path is saved in `receipt_image_path`.
    """
    content_type = request.headers.get("content-type", "").lower()
    receipt: Receipt
    image_bytes: bytes | None = None
    receipt_image_path: str | None = None

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()

        # Extract image bytes
        image_field = form.get("receipt_image") or form.get("image") or form.get("file")
        if image_field is not None:
            if hasattr(image_field, "read"):
                image_bytes = await image_field.read()
            elif isinstance(image_field, (bytes, bytearray)):
                image_bytes = bytes(image_field)
            elif isinstance(image_field, str) and image_field.strip():
                image_bytes = image_field.encode("utf-8")

        # Extract receipt payload
        receipt_json_str = _clean_str(form.get("receipt_json") or form.get("receipt") or form.get("body"))
        if receipt_json_str:
            try:
                loaded = json.loads(receipt_json_str)
                if isinstance(loaded, dict) and "receipt" in loaded:
                    receipt = Receipt(**loaded["receipt"])
                elif isinstance(loaded, dict):
                    receipt = Receipt(**loaded)
                else:
                    receipt = Receipt()
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"Invalid receipt JSON in form: {exc}") from exc
        else:
            # Fallback: construct Receipt from individual form fields if provided
            merchant = _clean_str(form.get("merchant_name")) or "N/A"
            total = float(form.get("total_amount", 0.0) or 0.0)
            receipt = Receipt(merchant_name=merchant, total_amount=total)
    else:
        # JSON mode
        try:
            body_dict = await request.json()
            if isinstance(body_dict, dict) and "receipt" in body_dict:
                receipt = Receipt(**body_dict["receipt"])
                receipt_image_path = body_dict.get("receipt_image_path")
            elif isinstance(body_dict, dict):
                receipt = Receipt(**body_dict)
                receipt_image_path = body_dict.get("receipt_image_path")
            else:
                raise ValueError("Expected JSON object")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON request body: {exc}") from exc

    logger.debug(
        "Entering create_receipt: merchant=%s, total_amount=%s, identity (user_id=%s)",
        receipt.merchant_name,
        receipt.total_amount,
        identity.user_id,
    )

    # Pre-generate receipt ID so storage path is deterministic
    receipt_id = str(uuid.uuid4())

    if image_bytes and len(image_bytes) > 0:
        validate_image_size(image_bytes, max_bytes=_settings.max_upload_size_bytes)

        user_id = identity.user_id or identity.device_id
        receipt_image_path = await image_storage.upload_receipt_image(
            user_id=user_id,
            receipt_id=receipt_id,
            image_bytes=image_bytes,
            target_max_bytes=_settings.max_compressed_image_bytes,
        )
        logger.info(
            "create_receipt: image uploaded → %s for receipt_id=%s",
            receipt_image_path,
            receipt_id,
        )

    record = await repo.create(
        identity,
        receipt,
        receipt_image_path=receipt_image_path,
        receipt_id=receipt_id,
    )
    logger.info(
        "Receipt created successfully: receipt_id=%s, merchant=%s, user_id=%s",
        record.get("id"),
        receipt.merchant_name,
        identity.user_id,
    )
    return record


# ── CREATE batch receipts ─────────────────────────────────────────────────────
@router.post(
    "/create/batch",
    response_model=list[ReceiptRecord],
    status_code=201,
    summary="Batch-create receipts (supports JSON or multipart files upload, 1 to 100)",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "format": "binary",
                                },
                                "description": "Array of receipt image files (JPEG, PNG, WEBP, max 20MB raw each). Compressed to <= 5MB.",
                            },
                            "receipts_json": {
                                "type": "string",
                                "description": "JSON array of receipt objects, e.g. [{\"merchant_name\":\"A\",\"total_amount\":10.0}]",
                            },
                        },
                        "required": ["receipts_json"],
                    }
                },
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/ReceiptBatchCreateRequest"
                    }
                },
            }
        }
    },
)
@router.post(
    "/batch",
    response_model=list[ReceiptRecord],
    status_code=201,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
    include_in_schema=False,
)
async def create_receipts_batch(
    request: Request,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
    image_storage: ImageStorageService = Depends(get_image_storage),
):
    """Batch-create up to 100 receipts bound to the caller's authenticated user identity.

    Accepts both:
    - `application/json` body (`ReceiptBatchCreateRequest` or list of receipts).
    - `multipart/form-data` with optional `receipt_images` file list and `receipts_json`.

    Images are matched to receipts by filename then by index. Each is compressed to <= 5MB.
    """
    content_type = request.headers.get("content-type", "").lower()
    receipts: list[Receipt] = []
    uploaded_files: list[object] = []

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()

        # Collect uploaded image files
        for key, value in form.multi_items():
            if key in ("receipt_images", "images", "files", "receipt_image") and value is not None:
                uploaded_files.append(value)

        # Parse receipts JSON array
        receipts_json_str = _clean_str(form.get("receipts_json") or form.get("receipts") or form.get("body"))
        if receipts_json_str:
            try:
                raw_list = json.loads(receipts_json_str)
                if isinstance(raw_list, dict) and "receipts" in raw_list:
                    raw_list = raw_list["receipts"]
                if not isinstance(raw_list, list) or len(raw_list) == 0:
                    raise ValueError("Expected a non-empty JSON array.")
                receipts = [Receipt(**item) if isinstance(item, dict) else item for item in raw_list]
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"Invalid receipts_json: {exc}") from exc
        else:
            raise HTTPException(status_code=422, detail="Provide receipts_json in multipart form.")
    else:
        # JSON mode
        try:
            body_dict = await request.json()
            if isinstance(body_dict, dict) and "receipts" in body_dict:
                raw_list = body_dict["receipts"]
            elif isinstance(body_dict, list):
                raw_list = body_dict
            else:
                raise ValueError("Expected JSON array or object with 'receipts' list")
            receipts = [Receipt(**item) if isinstance(item, dict) else item for item in raw_list]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON request body: {exc}") from exc

    if not receipts:
        raise HTTPException(status_code=422, detail="At least 1 receipt required.")

    logger.debug(
        "Entering create_receipts_batch: batch_count=%d, identity (user_id=%s)",
        len(receipts),
        identity.user_id,
    )

    # Pre-generate IDs for all receipts
    receipt_ids = [str(uuid.uuid4()) for _ in receipts]
    receipt_image_paths: list[str | None] = [None] * len(receipts)
    user_id = identity.user_id or identity.device_id

    if uploaded_files:
        for uploaded_file in uploaded_files:
            filename = getattr(uploaded_file, "filename", "") or ""
            matched_index: int | None = None

            # Look for index digit in filename
            digits = re.findall(r"\d+", filename)
            if digits:
                idx = int(digits[-1])
                if 0 <= idx < len(receipts):
                    matched_index = idx

            # Fallback to positional order
            if matched_index is None:
                pos = uploaded_files.index(uploaded_file)
                if pos < len(receipts):
                    matched_index = pos

            if matched_index is None:
                logger.warning("create_receipts_batch: could not map image '%s'; skipping", filename)
                continue

            raw_bytes: bytes = b""
            if hasattr(uploaded_file, "read"):
                raw_bytes = await uploaded_file.read()
            elif isinstance(uploaded_file, (bytes, bytearray)):
                raw_bytes = bytes(uploaded_file)

            if not raw_bytes:
                continue

            validate_image_size(raw_bytes, max_bytes=_settings.max_upload_size_bytes)

            path = await image_storage.upload_receipt_image(
                user_id=user_id,
                receipt_id=receipt_ids[matched_index],
                image_bytes=raw_bytes,
                target_max_bytes=_settings.max_compressed_image_bytes,
            )
            receipt_image_paths[matched_index] = path
            logger.info(
                "create_receipts_batch: image uploaded → %s for index=%d",
                path,
                matched_index,
            )

    records = await repo.create_batch(
        identity,
        receipts,
        receipt_image_paths=receipt_image_paths,
        receipt_ids=receipt_ids,
    )
    logger.info(
        "Batch receipts created successfully: created_count=%d, user_id=%s",
        len(records),
        identity.user_id,
    )
    return records


# ── UPDATE receipt ────────────────────────────────────────────────────────────
@router.patch(
    "/{receipt_id}",
    response_model=ReceiptRecord,
    summary="Update a receipt (supports JSON or multipart image upload)",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "receipt_image": {
                                "type": "string",
                                "format": "binary",
                                "description": "Optional new receipt image file (JPEG, PNG, WEBP, max 20MB raw). Replaces storage image, compressed to <= 5MB.",
                            },
                            "receipt_json": {
                                "type": "string",
                                "description": "Updated receipt JSON object string.",
                            },
                        },
                    }
                },
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/ReceiptUpdateRequest"
                    }
                },
            }
        }
    },
)
async def update_receipt(
    receipt_id: str,
    request: Request,
    identity: Identity = Depends(get_user_identity),
    repo: ReceiptRepository = Depends(get_repo),
    image_storage: ImageStorageService = Depends(get_image_storage),
):
    """Update a receipt owned by the caller's authenticated user identity.

    Accepts both:
    - `application/json` body (`ReceiptUpdateRequest`).
    - `multipart/form-data` with optional `receipt_image` file and `receipt_json`.

    Only non-None fields are updated; ownership metadata is immutable.
    When an image is provided, it is compressed to <= 5MB and replaces the existing image in storage.
    """
    content_type = request.headers.get("content-type", "").lower()
    receipt: Receipt | None = None
    receipt_image_path: str | None = None
    image_bytes: bytes | None = None

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()

        # Extract image bytes
        image_field = form.get("receipt_image") or form.get("image") or form.get("file")
        if image_field is not None:
            if hasattr(image_field, "read"):
                image_bytes = await image_field.read()
            elif isinstance(image_field, (bytes, bytearray)):
                image_bytes = bytes(image_field)
            elif isinstance(image_field, str) and image_field.strip():
                image_bytes = image_field.encode("utf-8")

        # Extract receipt JSON payload
        receipt_json_str = _clean_str(form.get("receipt_json") or form.get("receipt") or form.get("body"))
        if receipt_json_str:
            try:
                loaded = json.loads(receipt_json_str)
                if isinstance(loaded, dict) and "receipt" in loaded:
                    receipt = Receipt(**loaded["receipt"])
                elif isinstance(loaded, dict):
                    receipt = Receipt(**loaded)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"Invalid receipt_json in form: {exc}") from exc
    else:
        # JSON mode
        try:
            body_dict = await request.json()
            if isinstance(body_dict, dict):
                if "receipt" in body_dict and body_dict["receipt"] is not None:
                    receipt = Receipt(**body_dict["receipt"])
                elif "merchant_name" in body_dict:
                    receipt = Receipt(**body_dict)
                receipt_image_path = body_dict.get("receipt_image_path")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON request body: {exc}") from exc

    logger.debug(
        "Entering update_receipt: receipt_id=%s, merchant=%s, identity (user_id=%s)",
        receipt_id,
        receipt.merchant_name if receipt else "N/A",
        identity.user_id,
    )

    if image_bytes and len(image_bytes) > 0:
        validate_image_size(image_bytes, max_bytes=_settings.max_upload_size_bytes)

        user_id = identity.user_id or identity.device_id
        receipt_image_path = await image_storage.upload_receipt_image(
            user_id=user_id,
            receipt_id=receipt_id,
            image_bytes=image_bytes,
            target_max_bytes=_settings.max_compressed_image_bytes,
        )
        logger.info(
            "update_receipt: image uploaded → %s for receipt_id=%s",
            receipt_image_path,
            receipt_id,
        )

    if receipt is None and receipt_image_path is None:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one of: a receipt JSON body, receipt_json form field, or receipt_image.",
        )

    updated = await repo.update(
        receipt_id,
        identity,
        receipt=receipt,
        receipt_image_path=receipt_image_path,
    )
    if not updated:
        logger.warning(
            "Update receipt failed — not found or not owned: receipt_id=%s, user_id=%s",
            receipt_id,
            identity.user_id,
        )
        raise HTTPException(status_code=404, detail="Receipt not found or access denied")
    logger.info(
        "Receipt updated successfully: receipt_id=%s, user_id=%s",
        receipt_id,
        identity.user_id,
    )
    return updated


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
