import json
import math
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_user_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import (
    UserCreateRequest,
    UserLoginRequest,
    UserRecord,
    UserLoginResponse,
    UserUpdateRequest,
    CustomCategorySchema,
    PasswordResetInitiateRequest,
    PasswordResetOtpRequest,
    PasswordResetNewRequest,
    ChangePasswordRequest,
    TokenRefreshRequest,
    TokenRefreshResponse,
    VerifyInitiateRequest,
    VerifyCompleteRequest,
)
from src.Models.Users.user_repository import UserRepository
from src.Models.Users.password_reset_repository import PasswordResetRepository
from src.Services.image_service import ImageStorageService, validate_image_size
from src.Auth.jwt_token import create_access_token, create_refresh_token, verify_jwt_token
from src.config import get_settings

router = APIRouter(prefix="/user", tags=["Users"])
logger = get_logger("API.user")

_settings = get_settings()


def _clean_str(val: object | None) -> str | None:
    """Normalize empty string or null string representations to None."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() == "null" or s.lower() == "undefined":
        return None
    return s


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> UserRepository:
    return UserRepository(db)


async def get_reset_repo(db: AsyncClient = Depends(get_supabase_client)) -> PasswordResetRepository:
    return PasswordResetRepository(db)


async def get_image_storage(db: AsyncClient = Depends(get_supabase_client)) -> ImageStorageService:
    return ImageStorageService(db, bucket=_settings.supabase_user_data_bucket)


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
    logger.debug("Entering create_user: username=%s, email=%s", body.username, body.email)
    if await repo.get_by_username(body.username):
        logger.warning("Registration failed: Username '%s' already taken", body.username)
        raise HTTPException(status_code=409, detail="Username already taken.")

    if await repo.get_by_email(body.email):
        logger.warning("Registration failed: An account with email '%s' already exists", body.email)
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    user = await repo.create(body)
    logger.info("User created successfully: user_id=%s, username=%s", user.get("id"), user.get("username"))
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
    logger.debug("Entering login_user: identifier=%s", body.username)
    user = await repo.get_by_identifier(body.username)
    # Identical error for missing user AND wrong password — prevents username enumeration
    if not user:
        logger.warning("Login failed for identifier '%s': account not found", body.username)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    incoming_hash = repo.hash_password(body.password)
    if incoming_hash != user.get("password", ""):
        logger.warning("Login failed for user_id=%s: incorrect password", user.get("id"))
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    user.pop("password", None)
    logger.info("User logged in successfully: user_id=%s, username=%s", user.get("id"), user.get("username"))

    # Issue cryptographically signed JWT tokens
    access_token = create_access_token(user_id=user["id"], username=user["username"])
    refresh_token = create_refresh_token(user_id=user["id"], username=user["username"])
    expires_in_sec = _settings.jwt_access_token_expire_minutes * 60

    return UserLoginResponse(
        success=True,
        user=user,
        message="Login successful.",
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in_sec,
    )


# ── POST /user/refresh ────────────────────────────────────────────────────────
@router.post(
    "/refresh",
    response_model=TokenRefreshResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def refresh_user_token(
    body: TokenRefreshRequest,
    repo: UserRepository = Depends(get_repo),
):
    """Rotate JWT session tokens using a valid refresh token.

    Validates signature, expiration, and token type.
    Issues a new access token and rotated refresh token.
    """
    logger.debug("Entering refresh_user_token")
    payload = verify_jwt_token(body.refresh_token, expected_type="refresh")
    user_id = payload.get("sub")
    username = payload.get("username", "")

    user = await repo.get_by_id(user_id)
    if not user:
        logger.warning("Token refresh failed: User account %s no longer exists", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found or session terminated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Issue fresh rotated tokens
    new_access_token = create_access_token(user_id=user["id"], username=user["username"])
    new_refresh_token = create_refresh_token(user_id=user["id"], username=user["username"])
    expires_in_sec = _settings.jwt_access_token_expire_minutes * 60

    logger.info("Token refresh successful for user_id=%s, username=%s", user["id"], user["username"])
    return TokenRefreshResponse(
        success=True,
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=expires_in_sec,
        user=user,
    )


# ── GET /user/me ──────────────────────────────────────────────────────────────
@router.get(
    "/me",
    response_model=UserRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_my_profile(
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Retrieve the current authenticated user's profile.

    Requires X-User-Name and X-User-Token headers. Omits device headers.
    """
    logger.debug("Entering get_my_profile: identity (user_id=%s)", identity.user_id)
    user = await repo.get_by_id(identity.user_id)
    if not user:
        logger.warning("Get profile failed: User not found for user_id=%s", identity.user_id)
        raise HTTPException(status_code=404, detail="User not found.")
    logger.info("Retrieved profile for user_id=%s, username=%s", identity.user_id, user.get("username"))
    return user


