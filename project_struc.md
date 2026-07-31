# Receipt Logger Backend — Project Structure & Directory Layout

## Overview
The **Receipt Logger Backend** follows clean software architecture principles, separating concerns across Presentation (API Routers), Authentication & Session Resolution, Business Logic Services, Data Models/Schemas, Repository Data Accessors, and Infrastructure Data Providers.

For full project scope and API specifications, see [PROJECT_SCOPE.md](file:///C:/mobile-development/receipt_logging_backend/PROJECT_SCOPE.md).

---

## Directory Layout

```
receipt_logging_backend/
├── .agents/                    # Custom agentic skills & engineering guidelines
│   └── skills/                 # FastAPI, Instructor, RAG AI Chat, and Supabase skills
├── .env                        # Environment variable configuration (git-ignored)
├── main.py                     # FastAPI application entrypoint, middleware & exception handlers
├── README.md                   # Project overview, setup, curl examples & execution guide
├── run.ps1                     # PowerShell launcher script (runs on port 8085)
├── requirements.txt            # Python dependencies (FastAPI, google-genai, Supabase, pytest)
├── src/                        # Core Application Source Code
│   ├── API/                    # Presentation Layer (API Routers & Controllers)
│   │   └── v1/                 # API Version 1 Routers
│   │       ├── chat.py         # AI Chat creation, listing, history & RAG query endpoints
│   │       ├── devices.py      # Device registration, link & session-scoped device routes
│   │       ├── health.py       # Health check & status endpoints
│   │       ├── receipts.py     # Session-scoped Receipt CRUD routes
│   │       ├── scan.py         # Image upload & Gemini 3.6 Flash Vision parsing
│   │       └── user.py         # User registration, login & session-scoped /me route
│   ├── Auth/                   # Authentication & Session Identity Resolution
│   │   ├── __init__.py         # Package marker
│   │   └── identity.py         # Identity model, get_current_identity, require_user_identity
│   ├── Infrastructure/         # External System Integration Layer
│   │   └── database.py         # Supabase AsyncClient dependency provider
│   ├── Models/                 # Data Layer (Pydantic Schemas & Repositories per Model)
│   │   ├── Conversations/      # Model-specific repository package
│   │   │   ├── __init__.py     # Package marker
│   │   │   └── conversation_repository.py # Conversation DB CRUD & identity query logic
│   │   ├── Devices/            # Model-specific repository package
│   │   │   ├── __init__.py     # Package marker
│   │   │   └── device_repository.py # Device DB CRUD, token validation & guest data migration
│   │   ├── Receipts/           # Model-specific repository package
│   │   │   ├── __init__.py     # Package marker
│   │   │   └── receipt_repository.py # Receipt DB CRUD & identity-scoped query logic
│   │   ├── Users/              # Model-specific repository package
│   │   │   ├── __init__.py     # Package marker
│   │   │   └── user_repository.py # User DB CRUD & PBKDF2 password hashing
│   │   └── schemas.py          # Receipt, LineItem, User, Device, Chat Pydantic schemas
│   ├── Services/               # Business Logic Layer
│   │   ├── chat_service.py     # Gemini 3.6 Flash RAG AI Chat service
│   │   └── extraction_service.py # Gemini 3.6 Flash Vision AI extraction service
│   └── config.py               # Pydantic BaseSettings environment loader
└── test/                       # Unit and integration test suite (37 tests)
    ├── conftest.py             # Shared pytest fixtures (TestClient, mock device, mock user)
    ├── test_chat.py            # AI Chat endpoint tests (create, list, history, query, cap)
    ├── test_devices.py         # Device registration, link & guest data migration tests
    ├── test_errors.py          # HTTP 401, 404, 422 error handling tests
    ├── test_health.py          # Health check status tests
    ├── test_receipts.py        # Receipt CRUD & batch insertion tests
    └── test_user.py            # User creation, login & profile tests
```

---

## Layered Architecture Rationale

1. **API / Presentation Layer (`src/API/v1/`)**:
   - Handles incoming HTTP requests, file uploads, parameter validation, and HTTP status codes.
   - Thin routers that delegate identity resolution to `src/Auth/` and data persistence to repositories using FastAPI dependency injection (`Depends`).

2. **Authentication & Identity Resolution (`src/Auth/`)**:
   - Parses `X-Device-ID`, `X-Device-Token`, and `X-User-ID` headers.
   - Performs constant-time cryptographic verification (`secrets.compare_digest`) against stored `device_token`.
   - Derives `user_id` strictly from database ground truth to prevent identity spoofing.

3. **Services / Business Logic Layer (`src/Services/`)**:
   - Houses AI prompt orchestration, Gemini 3.6 Flash Vision AI model calls, and RAG conversational search over logged receipts.

4. **Models & Repositories (`src/Models/`)**:
   - Defines strict Pydantic models (`Receipt`, `LineItem`, `UserRecord`, `DeviceRecord`, `ConversationRecord`, `ChatMessageRecord`).
   - Grouped into model-specific repository directories (`Receipts`, `Users`, `Devices`, `Conversations`), encapsulating all direct Supabase `AsyncClient` queries, identity filtering, soft deletes (`deleted_at`), and guest data migration logic.

5. **Infrastructure Layer (`src/Infrastructure/`)**:
   - Manages external service connections using `acreate_client` for non-blocking async Supabase operations.

6. **Config & Lifespan (`src/config.py`, `main.py`)**:
   - Centralized environment setting management using Pydantic `BaseSettings` and FastAPI `lifespan` context manager.
   - Global exception handlers catching `RequestValidationError` and `ValidationError` to guarantee explicit HTTP 422 responses.
