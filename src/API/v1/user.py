from fastapi import APIRouter, Depends, HTTPException
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Models.schemas import (
    UserCreateRequest,
    UserLoginRequest,
    UserRecord,
    UserLoginResponse,
)
from src.Models.Users.user_repository import UserRepository

router = APIRouter(prefix="/user", tags=["Users"])


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> UserRepository:
    return UserRepository(db)


# ── POST /user/create ─────────────────────────────────────────────────────────
@router.post("/create", response_model=UserRecord, status_code=201)
async def create_user(
    body: UserCreateRequest,
    repo: UserRepository = Depends(get_repo),
):
    """Register a new user account. Rejects duplicate usernames (case-insensitive)."""
    existing = await repo.get_by_username(body.username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already taken.")

    user = await repo.create(body)
    return user


# ── POST /user/login ──────────────────────────────────────────────────────────
@router.post("/login", response_model=UserLoginResponse)
async def login_user(
    body: UserLoginRequest,
    repo: UserRepository = Depends(get_repo),
):
    """Authenticate user credentials and return sanitized user profile.

    The client sends a pre-encrypted password string. The backend applies
    a server-side PBKDF2/SHA-256 hash on top before comparing against the
    stored hash, preventing pass-the-hash attacks.
    """
    user = await repo.get_by_username(body.username)
    # Identical error for missing user AND wrong password — prevents username enumeration
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    incoming_hash = repo.hash_password(body.password)
    if incoming_hash != user.get("password", ""):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    user.pop("password", None)
    return UserLoginResponse(success=True, user=user, message="Login successful.")


# ── GET /user/{user_id} ───────────────────────────────────────────────────────
@router.get("/{user_id}", response_model=UserRecord)
async def get_user(
    user_id: str,
    repo: UserRepository = Depends(get_repo),
):
    """Retrieve a specific user's public profile by UUID. Password is never returned."""
    user = await repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user
