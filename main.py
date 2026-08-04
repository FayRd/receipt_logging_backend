import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import redis.asyncio as aioredis
from google import genai
from src.config import get_settings
from src.API.v1 import health, scan, receipts, user, devices, chat, bulk

redis_client: aioredis.Redis | None = None
genai_client: genai.Client | None = None


# ── PHASE 2: LIFESPAN INTEGRATION ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, genai_client
    settings = get_settings()
    print(f"Starting Receipt API in {settings.environment} mode")

    # Initialize Async Redis client
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/1")
    redis_client = aioredis.from_url(redis_url, decode_responses=True)

    # Initialize Google Gen AI async client
    genai_client = genai.Client(api_key=settings.gemini_api_key)

    # Pass clients to bulk router module
    bulk.init_bulk_clients(redis_client, genai_client)

    yield

    print("Shutting down Receipt API")
    if redis_client:
        await redis_client.aclose()


app = FastAPI(
    title="Receipt Scanner API",
    description="AI-powered receipt scanning and tracking backend",
    version="1.0.0",
    lifespan=lifespan,
)

# ── EXCEPTION HANDLERS ────────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    """Intercept FastAPI request body/parameter validation errors and return HTTP 422."""
    formatted_errors = []
    for err in exc.errors():
        loc = " -> ".join([str(x) for x in err.get("loc", [])])
        formatted_errors.append({
            "field": loc,
            "message": err.get("msg", "Invalid value"),
            "type": err.get("type", "value_error"),
        })
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request payload schema validation failed.",
            "errors": formatted_errors,
        },
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
    """Intercept internal Pydantic model validation errors and return HTTP 422."""
    formatted_errors = []
    for err in exc.errors():
        loc = " -> ".join([str(x) for x in err.get("loc", [])])
        formatted_errors.append({
            "field": loc,
            "message": err.get("msg", "Invalid value"),
            "type": err.get("type", "value_error"),
        })
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Internal schema validation failed for payload data.",
            "errors": formatted_errors,
        },
    )


# ── MIDDLEWARE ────────────────────────────────────────────────────────────────

# Allow localhost, local network IPs (192.168.x.x, 10.x.x.x), and Tailscale networks (100.x.x.x, *.ts.net)
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
app.include_router(bulk.router, prefix="/api/v1")
app.include_router(user.router, prefix="/api/v1")
app.include_router(devices.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"message": "Receipt Logging API", "docs": "/docs"}