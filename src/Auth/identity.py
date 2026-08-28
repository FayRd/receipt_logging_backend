import secrets
from fastapi import Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Infrastructure.logger import get_logger
from src.Models.Devices.device_repository import DeviceRepository
from src.Models.Users.user_repository import UserRepository
from src.Auth.device_security import hash_device_token
from src.Auth.jwt_token import verify_jwt_token

logger = get_logger("Auth.identity")


class Identity(BaseModel):
    """Container for caller's cryptographically verified session identity."""
    user_id: str | None = None
    username: str | None = None
    device_id: str = ""      # DB UUID or device name
    device_name: str = ""    # e.g. MS701-A1B1

    @property
    def is_authenticated(self) -> bool:
        """True when the request carries a verified user session."""
        return self.user_id is not None


# ── 1. DEVICE SCOPED IDENTITY (/devices/me, /scan/*) ──────────────────────────
async def get_device_identity(
    x_device_name: str = Header(
        ...,
        alias="X-Device-Name",
        description="Mobile hardware/variant device name identifier string.",
    ),
    x_device_token: str = Header(
        ...,
        alias="X-Device-Token",
        description="Device secret fingerprint token generated on first app boot.",
    ),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for device-scoped routes (/devices/me, /scan/*).

    Requires X-Device-Name and X-Device-Token headers. Omits X-User-Name and X-User-Token.
    """
    clean_device_name = x_device_name.strip()
    clean_device_token = x_device_token.strip()

    logger.debug(
        "Parsing device headers: X-Device-Name/X-Device-ID='%s', X-Device-Token='%s'",
        clean_device_name,
        "[PRESENT]" if clean_device_token else "[EMPTY]",
    )

    if not clean_device_name or not clean_device_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Device-Name and X-Device-Token headers are required and cannot be empty.",
        )

    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(clean_device_name)

    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device. Please call POST /api/v1/devices/register first.",
        )

    incoming_hash = hash_device_token(clean_device_token)
    stored_hash = device.get("device_token_hash", "")

    if not secrets.compare_digest(incoming_hash.encode("utf-8"), stored_hash.encode("utf-8")):
        logger.warning("Constant-time digest comparison FAIL for device token (device_name='%s')", clean_device_name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )
    logger.debug("Constant-time digest comparison PASS for device token (device_name='%s')", clean_device_name)

    db_user_id = device.get("user_id")
    db_username = None
    if db_user_id:
        user_repo = UserRepository(db)
        user_row = await user_repo.get_by_id(db_user_id)
        if user_row:
            db_username = user_row.get("username")

    canonical_name = device.get("name", clean_device_name)
    identity = Identity(
        user_id=db_user_id,
        username=db_username,
        device_id=device["id"],
        device_name=canonical_name,
    )
    resolution_mode = "user" if identity.is_authenticated else "device"
    logger.info(
        "Identity resolved (mode=%s): user_id=%s, username=%s, device_id=%s, device_name=%s",
        resolution_mode, db_user_id, db_username, device["id"], canonical_name
    )
    return identity


# Alias for backward compatibility on device endpoints
get_current_identity = get_device_identity


# ── 2. USER AUTHENTICATED IDENTITY (/user/*, /receipts/*, /chat/*) ─────────────
async def get_user_identity(
    authorization: str | None = Header(
        None,
        alias="Authorization",
        description="Standard Bearer <JWT_ACCESS_TOKEN> authentication header.",
    ),
    x_user_name: str | None = Header(
        None,
        alias="X-User-Name",
        description="Authenticated username string (legacy header).",
    ),
    x_user_token: str | None = Header(
        None,
        alias="X-User-Token",
        description="Authenticated user password token string (legacy header).",
    ),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for user-scoped routes (/user/*, /receipts/*, /chat/*).

    Accepts standard 'Authorization: Bearer <token>' header (preferred, fast 0-query JWT check)
    or legacy X-User-Name + X-User-Token headers for backward compatibility.
    """
    # 1. Primary: JWT Bearer Token validation
    if authorization and authorization.strip().lower().startswith("bearer "):
        bearer_token = authorization.strip()[7:].strip()
        payload = verify_jwt_token(bearer_token, expected_type="access")
        user_id = payload.get("sub")
        username = payload.get("username", "")
        identity = Identity(
            user_id=user_id,
            username=username,
            device_id="",
            device_name="",
        )
        logger.debug("Identity resolved via JWT Bearer: user_id=%s, username=%s", user_id, username)
        return identity

    # 2. Legacy: Header-based password hash verification
    clean_username = x_user_name.strip() if x_user_name else ""
    clean_user_token = x_user_token.strip() if x_user_token else ""

    logger.debug(
        "Parsing user headers: X-User-Name/X-User-ID='%s', X-User-Token='%s'",
        clean_username,
        "[PRESENT]" if clean_user_token else "[EMPTY]",
    )

    if not clean_username or not clean_user_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide 'Authorization: Bearer <token>' or user credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_repo = UserRepository(db)
    user = await user_repo.get_by_identifier(clean_username)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found or invalid credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    stored_password_hash = user.get("password", "")
    incoming_user_hash = UserRepository.hash_password(clean_user_token)

    if not secrets.compare_digest(incoming_user_hash.encode("utf-8"), stored_password_hash.encode("utf-8")):
        logger.warning("Constant-time digest comparison FAIL for user token (username='%s')", clean_username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    logger.debug("Constant-time digest comparison PASS for user token (username='%s')", clean_username)

    identity = Identity(
        user_id=user["id"],
        username=user["username"],
        device_id="",
        device_name="",
    )
    logger.info("Identity resolved (mode=user): user_id=%s, username=%s", user["id"], user["username"])
    return identity


# Alias for backward compatibility on user-authenticated routes
require_user_identity = get_user_identity


# ── 3. LINK BRIDGE IDENTITY (POST /devices/link) ──────────────────────────────
async def require_link_bridge_identity(
    x_device_name: str = Header(..., alias="X-Device-Name"),
    x_device_token: str = Header(..., alias="X-Device-Token"),
    authorization: str | None = Header(None, alias="Authorization"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    x_user_token: str | None = Header(None, alias="X-User-Token"),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for POST /devices/link:

    Requires X-Device-Name and X-Device-Token to verify device identity.
    If Authorization Bearer JWT or (X-User-Name and X-User-Token) are present,
    also verifies user credentials (for linking).
    Allows device-only authorization for unlinking (when user headers are omitted).
    """
    clean_device_name = x_device_name.strip() if x_device_name else ""
    clean_device_token = x_device_token.strip() if x_device_token else ""

    logger.debug(
        "Parsing link bridge headers: X-Device-Name/X-Device-ID='%s', X-Device-Token='%s', Authorization='%s', X-User-Name/X-User-ID='%s', X-User-Token='%s'",
        clean_device_name,
        "[PRESENT]" if clean_device_token else "[EMPTY]",
        "[BEARER]" if authorization else "[NONE]",
        x_user_name,
        "[PRESENT]" if x_user_token else "[NONE]",
    )

    if not clean_device_name or not clean_device_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Device-Name and X-Device-Token headers are required for POST /devices/link.",
        )

    # 1. Verify Device Identity
    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(clean_device_name)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device.",
        )

    incoming_device_hash = hash_device_token(clean_device_token)
    stored_device_hash = device.get("device_token_hash", "")
    if not secrets.compare_digest(incoming_device_hash.encode("utf-8"), stored_device_hash.encode("utf-8")):
        logger.warning("Constant-time digest comparison FAIL for link bridge device token (device_name='%s')", clean_device_name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )
    logger.debug("Constant-time digest comparison PASS for link bridge device token (device_name='%s')", clean_device_name)

    # 2. Verify User Identity if user credentials or JWT Bearer are provided
    db_user_id = device.get("user_id")
    db_username = None
    if authorization and authorization.strip().lower().startswith("bearer "):
        bearer_token = authorization.strip()[7:].strip()
        payload = verify_jwt_token(bearer_token, expected_type="access")
        db_user_id = payload.get("sub")
        db_username = payload.get("username", "")
        logger.debug("Link bridge user identity resolved via JWT Bearer: user_id=%s, username=%s", db_user_id, db_username)
    elif x_user_name and x_user_token:
        clean_username = x_user_name.strip()
        clean_user_token = x_user_token.strip()

        user_repo = UserRepository(db)
        user = await user_repo.get_by_identifier(clean_username)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account not found or invalid credentials.",
            )

        stored_password_hash = user.get("password", "")
        incoming_user_hash = UserRepository.hash_password(clean_user_token)
        if not secrets.compare_digest(incoming_user_hash.encode("utf-8"), stored_password_hash.encode("utf-8")):
            logger.warning("Constant-time digest comparison FAIL for link bridge user token (username='%s')", clean_username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user authentication token.",
            )
        logger.debug("Constant-time digest comparison PASS for link bridge user token (username='%s')", clean_username)
        db_user_id = user["id"]
        db_username = user["username"]

    canonical_name = device.get("name", clean_device_name)
    identity = Identity(
        user_id=db_user_id,
        username=db_username,
        device_id=device["id"],
        device_name=canonical_name,
    )
    resolution_mode = "user" if identity.is_authenticated else "device"
    logger.info(
        "Identity resolved (mode=%s): user_id=%s, username=%s, device_id=%s, device_name=%s",
        resolution_mode, db_user_id, db_username, device["id"], canonical_name
    )
    return identity


# Alias for device link route dependency
require_link_identity = require_link_bridge_identity


# ── 5. SCOPED IDENTITY — X-Request-Type (POST /scan/*, POST /chat/query) ───────
async def get_scoped_identity(
    x_request_type: str = Header(
        ...,
        alias="X-Request-Type",
        description="Request mode: 'user' or 'guest'. Determines which credential headers are required.",
    ),
    authorization: str | None = Header(None, alias="Authorization"),
    x_device_name: str | None = Header(None, alias="X-Device-Name"),
    x_device_token: str | None = Header(None, alias="X-Device-Token"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    x_user_token: str | None = Header(None, alias="X-User-Token"),
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for /scan/* and /chat/query endpoints supporting both Guest and User modes.

    X-Request-Type: 'guest'
        - Requires: X-Device-Name, X-Device-Token
        - Must omit: X-User-Name, X-User-Token

    X-Request-Type: 'user'
        - Requires: Authorization Bearer JWT OR (X-User-Name + X-User-Token)
        - Must omit: X-Device-Name, X-Device-Token
    """
    req_type = x_request_type.strip().lower()

    logger.debug(
        "Parsing scoped identity headers: X-Request-Type='%s', X-Device-Name/X-Device-ID='%s', X-Device-Token='%s', X-User-Name/X-User-ID='%s', X-User-Token='%s'",
        req_type,
        x_device_name,
        "[PRESENT]" if x_device_token else "[NONE]",
        x_user_name,
        "[PRESENT]" if x_user_token else "[NONE]",
    )

    if req_type == "guest":
        # User credential headers must be absent to prevent header confusion
        if x_user_name or x_user_token or authorization:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="For X-Request-Type 'guest', user headers (Authorization, X-User-Name, X-User-Token) must be omitted.",
            )
        if not x_device_name or not x_device_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="For X-Request-Type 'guest', X-Device-Name and X-Device-Token headers are required.",
            )
        # Delegate to device identity verification (reuses existing logic)
        clean_device_name = x_device_name.strip()
        clean_device_token = x_device_token.strip()
        device_repo = DeviceRepository(db)
        device = await device_repo.get_by_device_id(clean_device_name)
        if not device:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unregistered device. Please call POST /api/v1/devices/register first.",
            )
        incoming_hash = hash_device_token(clean_device_token)
        stored_hash = device.get("device_token_hash", "")
        if not secrets.compare_digest(incoming_hash.encode("utf-8"), stored_hash.encode("utf-8")):
            logger.warning("Constant-time digest comparison FAIL for guest device token (device_name='%s')", clean_device_name)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid device authentication token.",
            )
        logger.debug("Constant-time digest comparison PASS for guest device token (device_name='%s')", clean_device_name)

        canonical_name = device.get("name", clean_device_name)
        identity = Identity(
            user_id=None,
            username=None,
            device_id=device["id"],
            device_name=canonical_name,
        )
        logger.info(
            "Identity resolved (mode=guest): user_id=None, username=None, device_id=%s, device_name=%s",
            device["id"], canonical_name
        )
        return identity

    elif req_type == "user":
        # Device credential headers must be absent to prevent header confusion
        if x_device_name or x_device_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="For X-Request-Type 'user', device headers (X-Device-Name, X-Device-Token) must be omitted.",
            )

        # 1. Primary: JWT Bearer validation
        if authorization and authorization.strip().lower().startswith("bearer "):
            bearer_token = authorization.strip()[7:].strip()
            payload = verify_jwt_token(bearer_token, expected_type="access")
            user_id = payload.get("sub")
            username = payload.get("username", "")
            identity = Identity(
                user_id=user_id,
                username=username,
                device_id="",
                device_name="",
            )
            logger.debug("Identity resolved via JWT Bearer (mode=user): user_id=%s, username=%s", user_id, username)
            return identity

        # 2. Legacy fallback: X-User-Name + X-User-Token
        if not x_user_name or not x_user_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="For X-Request-Type 'user', Authorization Bearer token or (X-User-Name and X-User-Token) is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Delegate to user identity verification (reuses existing logic)
        clean_username = x_user_name.strip()
        clean_user_token = x_user_token.strip()
        user_repo = UserRepository(db)
        user = await user_repo.get_by_identifier(clean_username)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account not found or invalid credentials.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        stored_password_hash = user.get("password", "")
        incoming_user_hash = UserRepository.hash_password(clean_user_token)
        if not secrets.compare_digest(incoming_user_hash.encode("utf-8"), stored_password_hash.encode("utf-8")):
            logger.warning("Constant-time digest comparison FAIL for user token (username='%s')", clean_username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user authentication token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        logger.debug("Constant-time digest comparison PASS for user token (username='%s')", clean_username)

        identity = Identity(
            user_id=user["id"],
            username=user["username"],
            device_id="",
            device_name="",
        )
        logger.info("Identity resolved (mode=user): user_id=%s, username=%s", user["id"], user["username"])
        return identity

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Request-Type header value. Must be 'user' or 'guest'.",
        )


# ── 4. SSE STREAMING IDENTITY ──────────────────────────────────────────────────
async def get_sse_identity(
    x_request_type: str | None = Header(None, alias="X-Request-Type", description="Request mode: 'user' or 'guest'"),
    authorization: str | None = Header(None, alias="Authorization", description="Standard Bearer <JWT> token"),
    token: str | None = Query(None, alias="token", description="JWT access token parameter for EventSource connections"),
    access_token: str | None = Query(None, alias="access_token", description="JWT access token parameter for EventSource connections"),
    x_device_name: str | None = Header(None, alias="X-Device-Name", description="Mobile hardware/variant device name identifier string"),
    x_device_token: str | None = Header(None, alias="X-Device-Token", description="Device secret fingerprint token"),
    x_user_name: str | None = Header(None, alias="X-User-Name", description="Authenticated username"),
    x_user_token: str | None = Header(None, alias="X-User-Token", description="Authenticated user password token"),
    device_name_param: str | None = Query(None, alias="device_name", description="Device name query parameter (alternative to headers for SSE EventSource)"),
    device_token_param: str | None = Query(None, alias="device_token", description="Device token query parameter (alternative to headers for SSE EventSource)"),
    user_name_param: str | None = Query(None, alias="username", description="Username query parameter (alternative to headers for SSE EventSource in user mode)"),
    user_token_param: str | None = Query(None, alias="user_token", description="User token query parameter (alternative to headers for SSE EventSource in user mode)"),
    request: Request = None,
    db: AsyncClient = Depends(get_supabase_client),
) -> Identity:
    """FastAPI dependency for SSE endpoints supporting both User and Guest modes via HTTP Headers or URL Query Parameters."""
    req_type = (x_request_type or "").strip().lower()

    # 1. JWT Bearer / Query Token resolution for User mode
    jwt_raw = None
    if authorization and authorization.strip().lower().startswith("bearer "):
        jwt_raw = authorization.strip()[7:].strip()
    elif token and token.strip():
        jwt_raw = token.strip()
    elif access_token and access_token.strip():
        jwt_raw = access_token.strip()

    if jwt_raw:
        payload = verify_jwt_token(jwt_raw, expected_type="access")
        user_id = payload.get("sub")
        username_val = payload.get("username", "")
        identity = Identity(
            user_id=user_id,
            username=username_val,
            device_id="",
            device_name="",
        )
        logger.debug("Identity resolved via JWT for SSE: user_id=%s, username=%s", user_id, username_val)
        return identity

    device_name = (
        x_device_name
        or (request.headers.get("X-Device-ID") if request else None)
        or device_name_param
        or (request.query_params.get("device_id") if request else None)
        or ""
    ).strip()

    device_token = (
        x_device_token
        or device_token_param
        or (request.headers.get("X-Device-Token") if request else None)
        or (request.query_params.get("device_token") if request else None)
        or ""
    ).strip()

    username = (
        x_user_name
        or user_name_param
        or (request.headers.get("X-User-ID") if request else None)
        or (request.query_params.get("user_id") if request else None)
        or ""
    ).strip()

    user_token = (
        x_user_token
        or user_token_param
        or (request.headers.get("X-User-Token") if request else None)
        or (request.query_params.get("user_token") if request else None)
        or ""
    ).strip()

    logger.debug(
        "Parsing SSE identity (req_type='%s'): device_name='%s', device_token='%s', username='%s', user_token='%s'",
        req_type,
        device_name,
        "[PRESENT]" if device_token else "[EMPTY]",
        username,
        "[PRESENT]" if user_token else "[EMPTY]",
    )

    # 1. USER MODE RESOLUTION
    if req_type == "user" or (not req_type and username and user_token and not device_name):
        if not username or not user_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User authentication required via Authorization header, token query, or credentials.",
            )
        user_repo = UserRepository(db)
        user = await user_repo.get_by_identifier(username)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account not found or invalid credentials.",
            )
        stored_password_hash = user.get("password", "")
        incoming_user_hash = UserRepository.hash_password(user_token)
        if not secrets.compare_digest(incoming_user_hash.encode("utf-8"), stored_password_hash.encode("utf-8")):
            logger.warning("Constant-time digest comparison FAIL for SSE user token (username='%s')", username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user authentication token.",
            )
        identity = Identity(
            user_id=user["id"],
            username=user["username"],
            device_id="",
            device_name="",
        )
        logger.info("Identity resolved (mode=user/sse): user_id=%s, username=%s", user["id"], user["username"])
        return identity

    # 2. GUEST MODE RESOLUTION
    if not device_name or not device_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authentication required via user headers/query params or device headers/query params.",
        )

    device_repo = DeviceRepository(db)
    device = await device_repo.get_by_device_id(device_name)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unregistered device.",
        )

    incoming_hash = hash_device_token(device_token)
    stored_hash = device.get("device_token_hash", "")

    if not secrets.compare_digest(incoming_hash.encode("utf-8"), stored_hash.encode("utf-8")):
        logger.warning("Constant-time digest comparison FAIL for SSE device token (device_name='%s')", device_name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device authentication token.",
        )

    canonical_name = device.get("name", device_name)
    identity = Identity(
        user_id=None,
        username=None,
        device_id=device["id"],
        device_name=canonical_name,
    )
    logger.info(
        "Identity resolved (mode=device/sse): user_id=None, username=None, device_id=%s, device_name=%s",
        device["id"], canonical_name
    )
    return identity
