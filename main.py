import time
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import redis.asyncio as aioredis
from google import genai
from src.config import get_settings
from src.Infrastructure.logger import setup_logging, get_logger, set_request_id
from src.Infrastructure.database import get_supabase_client, close_supabase_client
from src.API.v1 import health, scan, receipts, user, devices, chat, help

# Initialize centralized logging
setup_logging()
logger = get_logger("HTTP")

redis_client: aioredis.Redis | None = None
genai_client: genai.Client | None = None


# ── LIFESPAN INTEGRATION ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, genai_client
    settings = get_settings()
    logger.info(f"Starting Receipt API in {settings.environment} mode (Logging: {settings.enable_file_logging})")

    # Initialize Async Redis client
    redis_client = aioredis.from_url(settings.redis_connection_string, decode_responses=True)

    # Initialize Google Gen AI async client
    genai_client = genai.Client(api_key=settings.gemini_api_key)

    # Pass redis client to scan router module
    scan.init_redis_client(redis_client)

    # Pre-warm Supabase async client singleton
    try:
        await get_supabase_client()
    except Exception as e:
        logger.warning(f"Failed to pre-warm Supabase client during startup: {e}")

    yield

    logger.info("Shutting down Receipt API")
    await close_supabase_client()
    if redis_client:
        await redis_client.aclose()


app = FastAPI(
    title="Receipt Scanner API",
    description="AI-powered receipt scanning and tracking backend",
    version="1.0.0",
    lifespan=lifespan,
)

# ── REQUEST TRACE MIDDLEWARE ──────────────────────────────────────────────────

@app.middleware("http")
async def request_trace_logging_middleware(request: Request, call_next):
    req_id = set_request_id(request.headers.get("X-Request-ID"))
    start_time = time.perf_counter()

    client_ip = request.client.host if request.client else "unknown"
    dev_id = request.headers.get("X-Device-ID", "-")
    user_name = request.headers.get("X-User-Name", "-")

    logger.info(
        f"--> {request.method} {request.url.path} | Client: {client_ip} | Dev: {dev_id} | User: {user_name}"
    )

    try:
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        response.headers["X-Request-ID"] = req_id
        logger.info(
            f"<-- {response.status_code} {request.method} {request.url.path} ({duration_ms:.2f}ms)"
        )
        return response
    except Exception as exc:
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        logger.error(
            f"❌ EXCEPTION {request.method} {request.url.path} ({duration_ms:.2f}ms): {exc}\n{traceback.format_exc()}"
        )
        raise exc


# ── EXCEPTION HANDLERS ────────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    formatted_errors = []
    for err in exc.errors():
        loc = " -> ".join([str(x) for x in err.get("loc", [])])
        formatted_errors.append({
            "field": loc,
            "message": err.get("msg", "Invalid value"),
            "type": err.get("type", "value_error"),
        })
    logger.warning(
        f"⚠️ HTTP 422 RequestValidationError on {request.method} {request.url.path}: {formatted_errors}"
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request payload schema validation failed.",
            "errors": formatted_errors,
        },
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
    formatted_errors = []
    for err in exc.errors():
        loc = " -> ".join([str(x) for x in err.get("loc", [])])
        formatted_errors.append({
            "field": loc,
            "message": err.get("msg", "Invalid value"),
            "type": err.get("type", "value_error"),
        })
    logger.warning(
        f"⚠️ HTTP 422 Internal ValidationError on {request.method} {request.url.path}: {formatted_errors}"
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Internal schema validation failed for payload data.",
            "errors": formatted_errors,
        },
    )


# ── MIDDLEWARE ────────────────────────────────────────────────────────────────

LOCAL_NETWORK_CORS_REGEX = r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|100\.\d{1,3}\.\d{1,3}\.\d{1,3}|.*\.ts\.net)(:\d+)?$"

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=LOCAL_NETWORK_CORS_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ── ROUTERS ───────────────────────────────────────────────────────────────────

app.include_router(health.router, prefix="/api/v1")
app.include_router(scan.router, prefix="/api/v1")
app.include_router(receipts.router, prefix="/api/v1")

app.include_router(user.router, prefix="/api/v1")
app.include_router(devices.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(help.router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"message": "Receipt Logging API", "docs": "/docs"}