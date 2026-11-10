"""Web Account Deletion Router.

Provides public web endpoints for Google Play Data Safety policy compliance:
- GET /delete-account: Public web landing page with responsive dark neumorphic UI.
- POST /api/v1/web/request-deletion: Initiates account deletion by dispatching email OTP.
- POST /api/v1/web/confirm-deletion: Validates OTP and triggers full cryptographic shredding cascade.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from supabase._async.client import AsyncClient

from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Infrastructure.redis_service import (
    check_resend_cooldown,
    generate_otp,
    set_resend_cooldown,
    store_otp,
    verify_otp,
)
from src.Infrastructure.database import get_supabase_client
from src.Services.deletion_service import execute_account_deletion
from src.Services.email_service import send_2fa_action_email

logger = get_logger("API.web.deletion")

router = APIRouter(tags=["Web Account Deletion"])

_GENERIC_OTP_MSG = (
    "If an account associated with this email exists, a 6-digit verification code has been sent."
)

# ─────────────────────────────────────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────────────────────────────────────

class WebDeletionRequestBody(BaseModel):
    email: EmailStr
    website: str = ""  # Honeypot field — bots fill this, legitimate users leave empty


class WebDeletionConfirmBody(BaseModel):
    email: EmailStr
    otp: str
    website: str = ""  # Honeypot field


# ─────────────────────────────────────────────────────────────────────────────
# HTML Template
# ─────────────────────────────────────────────────────────────────────────────

_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Delete Account & Data | SancFund</title>
  <style>
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    body {
      background-color: #0b1120;
      color: #e2e8f0;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      padding: 20px;
    }
    .card {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 16px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4), 0 8px 10px -6px rgba(0, 0, 0, 0.4);
      width: 100%;
      max-width: 520px;
      padding: 32px;
    }
    .header {
      margin-bottom: 24px;
      text-align: center;
    }
    .logo-badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 54px;
      height: 54px;
      border-radius: 12px;
      background: rgba(239, 68, 68, 0.15);
      color: #ef4444;
      font-size: 28px;
      margin-bottom: 12px;
    }
    h1 {
      font-size: 24px;
      font-weight: 700;
      color: #f8fafc;
      margin-bottom: 8px;
    }
    .subtitle {
      font-size: 14px;
      color: #94a3b8;
      line-height: 1.5;
    }
    .disclosure-box {
      background: #0f172a;
      border-left: 4px solid #ef4444;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 24px;
      font-size: 13px;
      color: #cbd5e1;
      line-height: 1.6;
    }
    .disclosure-box strong {
      color: #f8fafc;
      display: block;
      margin-bottom: 6px;
    }
    .disclosure-box ul {
      margin-left: 20px;
      margin-top: 6px;
    }
    .form-group {
      margin-bottom: 20px;
    }
    label {
      display: block;
      font-size: 13px;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 8px;
    }
    input[type="email"],
    input[type="text"] {
      width: 100%;
      padding: 12px 16px;
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      color: #f8fafc;
      font-size: 15px;
      outline: none;
      transition: border-color 0.2s;
    }
    input[type="email"]:focus,
    input[type="text"]:focus {
      border-color: #3b82f6;
    }
    .otp-input {
      letter-spacing: 6px;
      font-size: 20px !important;
      text-align: center;
      font-weight: 700;
    }
    .btn {
      width: 100%;
      padding: 14px;
      border: none;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.2s, opacity 0.2s;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    .btn-primary {
      background: #3b82f6;
      color: #ffffff;
    }
    .btn-primary:hover {
      background: #2563eb;
    }
    .btn-danger {
      background: #ef4444;
      color: #ffffff;
    }
    .btn-danger:hover {
      background: #dc2626;
    }
    .btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .banner {
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 13px;
      line-height: 1.5;
      margin-bottom: 20px;
      display: none;
    }
    .banner-success {
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid #10b981;
      color: #6ee7b7;
    }
    .banner-error {
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid #ef4444;
      color: #fca5a5;
    }
    .banner-info {
      background: rgba(59, 130, 246, 0.15);
      border: 1px solid #3b82f6;
      color: #93c5fd;
    }
    .hidden {
      display: none;
    }
    .legal-footer {
      text-align: center;
      font-size: 12px;
      color: #64748b;
      margin-top: 24px;
      line-height: 1.5;
    }
    /* Honeypot field: invisible to real users */
    .hp-field {
      display: none !important;
      visibility: hidden !important;
      position: absolute !important;
      left: -9999px !important;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="logo-badge">&#9888;</div>
      <h1>Delete Account &amp; Data</h1>
      <p class="subtitle">Google Play Data Safety &amp; Privacy Request Portal</p>
    </div>

    <div class="disclosure-box">
      <strong>Data Deletion Scope Notice:</strong>
      Initiating account deletion is immediate and permanent. The following items will be permanently erased:
      <ul>
        <li>Cryptographic data encryption keys (instant crypto-shredding)</li>
        <li>All receipt images, line-item details, and scanned metrics</li>
        <li>Account credentials, contact information, and avatar</li>
        <li>Device identifiers and push notification tokens</li>
      </ul>
      This process cannot be undone.
    </div>

    <div id="banner" class="banner"></div>

    <!-- Honeypot anti-bot hidden input -->
    <input type="text" id="website" name="website" class="hp-field" tabindex="-1" autocomplete="off">

    <!-- Step 1: Email Form -->
    <div id="step-request">
      <div class="form-group">
        <label for="email">Registered Account Email</label>
        <input type="email" id="email" placeholder="you@example.com" required autocomplete="email">
      </div>
      <button type="button" id="btn-request" class="btn btn-primary" onclick="onRequestOtp()">
        Send Verification Code
      </button>
    </div>

    <!-- Step 2: OTP Confirmation Form -->
    <div id="step-confirm" class="hidden">
      <div class="form-group">
        <label for="otp">Enter 6-Digit Verification Code</label>
        <input type="text" id="otp" class="otp-input" maxlength="6" placeholder="000000" inputmode="numeric" pattern="[0-9]*">
      </div>
      <button type="button" id="btn-confirm" class="btn btn-danger" onclick="onConfirmDeletion()">
        Permanently Delete My Account
      </button>
      <div style="text-align: center; margin-top: 14px;">
        <a href="javascript:void(0)" onclick="resetForm()" style="color: #94a3b8; font-size: 13px; text-decoration: none;">&larr; Back to Email Entry</a>
      </div>
    </div>

    <!-- Step 3: Success Screen -->
    <div id="step-success" class="hidden" style="text-align: center; padding: 20px 0;">
      <div style="font-size: 48px; color: #10b981; margin-bottom: 16px;">&#10003;</div>
      <h2 style="font-size: 20px; color: #f8fafc; margin-bottom: 8px;">Account Successfully Deleted</h2>
      <p style="font-size: 14px; color: #94a3b8; line-height: 1.6;">
        All personal records, encryption keys, and receipt archives have been cryptographically erased.
        You may safely close this browser window.
      </p>
    </div>

    <div class="legal-footer">
      SancFund &bull; Conforms to Google Play Data Safety Policy &amp; Singapore PDPA
    </div>
  </div>

  <script>
    function showBanner(message, type) {
      const banner = document.getElementById("banner");
      banner.className = "banner banner-" + type;
      banner.textContent = message;
      banner.style.display = "block";
    }

    function hideBanner() {
      const banner = document.getElementById("banner");
      banner.style.display = "none";
    }

    function resetForm() {
      hideBanner();
      document.getElementById("step-request").classList.remove("hidden");
      document.getElementById("step-confirm").classList.add("hidden");
      document.getElementById("step-success").classList.add("hidden");
      document.getElementById("otp").value = "";
    }

    async function onRequestOtp() {
      const emailInput = document.getElementById("email");
      const email = emailInput.value.trim();
      const website = document.getElementById("website").value;
      const btn = document.getElementById("btn-request");

      if (!email || !email.includes("@")) {
        showBanner("Please enter a valid email address.", "error");
        return;
      }

      btn.disabled = true;
      btn.textContent = "Sending Verification Code...";
      hideBanner();

      try {
        const res = await fetch("/api/v1/web/request-deletion", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email, website: website })
        });
        const data = await res.json();

        if (res.ok) {
          showBanner(data.message || "A verification code has been dispatched. Please check your inbox.", "info");
          document.getElementById("step-request").classList.add("hidden");
          document.getElementById("step-confirm").classList.remove("hidden");
        } else {
          showBanner(data.detail || "Unable to process request. Please try again later.", "error");
        }
      } catch (err) {
        showBanner("A network error occurred. Please check your connection.", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "Send Verification Code";
      }
    }

    async function onConfirmDeletion() {
      const email = document.getElementById("email").value.trim();
      const otp = document.getElementById("otp").value.trim();
      const website = document.getElementById("website").value;
      const btn = document.getElementById("btn-confirm");

      if (!otp || otp.length < 6) {
        showBanner("Please enter the complete 6-digit verification code.", "error");
        return;
      }

      const confirmed = confirm("Are you completely sure you want to permanently delete your account? This action cannot be reversed.");
      if (!confirmed) return;

      btn.disabled = true;
      btn.textContent = "Permanently Deleting Data...";
      hideBanner();

      try {
        const res = await fetch("/api/v1/web/confirm-deletion", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email, otp: otp, website: website })
        });
        const data = await res.json();

        if (res.ok) {
          document.getElementById("step-confirm").classList.add("hidden");
          document.getElementById("step-success").classList.remove("hidden");
        } else {
          showBanner(data.detail || "Verification failed. The code may be incorrect or expired.", "error");
        }
      } catch (err) {
        showBanner("A network error occurred. Please try again.", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "Permanently Delete My Account";
      }
    }
  </script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/delete-account", response_class=HTMLResponse)
async def get_delete_account_page(request: Request):
    """Serve the public web page for account and data deletion requests."""
    return HTMLResponse(content=_PAGE_HTML, status_code=200)


@router.post(
    "/api/v1/web/request-deletion",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limit(3, window_seconds=3600))],
)
async def request_web_deletion(
    body: WebDeletionRequestBody,
    db: AsyncClient = Depends(get_supabase_client),
):
    """Initiate account deletion via email OTP.

    - Protects against bot automation via honeypot field.
    - Prevents user enumeration by always returning HTTP 200 with generic copy.
    - Enforces a 60-second cooldown between resend requests.
    - Strict sliding window rate limiting (3 requests per hour per IP).
    """
    # 1. Honeypot check: If the hidden honeypot field was filled, silently drop the request
    if body.website.strip():
        logger.warning("Bot detected on web deletion request (honeypot triggered)")
        return {"success": True, "message": _GENERIC_OTP_MSG}

    email_clean = str(body.email).strip().lower()

    # 2. Look up user by email (case-insensitive)
    try:
        user_res = (
            await db.table("users")
            .select("id, username, email, deleted_at")
            .ilike("email", email_clean)
            .is_("deleted_at", "null")
            .execute()
        )
        user_rows = user_res.data if user_res and user_res.data else []
    except Exception as db_err:
        logger.error("Database error looking up user for web deletion: %s", db_err)
        return {"success": True, "message": _GENERIC_OTP_MSG}

    # 3. Email enumeration defense: Return 200 even if user doesn't exist
    if not user_rows:
        logger.info("Web deletion requested for non-existent or deleted email: %s", email_clean)
        return {"success": True, "message": _GENERIC_OTP_MSG}

    user = user_rows[0]
    user_id = str(user["id"])
    username = user.get("username") or "User"

    # 4. Check 60-second resend cooldown
    in_cooldown, _ = check_resend_cooldown(user_id, "web_deletion")
    if in_cooldown:
        logger.info("Web deletion OTP request suppressed by resend cooldown for user %s", user_id)
        return {"success": True, "message": _GENERIC_OTP_MSG}

    # 5. Generate and securely store salted OTP
    otp = generate_otp()
    store_otp(user_id=user_id, target_type="web_deletion", identifier=email_clean, otp=otp)
    set_resend_cooldown(user_id, "web_deletion")

    # 6. Dispatch email notice with OTP code
    try:
        sent = await send_2fa_action_email(
            to_email=email_clean,
            otp=otp,
            action_name="permanent account deletion",
            username=username,
        )
        if not sent:
            logger.warning("SMTP dispatch returned False for web deletion OTP to %s", email_clean)
    except Exception as email_err:
        logger.error("Failed to dispatch web deletion OTP email to %s: %s", email_clean, email_err)

    return {"success": True, "message": _GENERIC_OTP_MSG}


@router.post(
    "/api/v1/web/confirm-deletion",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limit(5, window_seconds=3600))],
)
async def confirm_web_deletion(
    body: WebDeletionConfirmBody,
    background_tasks: BackgroundTasks,
    db: AsyncClient = Depends(get_supabase_client),
):
    """Validate OTP and execute the full account deletion cascade."""
    # 1. Honeypot check
    if body.website.strip():
        logger.warning("Bot detected on web deletion confirm (honeypot triggered)")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request submission.",
        )

    email_clean = str(body.email).strip().lower()

    # 2. Look up user by email
    try:
        user_res = (
            await db.table("users")
            .select("*")
            .ilike("email", email_clean)
            .is_("deleted_at", "null")
            .execute()
        )
        user_rows = user_res.data if user_res and user_res.data else []
    except Exception as db_err:
        logger.error("Database error looking up user for deletion confirm: %s", db_err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while accessing user records.",
        )

    if not user_rows:
        logger.warning("Web deletion confirm failed: User not found for email %s", email_clean)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code.",
        )

    user = user_rows[0]
    user_id = str(user["id"])

    # 3. Verify OTP code against Redis/in-memory store
    is_valid, err_msg = verify_otp(
        user_id=user_id,
        target_type="web_deletion",
        identifier=email_clean,
        input_otp=body.otp.strip(),
    )

    if not is_valid:
        logger.warning("Invalid OTP attempt for user %s during web deletion: %s", user_id, err_msg)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=err_msg or "Invalid or expired verification code.",
        )

    # 4. Execute the full cryptographic shredding & cleanup cascade
    await execute_account_deletion(
        user_id=user_id,
        user_row=user,
        db=db,
        background_tasks=background_tasks,
    )

    logger.info("Successfully completed web account deletion for user_id=%s", user_id)
    return {
        "success": True,
        "message": "Your account and all associated data have been permanently deleted.",
    }
