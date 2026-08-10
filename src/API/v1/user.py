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
)
from src.Models.Users.user_repository import UserRepository

router = APIRouter(prefix="/user", tags=["Users"])


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> UserRepository:
    return UserRepository(db)


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
