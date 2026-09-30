import json
import math
import re
import secrets
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_user_identity, get_scoped_identity
from src.Auth.rate_limiter import rate_limit
from src.Auth.google_auth import verify_google_id_token
from src.Infrastructure.logger import get_logger
from src.Models.schemas import (
    UserCreateRequest,
    UserLoginRequest,
    UserRecord,
    UserLoginResponse,
    GoogleAuthRequest,
    GoogleAuthResponse,
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
    QuotaStatusResponse,
    UserStatsResponse,
    AdScanGrantResponse,
)
from src.Models.Users.user_repository import UserRepository
from src.Models.Users.password_reset_repository import PasswordResetRepository
from src.Services.image_service import ImageStorageService, validate_image_size
from src.Auth.jwt_token import create_access_token, create_refresh_token, verify_jwt_token
from src.Infrastructure.redis_service import is_contact_change_cooldown_active, set_contact_change_cooldown
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
    stored_hash = user.get("password") or ""
    if not stored_hash or not secrets.compare_digest(incoming_hash, stored_hash):
        logger.warning("Login failed for user_id=%s: incorrect password or passwordless account", user.get("id"))
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


async def _download_and_upload_google_avatar(
    user_id: str,
    picture_url: str | None,
    image_storage: ImageStorageService,
) -> str | None:
    """Download Google profile picture binary and store in Supabase Storage in 3 resolutions."""
    if not picture_url or not picture_url.startswith("http"):
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(picture_url)
            if resp.status_code == 200 and resp.content:
                folder_path = await image_storage.upload_avatar(
                    user_id=user_id,
                    image_bytes=resp.content,
                    target_max_bytes=_settings.max_compressed_image_bytes,
                )
                logger.info(
                    "google_auth: ingested Google avatar to object storage -> %s for user_id=%s",
                    folder_path,
                    user_id,
                )
                return folder_path
            else:
                logger.warning(
                    "google_auth: failed to download Google avatar: HTTP %s", resp.status_code
                )
    except Exception as exc:
        logger.warning("google_auth: error downloading/uploading Google avatar: %s", exc)
    return None


async def _suggest_google_username(email: str, name: str, repo: UserRepository) -> str:
    """Generate an available 3-10 character username based on Google display name or email."""
    base = re.sub(r"[^a-zA-Z0-9_]", "", name.lower()) if name else ""
    if not base:
        base = email.split("@")[0]
        base = re.sub(r"[^a-zA-Z0-9_]", "", base.lower())
    suggested = base[:10]
    if len(suggested) < 3:
        suggested = f"{suggested}user"[:10]
    if await repo.get_by_username(suggested):
        suffix = secrets.token_hex(2)
        suggested = f"{suggested[:6]}_{suffix}"[:10]
    return suggested


async def _handle_google_unverified_claim(
    user_by_email: dict,
    google_id: str,
    picture: str | None,
    repo: UserRepository,
    image_storage: ImageStorageService,
) -> GoogleAuthResponse:
    """Claim an unverified account with Google OAuth (OWASP Pre-Account Takeover Defense)."""
    logger.info("google_auth: claiming unverified account id=%s with google_id=%s", user_by_email.get("id"), google_id)
    claimed_user = await repo.claim_unverified_account_with_google(user_by_email["id"], google_id)
    claimed_user.pop("password", None)

    if not claimed_user.get("avatar_image_path") and picture:
        avatar_folder = await _download_and_upload_google_avatar(
            user_id=claimed_user["id"],
            picture_url=picture,
            image_storage=image_storage,
        )
        if avatar_folder:
            await repo.update_profile(
                claimed_user["id"],
                UserUpdateRequest(avatar_image_path=avatar_folder),
            )
            claimed_user["avatar_image_path"] = avatar_folder

    access_token = create_access_token(user_id=claimed_user["id"], username=claimed_user["username"])
    refresh_token = create_refresh_token(user_id=claimed_user["id"], username=claimed_user["username"])
    expires_in_sec = _settings.jwt_access_token_expire_minutes * 60
    return GoogleAuthResponse(
        success=True,
        needs_username=False,
        is_new_user=False,
        user=claimed_user,
        message="Account linked and verified with Google successfully.",
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in_sec,
    )