# ── GET /user/me/avatar ───────────────────────────────────────────────────────
@router.get(
    "/me/avatar",
    summary="Download the authenticated user's avatar image binary",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_my_avatar(
    size: str = "medium",
    identity: Identity = Depends(get_user_identity),
    image_storage: ImageStorageService = Depends(get_image_storage),
):
    """Retrieve the current authenticated user's avatar JPEG binary.

    Supports size="small" (128x128), size="medium" (256x256), size="large" (512x512).
    """
    logger.debug("Entering get_my_avatar: user_id=%s, size=%s", identity.user_id, size)
    data = await image_storage.download_avatar(user_id=identity.user_id, size=size)
    if not data:
        raise HTTPException(status_code=404, detail="Avatar image not found.")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ── PATCH /user/me ────────────────────────────────────────────────────────────
@router.patch(
    "/me",
    response_model=UserRecord,
    summary="Update authenticated user profile (supports JSON or multipart avatar upload)",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "avatar": {
                                "type": "string",
                                "format": "binary",
                                "description": "Optional avatar image file (JPEG, PNG, WEBP, max 20MB raw). Stored in 3 resolutions.",
                            },
                            "email": {
                                "type": "string",
                                "description": "New email address",
                            },
                            "country_code": {
                                "type": "string",
                                "description": "Country dialling code, e.g. +60",
                            },
                            "mobile_number": {
                                "type": "string",
                                "description": "Mobile number without country code",
                            },
                            "custom_categories_json": {
                                "type": "string",
                                "description": "JSON array of custom category objects (max 8)",
                            },
                            "preferences_json": {
                                "type": "string",
                                "description": "JSON object of user UI and currency preferences",
                            },
                        },
                    }
                },
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/UserUpdateRequest"
                    }
                },
            }
        }
    },
)
async def update_my_profile(
    request: Request,
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
    image_storage: ImageStorageService = Depends(get_image_storage),
):
    """Update mutable profile fields for the authenticated user.

    Accepts both:
    - `application/json` body (`UserUpdateRequest`).
    - `multipart/form-data` with optional `avatar` file upload and form fields.

    When an avatar image is provided, it is compressed and uploaded as 3 resolutions
    (small 128x128, medium 256x256, large 512x512) to Supabase Storage at
    `{user_id}/avatar_images/`. The folder path is saved in `avatar_image_path`.

    All fields are optional — only supplied (non-null) values are written.
    Rejects duplicate emails (case-insensitive) with HTTP 409.
    Requires X-User-Name and X-User-Token headers.
    """
    content_type = request.headers.get("content-type", "").lower()
    avatar_bytes: bytes | None = None
    update_req: UserUpdateRequest

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()

        # 1. Extract avatar image bytes (UploadFile or raw)
        avatar_field = form.get("avatar")
        if avatar_field is not None:
            if hasattr(avatar_field, "read"):
                avatar_bytes = await avatar_field.read()
            elif isinstance(avatar_field, (bytes, bytearray)):
                avatar_bytes = bytes(avatar_field)
            elif isinstance(avatar_field, str) and avatar_field.strip():
                # Allow raw string if non-empty
                avatar_bytes = avatar_field.encode("utf-8")

        # 2. Check if a JSON 'body' field was passed in form data
        body_field = form.get("body")
        parsed_body_dict: dict = {}
        if body_field is not None:
            body_str = _clean_str(str(body_field))
            if body_str:
                try:
                    loaded = json.loads(body_str)
                    if isinstance(loaded, dict):
                        parsed_body_dict = loaded
                except Exception:
                    pass

        # 3. Parse custom categories and preferences
        cats = None
        cats_raw = form.get("custom_categories_json") or form.get("custom_categories")
        if cats_raw is not None:
            cats_str = _clean_str(str(cats_raw))
            if cats_str:
                try:
                    cats = [CustomCategorySchema(**c) for c in json.loads(cats_str)]
                except Exception as exc:
                    raise HTTPException(status_code=422, detail=f"Invalid custom_categories_json: {exc}") from exc

        prefs = None
        prefs_raw = form.get("preferences_json") or form.get("preferences")
        if prefs_raw is not None:
            if isinstance(prefs_raw, dict):
                prefs = prefs_raw
            else:
                prefs_str = _clean_str(str(prefs_raw))
                if prefs_str:
                    try:
                        loaded_prefs = json.loads(prefs_str)
                        if isinstance(loaded_prefs, dict):
                            prefs = loaded_prefs
                    except Exception as exc:
                        raise HTTPException(status_code=422, detail=f"Invalid preferences_json: {exc}") from exc
        elif "preferences" in parsed_body_dict and isinstance(parsed_body_dict["preferences"], dict):
            prefs = parsed_body_dict["preferences"]

        # 4. Resolve update fields
        email_val = _clean_str(form.get("email")) or parsed_body_dict.get("email")
        country_val = _clean_str(form.get("country_code")) or parsed_body_dict.get("country_code")
        mobile_val = _clean_str(form.get("mobile_number")) or parsed_body_dict.get("mobile_number")
        avatar_path_val = _clean_str(form.get("avatar_image_path")) or parsed_body_dict.get("avatar_image_path")
        if cats is None and "custom_categories" in parsed_body_dict:
            cats_list = parsed_body_dict.get("custom_categories")
            if isinstance(cats_list, list):
                cats = [CustomCategorySchema(**c) if isinstance(c, dict) else c for c in cats_list]

        update_req = UserUpdateRequest(
            email=email_val,
            country_code=country_val,
            mobile_number=mobile_val,
            avatar_image_path=avatar_path_val,
            custom_categories=cats,
            preferences=prefs,
        )
    else:
        # Default JSON parsing
        try:
            body_data = await request.json()
            if not isinstance(body_data, dict):
                body_data = {}
            update_req = UserUpdateRequest(**body_data)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON request body: {exc}") from exc

    logger.debug(
        "Entering update_my_profile: identity (user_id=%s), fields_to_update=%s",
        identity.user_id,
        [k for k, v in update_req.model_dump().items() if v is not None],
    )

    if update_req.email is not None:
        existing = await repo.get_by_email(update_req.email)
        if existing and existing.get("id") != identity.user_id:
            logger.warning("Profile update failed: Email '%s' already taken by another user", update_req.email)
            raise HTTPException(status_code=409, detail="An account with this email already exists.")

    # Handle avatar upload if present
    if avatar_bytes and len(avatar_bytes) > 0:
        validate_image_size(avatar_bytes, max_bytes=_settings.max_upload_size_bytes)

        folder_path = await image_storage.upload_avatar(
            user_id=identity.user_id,
            image_bytes=avatar_bytes,
            target_max_bytes=_settings.max_compressed_image_bytes,
        )
        update_req = UserUpdateRequest(
            email=update_req.email,
            country_code=update_req.country_code,
            mobile_number=update_req.mobile_number,
            avatar_image_path=folder_path,
            custom_categories=update_req.custom_categories,
            preferences=update_req.preferences,
        )
        logger.info(
            "update_my_profile: avatar uploaded → %s for user_id=%s",
            folder_path,
            identity.user_id,
        )

    updated = await repo.update_profile(identity.user_id, update_req)
    if not updated:
        logger.warning("Profile update failed: User not found for user_id=%s", identity.user_id)
        raise HTTPException(status_code=404, detail="User not found.")
    logger.info("Profile updated successfully for user_id=%s", identity.user_id)
    return updated


