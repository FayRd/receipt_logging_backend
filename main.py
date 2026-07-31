from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from src.config import get_settings
from src.API.v1 import health, scan, receipts, user, devices


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings = get_settings()
    print(f"Starting Receipt API in {settings.environment} mode")
    yield
    # Shutdown
    print("Shutting down Receipt API")


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

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── ROUTERS ───────────────────────────────────────────────────────────────────

app.include_router(health.router, prefix="/api/v1")
app.include_router(scan.router, prefix="/api/v1")
app.include_router(receipts.router, prefix="/api/v1")
app.include_router(user.router, prefix="/api/v1")
app.include_router(devices.router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"message": "Receipt Logging API", "docs": "/docs"}