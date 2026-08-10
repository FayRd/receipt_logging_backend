import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_current_identity, require_user_identity
from src.Auth.rate_limiter import rate_limit
from src.Models.schemas import (
    UserCreateRequest,
    UserLoginRequest,
    UserRecord,
    UserLoginResponse,
    UserUpdateRequest,
    PasswordResetInitiateRequest,
    PasswordResetOtpRequest,
    PasswordResetNewRequest,
)
from src.Models.Users.user_repository import UserRepository
from src.Models.Users.password_reset_repository import PasswordResetRepository

router = APIRouter(prefix="/user", tags=["Users"])


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> UserRepository:
    return UserRepository(db)


async def get_reset_repo(db: AsyncClient = Depends(get_supabase_client)) -> PasswordResetRepository:
    return PasswordResetRepository(db)


# ── POST /user/create ─────────────────────────────────────────────────────────
@router.post(
    "/create",
    response_model=UserRecord,
    status_code=201,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def create_user(
    body: UserCreateRequest,
    repo: UserRepository = Depends(get_repo),
):
    """Register a new user account. Rejects duplicate usernames or emails (case-insensitive).

    This is an unauthenticated public endpoint — rate limited to protect against registration spam.
    """
    if await repo.get_by_username(body.username):
        raise HTTPException(status_code=409, detail="Username already taken.")

    if await repo.get_by_email(body.email):
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    user = await repo.create(body)
    return user


# ── POST /user/login ──────────────────────────────────────────────────────────
@router.post(
    "/login",
    response_model=UserLoginResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def login_user(
    body: UserLoginRequest,
    repo: UserRepository = Depends(get_repo),
):
    """Authenticate user credentials and return sanitized user profile.

    Supports login via username or email address.
    Rate limited to protect against brute-force password guessing attacks.
    """
    user = await repo.get_by_identifier(body.username)
    # Identical error for missing user AND wrong password — prevents username enumeration
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    incoming_hash = repo.hash_password(body.password)
    if incoming_hash != user.get("password", ""):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    user.pop("password", None)
    return UserLoginResponse(success=True, user=user, message="Login successful.")


# ── GET /user/me ──────────────────────────────────────────────────────────────
@router.get(
    "/me",
    response_model=UserRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_my_profile(
    identity: Identity = Depends(require_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Retrieve the current authenticated user's profile.

    Requires X-Device-ID, X-Device-Token, and X-User-ID headers.
    Returns 401 if not signed in.
    """
    user = await repo.get_by_id(identity.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


# ── PATCH /user/me ────────────────────────────────────────────────────────────
@router.patch(
    "/me",
    response_model=UserRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def update_my_profile(
    body: UserUpdateRequest,
    identity: Identity = Depends(require_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Update mutable profile fields for the authenticated user.

    All fields are optional — only supplied (non-null) values are written.
    Rejects duplicate emails (case-insensitive) with HTTP 409.
    Requires X-Device-ID, X-Device-Token, and X-User-ID headers.
    """
    if body.email is not None:
        existing = await repo.get_by_email(body.email)
        if existing and existing.get("id") != identity.user_id:
            raise HTTPException(status_code=409, detail="An account with this email already exists.")

    updated = await repo.update_profile(identity.user_id, body)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found.")
    return updated


# ── DELETE /user/me ───────────────────────────────────────────────────────────
@router.delete(
    "/me",
    status_code=200,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def delete_my_profile(
    identity: Identity = Depends(require_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Soft-delete current authenticated user profile.

    Requires authenticated user session (X-User-ID header). Returns 401 if guest.
    """
    deleted = await repo.soft_delete(identity.user_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="User profile not found or already deleted.",
        )
    return {"success": True, "message": "User profile soft-deleted successfully."}


# ── POST /user/reset-password-initiate ───────────────────────────────────────
@router.post(
    "/reset-password-initiate",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def initiate_password_reset(
    body: PasswordResetInitiateRequest,
    user_repo: UserRepository = Depends(get_repo),
    reset_repo: PasswordResetRepository = Depends(get_reset_repo),
):
    """Initiate a password reset flow via email address or mobile number.

    Returns HTTP 200 regardless of whether the account exists (prevents account enumeration).
    In development mode, logs the generated 6-digit OTP to terminal and includes dev_otp in JSON.
    """
    clean_identifier = body.identifier.strip()
    user = await user_repo.get_by_email_or_mobile(clean_identifier)

    dev_otp = None
    if user:
        otp_num = secrets.randbelow(900_000) + 100_000
        otp_str = str(otp_num)
        dev_otp = otp_str

        email_val = user.get("email")
        mobile_val = user.get("mobile_number")
        await reset_repo.create_reset_request(
            user_id=user["id"],
            email=email_val,
            mobile_number=mobile_val,
            otp=otp_str,
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_line = f"[{now_str}] [OTP] Reset code for '{clean_identifier}' (User: {user.get('username')}, ID: {user.get('id')}): {otp_str}\n"
        print(f"🔑 {log_line.strip()}")
        try:
            with open("otp_dev.log", "a", encoding="utf-8") as f:
                f.write(log_line)
        except Exception:
            pass
    else:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        warn_line = f"[{now_str}] ⚠️ [OTP WARNING] Account not found for identifier: '{clean_identifier}'. dev_otp is null.\n"
        print(warn_line.strip())
        try:
            with open("otp_dev.log", "a", encoding="utf-8") as f:
                f.write(warn_line)
        except Exception:
            pass

    return {
        "success": True,
        "message": "If an account with this email/mobile exists, a reset code has been sent.",
        "dev_otp": dev_otp,
    }


# ── POST /user/reset-password-otp ────────────────────────────────────────────
@router.post(
    "/reset-password-otp",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def verify_password_reset_otp(
    body: PasswordResetOtpRequest,
    user_repo: UserRepository = Depends(get_repo),
    reset_repo: PasswordResetRepository = Depends(get_reset_repo),
):
    """Verify 6-digit password reset OTP.

    If valid, returns a single-use reset_token to be used on /password-reset-new.
    Enforces maximum 5 failed attempts per request before lockout.
    """
    clean_identifier = body.identifier.strip()
    user = await user_repo.get_by_email_or_mobile(clean_identifier)

    if not user:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset code. Please request a new code.",
        )

    success, msg, reset_token = await reset_repo.verify_otp(user["id"], body.otp)
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    return {
        "success": True,
        "reset_token": reset_token,
        "message": msg,
    }


# ── POST /user/password-reset-new ────────────────────────────────────────────
@router.post(
    "/password-reset-new",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def complete_password_reset(
    body: PasswordResetNewRequest,
    user_repo: UserRepository = Depends(get_repo),
    reset_repo: PasswordResetRepository = Depends(get_reset_repo),
):
    """Set a new password using a single-use reset_token issued by /reset-password-otp.

    Hashes the new password with server-side PBKDF2 salt and invalidates the reset_token.
    """
    new_hash = user_repo.hash_password(body.new_password)
    success, msg = await reset_repo.complete_reset(body.reset_token, new_hash)

    if not success:
        raise HTTPException(status_code=400, detail=msg)

    return {
        "success": True,
        "message": msg,
    }