# ── POST /user/auth/google ───────────────────────────────────────────────────
@router.post(
    "/auth/google",
    response_model=GoogleAuthResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_auth_per_minute))],
)
async def google_auth(
    body: GoogleAuthRequest,
    repo: UserRepository = Depends(get_repo),
    image_storage: ImageStorageService = Depends(get_image_storage),
):
    """Authenticate or register a user via Google OAuth (OIDC ID Token).

    Flow:
    1. Cryptographically verify the Google ID token.
    2. If active user exists with this google_id, log them in immediately (is_new_user=False).
    3. If active user exists with this email:
       - If email is already verified: reject with 409 Conflict ("Email already in use.")
       - If email is unverified: claim account (link google_id, verify email, purge password stub, ingest avatar if missing, is_new_user=False).
    4. If new user:
       - If username not provided, return needs_username: True with suggested username.
       - If username provided, validate uniqueness & format, create user, ingest avatar to Supabase Storage, return is_new_user=True.
    """
    logger.debug("Entering google_auth")
    try:
        claims = verify_google_id_token(body.id_token, client_id=_settings.google_client_id)
    except ValueError as e:
        logger.warning("google_auth: token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid Google authentication token.")

    google_id = str(claims.get("sub", "")).strip()
    email = str(claims.get("email", "")).strip().lower()
    name = str(claims.get("name", "")).strip()
    picture = claims.get("picture")

    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Invalid token claims: missing subject or email.")

    # 1. Existing user by Google ID
    user_by_gid = await repo.get_by_google_id(google_id)
    if user_by_gid:
        user_by_gid.pop("password", None)
        logger.info("google_auth: logged in existing user by google_id=%s, user_id=%s", google_id, user_by_gid.get("id"))
        access_token = create_access_token(user_id=user_by_gid["id"], username=user_by_gid["username"])
        refresh_token = create_refresh_token(user_id=user_by_gid["id"], username=user_by_gid["username"])
        expires_in_sec = _settings.jwt_access_token_expire_minutes * 60
        return GoogleAuthResponse(
            success=True,
            needs_username=False,
            is_new_user=False,
            user=user_by_gid,
            message="Login successful.",
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=expires_in_sec,
        )

    # 2. Existing user by email
    user_by_email = await repo.get_by_email(email)
    if user_by_email:
        # Check if already verified
        if user_by_email.get("email_verified_at") is not None:
            logger.warning("google_auth: conflict - email '%s' already verified with account id=%s", email, user_by_email.get("id"))
            raise HTTPException(status_code=409, detail="Email already in use.")

        # Unverified account claim (OWASP Pre-Account Takeover Defense)
        return await _handle_google_unverified_claim(
            user_by_email=user_by_email,
            google_id=google_id,
            picture=picture,
            repo=repo,
            image_storage=image_storage,
        )

    # 3. New User Registration
    clean_username = body.username.strip() if body.username else ""
    if not clean_username:
        suggested = await _suggest_google_username(email=email, name=name, repo=repo)
        logger.info("google_auth: new Google user '%s' requires username selection. Suggested: '%s'", email, suggested)
        return GoogleAuthResponse(
            success=True,
            needs_username=True,
            is_new_user=False,
            suggested_username=suggested,
            email=email,
            display_name=name,
            message="Username required to complete registration.",
        )

    # Validate provided username format
    if not re.match(r"^[a-zA-Z0-9_]{3,10}$", clean_username):
        raise HTTPException(
            status_code=422,
            detail="Username must be 3-10 characters (letters, numbers, and underscores only).",
        )

    if await repo.get_by_username(clean_username):
        logger.warning("google_auth: chosen username '%s' already taken", clean_username)
        raise HTTPException(status_code=409, detail="Username already taken.")

    # Create new user authenticated via Google
    new_user = await repo.create_google_user(
        google_id=google_id,
        email=email,
        username=clean_username,
        avatar_image_path=None,
        preferences=body.preferences,
    )
    new_user.pop("password", None)

    # Ingest Google profile picture into Supabase Object Storage
    avatar_folder = await _download_and_upload_google_avatar(
        user_id=new_user["id"],
        picture_url=picture,
        image_storage=image_storage,
    )
    if avatar_folder:
        await repo.update_profile(
            new_user["id"],
            UserUpdateRequest(avatar_image_path=avatar_folder),
        )
        new_user["avatar_image_path"] = avatar_folder

    logger.info("google_auth: successfully registered user_id=%s, username=%s via Google (avatar=%s)", new_user.get("id"), clean_username, new_user.get("avatar_image_path"))

    access_token = create_access_token(user_id=new_user["id"], username=new_user["username"])
    refresh_token = create_refresh_token(user_id=new_user["id"], username=new_user["username"])
    expires_in_sec = _settings.jwt_access_token_expire_minutes * 60

    return GoogleAuthResponse(
        success=True,
        needs_username=False,
        is_new_user=True,
        user=new_user,
        message="User registered and logged in successfully.",
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


# ── GET /user/me/stats ────────────────────────────────────────────────────────
@router.get(
    "/me/stats",
    response_model=UserStatsResponse,
    summary="Get user receipt count, time saved, trial and downgrade discount status",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_user_stats(
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Retrieve statistical aggregates for the current user:
    - Total receipts scanned and saved
    - Estimated time saved by scanning using the fast AI vision model (16.5s per scan avg)
    - 14-day trial status and downgrade discount offer expiry window
    """
    logger.debug("Entering get_user_stats: user_id=%s", identity.user_id)
    stats = await repo.get_user_stats(identity.user_id)
    return UserStatsResponse(
        success=True,
        total_receipts=stats["total_receipts"],
        time_saved_seconds=stats["time_saved_seconds"],
        time_saved_minutes=stats["time_saved_minutes"],
        trial_start_at=stats.get("trial_start_at"),
        discount_offer_shown_at=stats.get("discount_offer_shown_at"),
        tier=stats.get("tier", "free"),
        is_in_trial=stats.get("is_in_trial", False),
        ad_scans_today=stats.get("ad_scans_today", 0),
        ad_scans_remaining=stats.get("ad_scans_remaining", 5),
    )


# ── POST /user/me/ad-scan-grant ───────────────────────────────────────────────
@router.post(
    "/me/ad-scan-grant",
    response_model=AdScanGrantResponse,
    summary="Claim +1 scan bonus after watching a rewarded video ad (max 5/day)",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def grant_ad_scan(
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Grant 1 additional scan after completing a rewarded ad (max 5 ad scans per day)."""
    logger.debug("Entering grant_ad_scan: user_id=%s", identity.user_id)
    granted, count, msg = await repo.grant_ad_scan(identity.user_id)
    if not granted:
        raise HTTPException(status_code=400, detail=msg)
    return AdScanGrantResponse(
        success=True,
        ad_scans_today=count,
        ad_scans_remaining=max(0, 5 - count),
        message=f"Ad scan reward granted. You now have {max(0, 5 - count)} ad scans remaining today.",
    )


# ── POST /user/me/simulate-trial-expiry ────────────────────────────────────────
@router.post(
    "/me/simulate-trial-expiry",
    response_model=UserStatsResponse,
    summary="Debug/Test: Fast-forward trial timestamp to 15 days ago and apply downgrade to Free tier",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def simulate_trial_expiry_endpoint(
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Simulate 14-day trial expiration for developer testing and verify the downgrade workflow."""
    settings = get_settings()
    if settings.environment.lower() not in ("development", "dev"):
        raise HTTPException(
            status_code=403,
            detail="Simulation endpoints are only available in development environment (ENVIRONMENT=development).",
        )
    logger.debug("Entering simulate_trial_expiry_endpoint: user_id=%s", identity.user_id)
    updated_user = await repo.simulate_trial_expiry(identity.user_id)
    if not updated_user:
        raise HTTPException(status_code=404, detail="User account not found.")

    stats = await repo.get_user_stats(identity.user_id)
    return UserStatsResponse(
        success=True,
        total_receipts=stats["total_receipts"],
        time_saved_seconds=stats["time_saved_seconds"],
        time_saved_minutes=stats["time_saved_minutes"],
        trial_start_at=stats.get("trial_start_at"),
        discount_offer_shown_at=stats.get("discount_offer_shown_at"),
        tier=stats.get("tier", "free"),
        is_in_trial=stats.get("is_in_trial", False),
        ad_scans_today=stats.get("ad_scans_today", 0),
        ad_scans_remaining=stats.get("ad_scans_remaining", 5),
    )


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
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
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

    # Check if contact fields are changing
    email_changing = False
    new_email: str | None = None
    current_user = None

    if update_req.email is not None:
        new_email = update_req.email.strip().lower()
        current_user = await repo.get_by_id(identity.user_id)
        current_email = (current_user.get("email") or "").strip().lower() if current_user else ""
        if new_email and new_email != current_email:
            email_changing = True

    mobile_changing = False
    if update_req.country_code is not None or update_req.mobile_number is not None:
        if not current_user:
            current_user = await repo.get_by_id(identity.user_id)
        curr_cc = current_user.get("country_code") if current_user else None
        curr_mn = current_user.get("mobile_number") if current_user else None
        if (update_req.country_code is not None and update_req.country_code != curr_cc) or (
            update_req.mobile_number is not None and update_req.mobile_number != curr_mn
        ):
            mobile_changing = True

    if email_changing or mobile_changing:
        if is_contact_change_cooldown_active(identity.user_id):
            logger.warning(
                "Profile update rejected: 5-minute contact cooldown active for user_id=%s",
                identity.user_id,
            )
            raise HTTPException(
                status_code=429,
                detail="Please wait and try again later.",
            )

    if email_changing and new_email:
        existing = await repo.get_by_email(new_email)
        if existing and existing.get("id") != identity.user_id:
            if existing.get("email_verified_at"):
                logger.warning(
                    "Profile update failed: Email '%s' already taken and verified by %s",
                    new_email,
                    existing.get("id"),
                )
                raise HTTPException(status_code=409, detail="An account with this email already exists.")
            else:
                displaced_email = f"unverified_{existing['id']}@sancfund.internal"
                logger.info(
                    "Displacing unverified account email: user_id=%s, email=%s -> %s",
                    existing["id"],
                    new_email,
                    displaced_email,
                )
                await repo.update_profile(existing["id"], UserUpdateRequest(email=displaced_email))

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

    if email_changing or mobile_changing:
        set_contact_change_cooldown(identity.user_id, ttl_seconds=300)
        logger.info("5-minute contact change cooldown activated for user_id=%s", identity.user_id)

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
                    countdown_str = f"{remaining_days} {day_word} and {remaining_hours} {hour_word}"
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

    # Re-evaluate trial eligibility if trial was pending verification
    updated_user = await repo.evaluate_and_apply_deferred_trial(identity.user_id, identifier, updated_user)

    logger.info("verify_complete: email verified for user_id=%s, email=%s", identity.user_id, identifier)
    return updated_user


# ── GET /user/quota ──────────────────────────────────────────────────────────
@router.get(
    "/quota",
    response_model=QuotaStatusResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_health_per_minute))],
)
async def get_user_quota(
    identity: Identity = Depends(get_scoped_identity),
    user_repo: UserRepository = Depends(get_repo),
):
    """Retrieve caller's current tier, scan quota, chat token quota, and reset countdown."""
    from src.Services.quota_service import get_quota_service
    quota_svc = get_quota_service()
    status = await quota_svc.get_quota_status(identity, user_repo)
    return status

