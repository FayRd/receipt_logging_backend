import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, AliasChoices
from datetime import datetime, timezone


class LineItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    description: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    total_price: float | None = None

    @field_validator("description", mode="before")
    @classmethod
    def coerce_description(cls, v):
        if v is None:
            return ""
        return str(v)


class Receipt(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    merchant_name: str = "N/A"
    line_items: list[LineItem] | None = None
    subtotal: float | None = None
    tax_amount: float | None = None
    total_amount: float = 0.0
    currency: str = "USD"
    category: str | None = None
    date: str | datetime = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw_text: str = ""
    confidence_score: float = 0.0
    notes: str | None = None

    @field_validator("merchant_name", mode="before")
    @classmethod
    def coerce_merchant_name(cls, v):
        if v is None or not str(v).strip():
            return "N/A"
        return str(v).strip()

    @field_validator("total_amount", mode="before")
    @classmethod
    def coerce_total_amount(cls, v):
        if v is None:
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    @field_validator("currency", mode="before")
    @classmethod
    def coerce_currency(cls, v):
        if v is None or not str(v).strip():
            return "USD"
        return str(v).strip()

    @field_validator("date", mode="before")
    @classmethod
    def coerce_date(cls, v):
        if v is None or not str(v).strip():
            return datetime.now(timezone.utc).isoformat()
        return v

    @field_validator("raw_text", mode="before")
    @classmethod
    def coerce_raw_text(cls, v):
        if v is None:
            return ""
        return str(v)

    @field_validator("confidence_score", mode="before")
    @classmethod
    def coerce_confidence_score(cls, v):
        if v is None:
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0


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
    receipt_image_path: str | None = None
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
    Only non-None fields are updated; ownership metadata is immutable.
    Accepts both JSON and multipart/form-data (receipt_image_path is set server-side
    after image upload).
    """
    receipt: Receipt | None = None
    receipt_image_path: str | None = None


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str = "1.0.0"


# ── USER SCHEMAS ──────────────────────────────────────────────────────────────

class CustomCategorySchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    color_value: int = Field(..., alias="colorValue")
    icon_code_point: int = Field(..., alias="iconCodePoint")

    model_config = ConfigDict(populate_by_name=True)


class UserCreateRequest(BaseModel):
    """Request body for new user registration."""
    username: str = Field(..., min_length=3, max_length=10, pattern=r"^[a-zA-Z0-9_]{3,10}$")
    email: str = Field(..., description="Unique email address for account identification and login.")
    password: str = Field(..., min_length=8)  # Pre-encrypted string from client
    country_code: str | None = Field(default=None, max_length=10, description="E.164 country dialling code, e.g. +60.")
    mobile_number: str | None = Field(default=None, max_length=20, description="Mobile number without country code.")
    avatar_image_path: str | None = None
    custom_categories: list[CustomCategorySchema] | None = Field(default=None, max_length=8, description="Custom user categories (max 8)")
    preferences: dict[str, Any] | None = Field(default=None, description="User UI and spending preferences dictionary.")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        s = v.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s):
            raise ValueError("Invalid email address format.")
        return s

    @field_validator("password")
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one digit.")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', v):
            raise ValueError("Password must contain at least one special character.")
        return v


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
    custom_categories: list[CustomCategorySchema] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    email_verified_at: datetime | None = None
    mobile_verified_at: datetime | None = None
    tier: str = "free"
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
    custom_categories: list[CustomCategorySchema] | None = Field(default=None, max_length=8)
    preferences: dict[str, Any] | None = Field(default=None, description="User UI and spending preferences dictionary.")


class UserLoginResponse(BaseModel):
    success: bool
    user: UserRecord
    message: str
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None


class TokenRefreshRequest(BaseModel):
    """Request body for rotating JWT session tokens."""
    refresh_token: str = Field(..., description="Valid JWT refresh token.")


class TokenRefreshResponse(BaseModel):
    """Response containing fresh JWT access and rotated refresh tokens."""
    success: bool
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserRecord


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
    new_password: str = Field(..., min_length=8, description="New password.")

    @field_validator("new_password")
    @classmethod
    def validate_new_password_complexity(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one digit.")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', v):
            raise ValueError("Password must contain at least one special character.")
        return v


class ChangePasswordRequest(BaseModel):
    """Request body for authenticated password change via POST /user/change-password."""
    old_password: str = Field(..., min_length=1, description="Current account password.")
    new_password: str = Field(..., min_length=8, description="New account password (min 8 characters).")

    @field_validator("new_password")
    @classmethod
    def validate_change_password_complexity(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one digit.")
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', v):
            raise ValueError("Password must contain at least one special character.")
        return v


# ── EMAIL / MOBILE VERIFICATION SCHEMAS ──────────────────────────────────────

class VerifyInitiateRequest(BaseModel):
    """Request body for initiating email verification via OTP."""
    type: Literal["email", "mobile"] = "email"
    identifier: str = Field(..., min_length=3, max_length=255, description="Email address to verify.")


class VerifyCompleteRequest(BaseModel):
    """Request body for completing email verification by submitting the OTP."""
    type: Literal["email", "mobile"] = "email"
    identifier: str = Field(..., min_length=3, max_length=255, description="Email address being verified.")
    otp: str = Field(..., min_length=6, max_length=6, description="6-digit numeric OTP code.")


# ── CHAT / CONVERSATION SCHEMAS ───────────────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    """Request body for creating a new conversation.

    Identity (user_id + device_id) is resolved server-side from session headers.
    """
    title: str | None = None


class ConversationUpdateRequest(BaseModel):
    """Request body for updating conversation title."""
    title: str = Field(..., min_length=1, max_length=255, description="New conversation title.")


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
      Client must supply `conversation_history` (prior message turns) and optionally `receipts`
      (local Isar DB receipts) for AI context.
    """
    conversation_id: str | None = Field(default=None, description="Supabase conversation UUID. Omit/null for local-only storage (Guest or User local mode).")
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_history: list[ChatMessageInput] = Field(
        default_factory=list,
        max_length=50,
        description="Client-managed local conversation history (max 50 turns). Used only in local/guest mode.",
    )
    receipts: list[Receipt] = Field(
        default_factory=list,
        max_length=100,
        validation_alias=AliasChoices('receipts', 'recent_receipts'),
        description="Client-supplied full receipts for AI spending analysis RAG context. Used only in local/guest mode.",
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
    custom_categories: list[CustomCategorySchema] = Field(default_factory=list, max_length=8)


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


# ── HELP & FEEDBACK SCHEMAS ───────────────────────────────────────────────────

class FeedbackSubmitRequest(BaseModel):
    """Request payload for submitting user/device feedback."""
    sender: str = Field(..., min_length=3, max_length=100, description="Username or device UUID identifier")
    description: str = Field(..., min_length=25, max_length=2000, description="Feedback message content (25 - 2000 characters)")
    app_version: str | None = Field(default="1.0.0", max_length=50, description="Client app version")
    device_id: str | None = Field(default=None, max_length=100, description="Client device identifier")
    platform: str | None = Field(default="mobile", max_length=50, description="Operating system / platform")


class FeedbackSubmitResponse(BaseModel):
    """Response returned upon feedback submission."""
    success: bool
    message: str


# ── TIER QUOTA SCHEMAS ───────────────────────────────────────────────────────

class QuotaMetric(BaseModel):
    used: int
    limit: int  # -1 represents unlimited
    remaining: int  # -1 represents unlimited
    is_exhausted: bool


class QuotaStatusResponse(BaseModel):
    success: bool = True
    tier: str
    scan: QuotaMetric
    chat: QuotaMetric
    reset_at: str
    seconds_to_reset: int
    reset_countdown: str


