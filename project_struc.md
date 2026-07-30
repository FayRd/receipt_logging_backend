# Receipt Logger Backend — Project Structure & Directory Layout

## Overview
The **Receipt Logger Backend** follows clean software architecture principles, separating concerns across API Routing, Business Logic Services, Data Models/Schemas, Repository Data Accessors, and Infrastructure Data Providers.

For full project scope and API specifications, see [PROJECT_SCOPE.md](file:///C:/mobile-development/receipt_logging_backend/PROJECT_SCOPE.md).

---

## Directory Layout

```
receipt_logging_backend/
├── .agents/                    # Custom agentic skills & engineering guidelines
│   └── skills/                 # FastAPI, Instructor, RAG AI Chat, and Supabase skills
├── .env                        # Environment variable configuration (git-ignored)
├── main.py                     # FastAPI application entrypoint & middleware lifecycle
├── README.md                   # Project overview, installation, setup & execution guide
├── run.ps1                     # PowerShell launcher script (runs on port 8085)
├── requirements.txt            # Python dependencies (FastAPI, google-genai, Supabase)
├── src/                        # Core Application Source Code
│   ├── API/                    # Presentation Layer (API Routers & Controllers)
│   │   └── v1/                 # API Version 1 Routers
│   │       ├── health.py       # Health check & status endpoints
│   │       ├── receipts.py     # Receipt CRUD & user ownership-validated routes
│   │       └── scan.py         # Image upload & Gemini 3.6 Flash Vision parsing endpoints
│   ├── Infrastructure/         # External System Integration Layer
│   │   └── database.py         # Supabase client dependency provider
│   ├── Models/                 # Data Layer (Pydantic Schemas & Repositories per Model)
│   │   ├── Receipts/           # Model-specific repository package
│   │   │   ├── __init__.py     # Package marker
│   │   │   └── receipt_repository.py # Receipt DB CRUD & query logic
│   │   ├── schemas.py          # Receipt, LineItem, ScanContext, ScanResponse, ReceiptRecord models (UUID strings)
│   │   └── supabase_connection.py # Supabase connection factory
│   ├── Services/               # Business Logic Layer
│   │   └── extraction_service.py # Gemini 3.6 Flash Vision AI extraction logic
│   └── config.py               # Pydantic BaseSettings environment loader
└── test/                       # Unit and integration tests (pytest suite)
```

---

## Layered Architecture Rationale

1. **API / Presentation Layer (`src/API/v1/`)**:
   - Handles incoming HTTP requests, file uploads, parameter validation, and status codes.
   - Thin routers that delegate business logic to services or repository classes using FastAPI dependency injection (`Depends`).

2. **Services / Business Logic Layer (`src/Services/`)**:
   - Houses AI prompt orchestration, Gemini 3.6 Flash Vision AI model calls, and structured JSON parsing.
   - Keeps business rules isolated from HTTP endpoints.

3. **Models & Repositories (`src/Models/`)**:
   - Defines strict Pydantic models (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord` with UUID string IDs, `ReceiptCreateRequest`, `ReceiptBatchCreateRequest`).
   - Grouped into model-specific repository directories (e.g. `src/Models/Receipts/receipt_repository.py`), encapsulating all direct Supabase queries, ownership filtering (`user_id`), soft deletes (`deleted_at`), and batch insertions.

4. **Infrastructure Layer (`src/Infrastructure/`)**:
   - Manages external service connections (Supabase database client, storage buckets, vector database).

5. **Config & Lifespan (`src/config.py`, `main.py`)**:
   - Centralized environment setting management using Pydantic `BaseSettings` and FastAPI `lifespan` context manager.

---

## Key Dependencies & Setup

- **`google-genai`**: Google Gemini API client (`gemini-3.6-flash`) for multimodal Vision AI extraction.
- **`fastapi`**: High-performance web framework.
- **`pydantic` & `pydantic-settings`**: Data parsing, configuration, and model serialization.
- **`supabase`**: Supabase client SDK for Postgres DB, Storage, and Vector search.
- **`python-multipart`**: Form-data file upload parsing for receipt images.
