from fastapi import APIRouter, HTTPException, Depends
from src.Models.schemas import ScanRequest, ScanResponse
from src.Services.extraction_service import ExtractionService

router = APIRouter(prefix="/scan", tags=["Scanning"])

def get_extraction_service() -> ExtractionService:
    return ExtractionService()

@router.post("/parse", response_model=ScanResponse)
async def parse_receipt(
    request: ScanRequest,
    service: ExtractionService = Depends(get_extraction_service)
):
    """Parse OCR text from a scanned receipt and return structured data."""
    try:
        extraction = await service.extract_from_ocr(request)
        return ScanResponse(success=True, data=extraction)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
