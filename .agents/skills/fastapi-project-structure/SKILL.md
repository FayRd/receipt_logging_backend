---
name: fastapi-project-structure
description: Structure a FastAPI project with routers, dependency injection, Pydantic BaseSettings config, lifespan events, and CORS. Use when bootstrapping or refactoring the FastAPI backend.
metadata:
  version: "1.0.0"
---
# FastAPI Project Structure

## Contents
- [Core Concepts](#core-concepts)
- [Workflow](#workflow)
- [Code Examples](#code-examples)

## Core Concepts
This skill covers the standard project structure for a scalable FastAPI application.
- **BaseSettings**: Uses `pydantic-settings` to manage environment variables safely and with type checking.
- **Application Factory**: Uses a `create_app` pattern and an `async contextlib.asynccontextmanager` for `lifespan` events to handle startup and shutdown logic (e.g., DB connections).
- **CORS Middleware**: Allows configuring cross-origin resource sharing from environment settings.
- **Routers**: Modularizes endpoints using `APIRouter`.
- **Dependency Injection**: Promotes decoupled, testable code using FastAPI's `Depends()`.
- **Directory Structure**: Employs an onion-like architecture (API, Models, Services, Infrastructure) to separate concerns.

The exact directory structure is:
```text
src/
├── API/
│   └── v1/
│       └── routes/          # APIRouter definitions
├── Models/                  # Pydantic schemas and domain models
├── Services/                # Business logic (e.g., Receipt extraction logic)
└── Infrastructure/          # DB connections, external clients (Supabase, Gemini)
```

## Workflow
### Task Progress
- [ ] Initialize `src/` directory with the required subdirectories (`API`, `Models`, `Services`, `Infrastructure`).
- [ ] Create a `config.py` in `Infrastructure` using `BaseSettings` for env vars.
- [ ] Setup `main.py` with `create_app` factory, `lifespan` manager, and CORS middleware.
- [ ] Define reusable dependencies (e.g., database clients, current user) in `Infrastructure/dependencies.py`.
- [ ] Create domain schemas in `Models/`.
- [ ] Write business logic in `Services/` using injected dependencies.
- [ ] Define API routes in `API/v1/routes/` utilizing `APIRouter`.
- [ ] Wire up routers in `main.py` using `app.include_router()`.

## Code Examples

### 1. Configuration (src/Infrastructure/config.py)
```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    environment: str = "development"
    SUPABASE_URL: str
    SUPABASE_KEY: str
    GEMINI_API_KEY: str
    CORS_ORIGINS: list[str] = ["*"]

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

### 2. Main Application with Lifespan & CORS (src/main.py)
```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.Infrastructure.config import get_settings
from src.API.v1.routes import receipts

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB pools, load ML models, etc.
    print("Application startup")
    yield
    # Shutdown: Close connections
    print("Application shutdown")

def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Receipt Logging API",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(receipts.router, prefix="/api/v1")
    return app

app = create_app()
```

### 3. Dependencies & Error Handling (src/Infrastructure/dependencies.py)
```python
from fastapi import Depends, HTTPException, status
from src.Infrastructure.config import get_settings, Settings
from supabase import create_client, Client

def get_supabase_client(settings: Settings = Depends(get_settings)) -> Client:
    try:
        supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        return supabase
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database connection error",
        )
```

### 4. Router (src/API/v1/routes/receipts.py)
```python
from fastapi import APIRouter, Depends, HTTPException
from src.Models.receipt import Receipt
from src.Services.receipt_service import process_receipt
from src.Infrastructure.dependencies import get_supabase_client
from supabase import Client

router = APIRouter(prefix="/receipts", tags=["Receipts"])

@router.post("/", response_model=Receipt)
async def upload_receipt(
    file_content: bytes,
    db: Client = Depends(get_supabase_client)
):
    try:
        return await process_receipt(file_content, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```
