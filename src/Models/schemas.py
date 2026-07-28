from pydantic import BaseModel

class LineItem(BaseModel):
    description: str
    quantity: float | None
    unit_price: float | None
    total_price: float | None

class ReceiptExtraction(BaseModel):
    merchant_name: str | None
    total_amount: float | None
    subtotal: float | None
    tax_amount: float | None
    currency: str = "USD"
    date: str | None
    category: str | None
    line_items: list[LineItem] = []
    raw_ocr_text: str
    confidence_score: float = 0.0
    notes: str | None

class ScanRequest(BaseModel):
    ocr_text: str
    image_url: str | None = None
    device_id: str | None = None

class ScanResponse(BaseModel):
    success: bool
    data: ReceiptExtraction | None
    error: str | None

class ReceiptRecord(BaseModel):
    id: str | None
    device_id: str | None
    merchant_name: str | None
    total_amount: float | None
    currency: str
    date: str | None
    category: str | None
    created_at: str | None

class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str = "1.0.0"
