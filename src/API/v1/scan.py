import logging
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from src.Auth.identity import Identity, get_current_identity
from src.Models.schemas import ScanContext, ScanResponse
from src.Services.extraction_service import ExtractionService
from src.config import get_settings

router = APIRouter(prefix="/scan", tags=["Scanning"])
logger = logging.getLogger("receipt_scanner")


async def get_extraction_service() -> ExtractionService:
    return ExtractionService()


@router.post("/parse", response_model=ScanResponse)
async def parse_receipt(
    image: UploadFile = File(..., description="Receipt or financial statement image file (JPEG, PNG, WEBP, etc.)"),
    identity: Identity = Depends(get_current_identity),
    service: ExtractionService = Depends(get_extraction_service),
) -> ScanResponse:
    """Accept a multipart receipt/financial statement image upload and return AI-extracted structured data.

    Requires device authentication (X-Device-ID and X-Device-Token).
    Enforces dynamic upload size ceiling and document validation confidence threshold from settings.
    """
    settings = get_settings()

    try:
        image_bytes = await image.read()

        # Enforce maximum upload ceiling to prevent DoS & memory exhaustion
        if len(image_bytes) > settings.max_image_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Image file size exceeds maximum limit of {settings.max_image_size_bytes // (1024 * 1024)}MB.",
            )

        content_type = image.content_type or "image/jpeg"

        context = ScanContext(
            image_bytes=image_bytes,
            content_type=content_type,
            user_id=identity.user_id,
            device_id=identity.device_id,
        )

        receipt = await service.extract_from_image(context)

        # Enforce document validation threshold (must be >= confidence_threshold for valid receipts / financial statements)
        if receipt.confidence_score < settings.confidence_threshold:
            return ScanResponse(
                success=False,
                data=None,
                error=(
                    f"Invalid document type. The uploaded image does not appear to be a valid receipt or "
                    f"financial statement (confidence score {receipt.confidence_score:.2f} is below the {settings.confidence_threshold} threshold)."
                ),
            )

        return ScanResponse(success=True, data=receipt, error=None)

    except HTTPException as he:
        raise he
    except Exception as e:
        # Log raw exception internally for server diagnostics without exposing tracebacks to client
        logger.error(f"Receipt extraction error: {e}", exc_info=True)
        return ScanResponse(
            success=False,
            data=None,
            error="Receipt parsing failed. Please ensure the image is clear and try again.",
        )
