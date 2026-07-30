from fastapi import APIRouter, Depends, File, Form, UploadFile
from src.Models.schemas import ScanContext, ScanResponse
from src.Services.extraction_service import ExtractionService

router = APIRouter(prefix="/scan", tags=["Scanning"])


def get_extraction_service() -> ExtractionService:
    return ExtractionService()


@router.post("/parse", response_model=ScanResponse)
async def parse_receipt(
    image: UploadFile = File(..., description="Receipt image file (JPEG, PNG, WEBP, etc.)"),
    device_id: str = Form(..., description="Unique device identifier from the mobile client"),
    user_id: str | None = Form(None, description="Optional authenticated user UUID"),
    service: ExtractionService = Depends(get_extraction_service),
) -> ScanResponse:
    """Accept a multipart receipt image upload and return AI-extracted structured data.

    Sends the image directly to Gemini 1.5 Flash Vision API with a strict JSON output
    schema. On success, returns the parsed Receipt. On any failure, returns success=False
    with a descriptive error message (HTTP 200 in both cases).
    """
    try:
        image_bytes = await image.read()
        content_type = image.content_type or "image/jpeg"

        context = ScanContext(
            image_bytes=image_bytes,
            content_type=content_type,
            user_id=user_id,
            device_id=device_id,
        )

        receipt = await service.extract_from_image(context)
        return ScanResponse(success=True, data=receipt, error=None)

    except Exception as e:
        return ScanResponse(success=False, data=None, error=str(e))