# ── DELETE /user/me ───────────────────────────────────────────────────────────
@router.delete(
    "/me",
    status_code=200,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def delete_my_profile(
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Soft-delete current authenticated user profile.

    Requires X-User-Name and X-User-Token headers. Omits device headers.
    """
    logger.debug("Entering delete_my_profile: identity (user_id=%s)", identity.user_id)
    deleted = await repo.soft_delete(identity.user_id)
    if not deleted:
        logger.warning("Soft-delete profile failed: User not found or already deleted for user_id=%s", identity.user_id)
        raise HTTPException(
            status_code=404,
            detail="User profile not found or already deleted.",
        )
    logger.info("User profile soft-deleted successfully: user_id=%s", identity.user_id)
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
    If in active 7-day cooldown, dispatches a Cooldown Advisory Email via Mailtrap SMTP.
    Otherwise, generates a secure 6-digit OTP and dispatches a password reset email via Mailtrap SMTP.
    """
    from src.Services.email_service import send_password_reset_email, send_password_reset_cooldown_email

    clean_identifier = body.identifier.strip()
    logger.debug("Entering initiate_password_reset: identifier=%s", clean_identifier)
    user = await user_repo.get_by_email_or_mobile(clean_identifier)

    if user:
        email_val = user.get("email")
        username = user.get("username", "User")

        # 0. Check 7-day password cooldown
        user_prefs = user.get("preferences")
        if isinstance(user_prefs, str):
            try:
                user_prefs = json.loads(user_prefs)
            except Exception:
                user_prefs = {}
        elif not isinstance(user_prefs, dict):
            user_prefs = {}

        last_changed_str = user_prefs.get("password_changed_at")
        if not last_changed_str:
            # Fallback to forget_password table's latest completed reset
            last_changed_str = await reset_repo.get_latest_reset_timestamp(user["id"])

        in_cooldown = False
        countdown_str = ""
        if last_changed_str:
            try:
                last_changed = datetime.fromisoformat(str(last_changed_str).replace("Z", "+00:00"))
                elapsed_seconds = (datetime.now(timezone.utc) - last_changed).total_seconds()
                cooldown_seconds = 7 * 86400  # 7 days
                if elapsed_seconds < cooldown_seconds:
                    in_cooldown = True
                    remaining_seconds = int(cooldown_seconds - elapsed_seconds)
                    remaining_days = remaining_seconds // 86400
                    remaining_hours = (remaining_seconds % 86400) // 3600
                    day_word = "day" if remaining_days == 1 else "days"
                    hour_word = "hour" if remaining_hours == 1 else "hours"
                    countdown_str = f"{remaining_days} {day_word} & {remaining_hours} {hour_word}"
            except (ValueError, TypeError):
                pass

        if in_cooldown:
            logger.warning(
                "Password reset requested during active cooldown for user_id=%s: %s remaining",
                user.get("id"),
                countdown_str,
            )
            if email_val:
                await send_password_reset_cooldown_email(
                    to_email=email_val,
                    countdown_str=countdown_str,
                    username=username,
                )
        else:
            otp_num = secrets.randbelow(900_000) + 100_000
            otp_str = str(otp_num)

            mobile_val = user.get("mobile_number")
            await reset_repo.create_reset_request(
                user_id=user["id"],
                email=email_val,
                mobile_number=mobile_val,
                otp=otp_str,
            )

            if email_val:
                await send_password_reset_email(to_email=email_val, otp=otp_str, username=username)

            logger.info("Password reset code generated and dispatched for user_id=%s (identifier='%s')", user.get("id"), clean_identifier)
    else:
        logger.warning("Password reset initiated for non-existent identifier='%s'", clean_identifier)

    logger.info("Password reset initiation completed for identifier='%s'", clean_identifier)
    return {
        "success": True,
        "message": "If an account with this email or mobile number exists, a verification code has been sent.",
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
    logger.debug("Entering verify_password_reset_otp: identifier=%s", clean_identifier)
    user = await user_repo.get_by_email_or_mobile(clean_identifier)

    if not user:
        logger.warning("OTP verification failed: Account not found for identifier='%s'", clean_identifier)
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset code. Please request a new code.",
        )

    success, msg, reset_token = await reset_repo.verify_otp(user["id"], body.otp)
    if not success:
        logger.warning("OTP verification failed for user_id=%s: %s", user.get("id"), msg)
        raise HTTPException(status_code=400, detail=msg)

    logger.info("OTP verified successfully for user_id=%s", user.get("id"))
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
    logger.debug("Entering complete_password_reset")
    new_hash = user_repo.hash_password(body.new_password)
    success, msg = await reset_repo.complete_reset(body.reset_token, new_hash)

    if not success:
        logger.warning("Password reset completion failed: %s", msg)
        raise HTTPException(status_code=400, detail=msg)

    logger.info("Password reset completed successfully")
    return {
        "success": True,
        "message": msg,
    }


# ── POST /user/change-password ───────────────────────────────────────────────
@router.post(
    "/change-password",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def change_password(
    body: ChangePasswordRequest,
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Change account password for an authenticated user.

    Requires current session authentication, verifies old password against stored hash,
    enforces password complexity, and strictly requires new_password != old_password.
    """
    logger.debug("Entering change_password for user_id=%s", identity.user_id)
    if not identity.is_authenticated or not identity.user_id:
        raise HTTPException(status_code=401, detail="Authentication required to change password.")

    user = await repo.get_by_id_with_password(identity.user_id)
    if not user:
        logger.warning("Password change failed: user_id=%s not found", identity.user_id)
        raise HTTPException(status_code=404, detail="User account not found.")

    # 0. Enforce 7-day rate-limiting cooldown per user
    user_prefs = user.get("preferences")
    if isinstance(user_prefs, str):
        try:
            user_prefs = json.loads(user_prefs)
        except Exception:
            user_prefs = {}
    elif not isinstance(user_prefs, dict):
        user_prefs = {}

    last_changed_str = user_prefs.get("password_changed_at")
    if last_changed_str:
        try:
            last_changed = datetime.fromisoformat(str(last_changed_str).replace("Z", "+00:00"))
            elapsed_seconds = (datetime.now(timezone.utc) - last_changed).total_seconds()
            cooldown_seconds = 7 * 86400  # 7 days = 604,800 seconds
            if elapsed_seconds < cooldown_seconds:
                remaining_seconds = cooldown_seconds - elapsed_seconds
                days_remaining = max(1, math.ceil(remaining_seconds / 86400))
                day_word = "day" if days_remaining == 1 else "days"
                logger.warning(
                    "Password change rate-limited for user_id=%s: %d %s remaining in 7-day cooldown",
                    identity.user_id, days_remaining, day_word
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Password can only be changed once every 7 days. Change allowed in {days_remaining} {day_word}.",
                )
        except (ValueError, TypeError):
            pass

    # 1. Verify old password
    old_hash = repo.hash_password(body.old_password)
    if old_hash != user.get("password"):
        logger.warning("Password change failed for user_id=%s: Incorrect old password", identity.user_id)
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    # 2. Reject if new password matches old password
    if body.new_password == body.old_password:
        logger.warning("Password change rejected for user_id=%s: new password equals old password", identity.user_id)
        raise HTTPException(status_code=400, detail="New password cannot be the same as your old password.")

    # 3. Hash, record cooldown timestamp, and update
    now_iso = datetime.now(timezone.utc).isoformat()
    updated_prefs = dict(user_prefs)
    updated_prefs["password_changed_at"] = now_iso

    new_hash = repo.hash_password(body.new_password)
    success = await repo.update_password(identity.user_id, new_hash, updated_preferences=updated_prefs)
    if not success:
        logger.error("Failed to update password in database for user_id=%s", identity.user_id)
        raise HTTPException(status_code=500, detail="Failed to update password.")

    logger.info("Password changed successfully for user_id=%s", identity.user_id)
    return {
        "success": True,
        "message": "Password changed successfully.",
        "password_changed_at": now_iso,
    }


# ── POST /user/verify-initiate ────────────────────────────────────────────────
@router.post(
    "/verify-initiate",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def verify_initiate(
    body: VerifyInitiateRequest,
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Initiate email verification by generating and dispatching a 6-digit OTP.

    - Validates email syntax (RFC 5322 basic pattern).
    - Enforces 60-second resend cooldown per user per type.
    - Generates a secure 6-digit OTP, stores its salted SHA-256 hash in Redis (TTL 600s).
    - Dispatches branded HTML + text email via Mailtrap SMTP.
    - Always returns HTTP 200 to prevent email enumeration.
    """
    from src.Infrastructure.redis_service import (
        generate_otp, store_otp, check_resend_cooldown, set_resend_cooldown
    )
    from src.Services.email_service import send_verification_email

    logger.debug("Entering verify_initiate: user_id=%s, type=%s", identity.user_id, body.type)
    if not identity.is_authenticated or not identity.user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")

    if body.type != "email":
        raise HTTPException(status_code=400, detail="Only email verification is currently supported.")

    identifier = body.identifier.strip().lower()

    # Basic RFC 5322 email syntax validation
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", identifier):
        raise HTTPException(status_code=422, detail="Invalid email address format.")

    # Check 60-second resend cooldown
    in_cooldown, seconds_remaining = check_resend_cooldown(identity.user_id, body.type)
    if in_cooldown:
        raise HTTPException(
            status_code=429,
            detail=f"Please wait {seconds_remaining} second(s) before requesting a new code.",
        )

    # Fetch user for username (used in email template)
    user = await repo.get_by_id(identity.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User account not found.")

    # Check uniqueness — if email differs from current, ensure it's not taken
    current_email = user.get("email", "").strip().lower()
    if identifier != current_email:
        existing = await repo.get_by_email(identifier)
        if existing and existing.get("id") != identity.user_id:
            # Return generic success to prevent email enumeration
            logger.info(
                "verify_initiate: email %s already claimed by another user; returning generic 200",
                identifier,
            )
            return {"success": True, "message": "Verification code dispatched.", "cooldown_seconds": 60}

    # Generate OTP, store hash, set cooldown
    otp = generate_otp()
    store_otp(identity.user_id, body.type, identifier, otp)
    set_resend_cooldown(identity.user_id, body.type)

    # Dispatch email (non-blocking via asyncio.to_thread)
    username = user.get("username", "User")
    await send_verification_email(to_email=identifier, otp=otp, username=username)

    logger.info("verify_initiate: OTP dispatched for user_id=%s to %s", identity.user_id, identifier)
    return {"success": True, "message": "Verification code dispatched.", "cooldown_seconds": 60}


# ── POST /user/verify-complete ────────────────────────────────────────────────
@router.post(
    "/verify-complete",
    response_model=UserRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def verify_complete(
    body: VerifyCompleteRequest,
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Complete email verification by validating the submitted 6-digit OTP.

    - Verifies OTP against Redis-stored salted SHA-256 hash.
    - Enforces 5-attempt brute-force lockout per OTP issuance.
    - On success: updates users.email and users.email_verified_at.
    - Returns updated UserRecord DTO.
    """
    from src.Infrastructure.redis_service import verify_otp
    from datetime import datetime, timezone

    logger.debug("Entering verify_complete: user_id=%s, type=%s", identity.user_id, body.type)
    if not identity.is_authenticated or not identity.user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")

    if body.type != "email":
        raise HTTPException(status_code=400, detail="Only email verification is currently supported.")

    identifier = body.identifier.strip().lower()

    # Verify OTP
    ok, error_msg = verify_otp(identity.user_id, body.type, identifier, body.otp)
    if not ok:
        logger.warning("verify_complete failed for user_id=%s: %s", identity.user_id, error_msg)
        raise HTTPException(status_code=400, detail=error_msg)

    # Mark email as verified (and update email if it changed)
    verified_at = datetime.now(timezone.utc)
    updated_user = await repo.set_email_verified(identity.user_id, identifier, verified_at)
    if not updated_user:
        logger.error("verify_complete: set_email_verified returned no data for user_id=%s", identity.user_id)
        raise HTTPException(status_code=500, detail="Failed to update email verification status.")

    logger.info("verify_complete: email verified for user_id=%s, email=%s", identity.user_id, identifier)
    return updated_user
