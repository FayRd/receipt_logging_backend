import json
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    date: str | datetime
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


class BulkJobSummary(BaseModel):
    job_id: str
    filename: str | None = None


class BulkJobCreateResponse(BaseModel):
    batch_id: str
    total_jobs: int
    jobs: list[BulkJobSummary]


class BulkJobStatus(BaseModel):
    job_id: str
    batch_id: str
    filename: str | None = None
    status: str
    data: Receipt | None = None
    error: str | None = None


class BulkBatchStatusResponse(BaseModel):
    batch_id: str
    total_jobs: int
    completed_jobs: int
    jobs: list[BulkJobStatus]


class ReceiptRecord(BaseModel):
    id: str
    user_id: str | None = None
    device_id: str
    receipt: Receipt
    created_at: datetime
    updated_at: datetime | None = None
    deleted_at: datetime | None = None


class ReceiptCreateRequest(BaseModel):
    """Request body for creating a single receipt record.

    Identity (user_id + device_id) is resolved server-side from session headers.
    """
    receipt: Receipt


class ReceiptBatchCreateRequest(BaseModel):
    """Request body for batch-creating up to 100 receipt records in one DB call.

    Identity (user_id + device_id) is resolved server-side from session headers.
    """
    receipts: list[Receipt] = Field(..., min_length=1, max_length=100)


class ReceiptUpdateRequest(BaseModel):
    """Request body for updating an existing receipt record via PATCH.

    Identity (user_id) is resolved server-side from session headers.
    Only the receipt payload is updated; ownership metadata is immutable.
    """
    receipt: Receipt


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str = "1.0.0"


# ── USER SCHEMAS ──────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    """Request body for new user registration."""
    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., description="Unique email address for account identification and login.")
    password: str = Field(..., min_length=6)  # Pre-encrypted string from client
    country_code: str | None = Field(default=None, max_length=10, description="E.164 country dialling code, e.g. +60.")
    mobile_number: str | None = Field(default=None, max_length=20, description="Mobile number without country code.")
    avatar_image_path: str | None = None


class UserLoginRequest(BaseModel):
    """Request body for user login. `username` may be an email address or username."""
    username: str = Field(..., description="Username or email address.")
    password: str  # Pre-encrypted string from client


class UserRecord(BaseModel):
    """Sanitized user model — password is never included."""
    id: str
    username: str
    email: str
    country_code: str | None = None
    mobile_number: str | None = None
    avatar_image_path: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    deleted_at: datetime | None = None


class UserUpdateRequest(BaseModel):
    """Request body for updating user profile via PATCH /user/me.

    All fields are optional — only non-None values will be persisted.
    """
    email: str | None = None
    country_code: str | None = Field(default=None, max_length=10)
    mobile_number: str | None = Field(default=None, max_length=20)
    avatar_image_path: str | None = None


class UserLoginResponse(BaseModel):
    success: bool
    user: UserRecord
    message: str


# ── PASSWORD RESET SCHEMAS ───────────────────────────────────────────────────

class PasswordResetInitiateRequest(BaseModel):
    """Request body for initiating a password reset via email or mobile number."""
    identifier: str = Field(..., min_length=3, description="Email address or mobile number.")


class PasswordResetOtpRequest(BaseModel):
    """Request body for verifying a 6-digit password reset OTP."""
    identifier: str = Field(..., min_length=3, description="Email address or mobile number.")
    otp: str = Field(..., min_length=6, max_length=6, description="6-digit numeric OTP code.")


class PasswordResetNewRequest(BaseModel):
    """Request body for setting a new password using a single-use reset_token."""
    reset_token: str = Field(..., min_length=10, description="Single-use reset token issued after OTP verification.")
    new_password: str = Field(..., min_length=6, description="New password.")


