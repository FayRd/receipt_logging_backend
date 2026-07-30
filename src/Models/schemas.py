from pydantic import BaseModel
from datetime import datetime

class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = None
    total_price: float | None = None

class Receipt(BaseModel):
    merchant_name: str
    line_items: list[LineItem]
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float
    currency: str = "USD"
    category: str | None = None
    date: datetime
    raw_text: str
    confidence_score: float = 0.0
    notes: str | None = None

class ScanRequest(BaseModel):
    user_id: str | None = None
    device_id: str
    image_url: str

class ScanResponse(BaseModel):
    success: bool
    data: Receipt | None
    error: str | None

class ReceiptRecord(BaseModel):
    id: str
    user_id : str | None = None
    device_id: str
    receipt: Receipt
    created_at: datetime
    deleted_at: datetime | None = None
    
class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str = "1.0.0"
