from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


class LineItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    description: str
    quantity: float | None = None
    unit_price: float | None = None
    total_price: float | None = None


class Receipt(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    merchant_name: str
    line_items: list[LineItem] | None = None
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float
    currency: str = "USD"
    category: str | None = None
    date: datetime
    raw_text: str
    confidence_score: float = 0.0
    notes: str | None = None


@dataclass
class ScanContext:
    """Internal dataclass carrying the parsed multipart upload fields."""
    image_bytes: bytes
    content_type: str
    user_id: str | None
    device_id: str


class ScanResponse(BaseModel):
    success: bool
    data: Receipt | None
    error: str | None


class ReceiptRecord(BaseModel):
    id: str
    user_id: str | None = None
    device_id: str
    receipt: Receipt
    created_at: datetime
    deleted_at: datetime | None = None


class ReceiptCreateRequest(BaseModel):
    """Request body for creating a single receipt record."""
    user_id: str
    device_id: str
    receipt: Receipt


class ReceiptBatchCreateRequest(BaseModel):
    """Request body for batch-creating up to 100 receipt records in one DB call."""
    user_id: str
    device_id: str
    receipts: list[Receipt] = Field(..., min_length=1, max_length=100)


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str = "1.0.0"


# ── USER SCHEMAS ──────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    """Request body for new user registration."""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)  # Pre-encrypted string from client
    avatar_image_path: str | None = None


class UserLoginRequest(BaseModel):
    """Request body for user login."""
    username: str
    password: str  # Pre-encrypted string from client


class UserRecord(BaseModel):
    """Sanitized user model — password is never included."""
    id: str
    username: str
    avatar_image_path: str | None = None
    created_at: datetime
    deleted_at: datetime | None = None


class UserLoginResponse(BaseModel):
    success: bool
    user: UserRecord
    message: str


# ── CHAT / CONVERSATION SCHEMAS ───────────────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    """Request body for creating a new conversation."""
    user_id: str | None = None
    device_id: str
    title: str | None = None


class ConversationRecord(BaseModel):
    id: str
    user_id: str | None = None
    device_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ChatMessageRecord(BaseModel):
    id: str
    conversation_id: str
    sender: str  # "user" | "assistant"
    content: str
    created_at: datetime


class ChatQueryRequest(BaseModel):
    """Request body for sending a message to the AI."""
    conversation_id: str
    user_id: str | None = None
    device_id: str
    message: str = Field(..., min_length=1, max_length=2000)


class ChatQueryResponse(BaseModel):
    conversation_id: str
    user_message: ChatMessageRecord
    assistant_message: ChatMessageRecord


class ChatHistoryResponse(BaseModel):
    conversation_id: str
    messages: list[ChatMessageRecord]
    total_count: int
    has_more: bool
