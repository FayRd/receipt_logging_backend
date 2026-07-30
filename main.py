from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.config import get_settings
from src.API.v1 import health, scan, receipts

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

# CORS
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(health.router, prefix="/api/v1")
app.include_router(scan.router, prefix="/api/v1")
app.include_router(receipts.router, prefix="/api/v1")

@app.get("/")
async def root():
    return {"message": "Receipt Logging API", "docs": "/docs"}