# ── CHAT / CONVERSATION SCHEMAS ───────────────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    """Request body for creating a new conversation.

    Identity (user_id + device_id) is resolved server-side from session headers.
    """
    title: str | None = None


class ConversationRecord(BaseModel):
    id: str
    user_id: str | None = None
    device_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ChatMessageInput(BaseModel):
    """A single client-supplied conversation history turn for local/guest AI chat."""
    role: str = Field(..., description="Role of the message sender: 'user' or 'assistant'.")
    content: str = Field(..., min_length=1, max_length=4000, description="Text content of the message.")


class ReceiptContextItem(BaseModel):
    """A lightweight receipt summary for client-supplied AI RAG context in local/guest chat."""
    merchant_name: str
    total_amount: float
    category: str | None = None
    date: str | None = None


class ChatMessageRecord(BaseModel):
    id: str
    conversation_id: str | None  # None for local/guest mode messages not persisted to Supabase
    sender: str  # "user" | "assistant"
    content: str
    created_at: datetime


class ChatQueryRequest(BaseModel):
    """Request body for sending a message to the AI.

    Identity (user_id + device_id) is resolved server-side from session headers.

    Storage mode is controlled by conversation_id:
    - Cloud Store (User): Supply a valid `conversation_id` UUID — messages are persisted to Supabase DB.
    - Local Store (User or Guest): Omit or pass null for `conversation_id` — no Supabase DB writes.
      Client must supply `conversation_history` (prior message turns) and optionally `recent_receipts`
      (local Isar DB receipts) for AI context.
    """
    conversation_id: str | None = Field(default=None, description="Supabase conversation UUID. Omit/null for local-only storage (Guest or User local mode).")
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_history: list[ChatMessageInput] = Field(
        default_factory=list,
        max_length=20,
        description="Client-managed local conversation history (max 20 turns). Used only in local/guest mode.",
    )
    recent_receipts: list[ReceiptContextItem] = Field(
        default_factory=list,
        max_length=50,
        description="Client-supplied local receipts for AI spending analysis RAG context. Used only in local/guest mode.",
    )


class ChatQueryResponse(BaseModel):
    conversation_id: str | None  # None for local/guest mode
    user_message: ChatMessageRecord
    assistant_message: ChatMessageRecord


class ChatHistoryResponse(BaseModel):
    conversation_id: str
    messages: list[ChatMessageRecord]
    total_count: int
    has_more: bool


# ── DEVICE SCHEMAS ────────────────────────────────────────────────────────────

class DeviceRegisterRequest(BaseModel):
    """Request body for registering or updating a device.

    device_name is the hardware or session variant identifier string, e.g. MS701-A1B1.
    device_token is the plaintext secret device token (hashed server-side).
    username is optional user account to associate on registration.
    """
    device_name: str = Field(..., min_length=3, max_length=100)
    device_token: str = Field(..., min_length=8, max_length=256)
    username: str | None = None


class GuestDataMigrationPayload(BaseModel):
    """Payload containing guest records exported from local Isar DB for migration on signup."""
    receipts: list[dict] = Field(default_factory=list, max_length=200)
    conversations: list[dict] = Field(default_factory=list, max_length=200)
    chat_messages: list[dict] = Field(default_factory=list, max_length=500)


class DeviceLinkRequest(BaseModel):
    """Request body for linking/unlinking a device to a user account.

    device_name is the hardware or session variant name string (e.g. MS701-A1B1).
    username specifies target user account to link, or null to unlink (guest mode).
    Tokens are supplied exclusively via X-Device-Token and X-User-Token HTTP headers.
    migrate_data is an optional guest data payload exported from local Isar DB (object or stringified JSON).
    """
    device_name: str = Field(..., min_length=3, max_length=100)
    username: str | None = None  # None unlinks the user (guest mode)
    migrate_data: GuestDataMigrationPayload | dict | str | None = Field(
        default=None,
        description="Optional guest data payload: {receipts: [...], conversations: [...], chat_messages: [...]} or JSON string.",
    )

    @field_validator("migrate_data", mode="before")
    @classmethod
    def parse_migrate_data(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                raise ValueError("Invalid JSON string for migrate_data")
        return v



class DeviceRecord(BaseModel):
    id: str
    name: str
    username: str | None = None
    user_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    deleted_at: datetime | None = None


class DeviceTokenRotateRequest(BaseModel):
    """Request body for rotating an existing device's secret authentication token."""
    new_device_token: str = Field(..., min_length=8, max_length=256, description="Fresh secret device authentication token.")
