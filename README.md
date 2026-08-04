# Receipt Logger Backend

An AI-powered FastAPI backend service for receipt scanning, structured data extraction using **Google Gemini 3.6 Flash Vision AI**, RAG AI Chat assistant, session-scoped identity security, Sliding Window Counter rate limiting, Backend Service Gateway database security, soft-delete data lifecycle management, and cloud database synchronization for the **Receipt Logger** mobile application.

---

## 🚀 Application Summary

The **Receipt Logger Backend** provides high-speed, intelligent multimodal receipt parsing, conversational AI financial assistance, and secure data management for the privacy-first mobile client. It accepts receipt image uploads, processes them directly with Gemini 3.6 Flash Vision AI using strict structured Pydantic schemas, and returns validated JSON containing merchant info, line items, totals, dates, and categories in ~1.5 seconds. It also features a personalized RAG AI Chat assistant powered by Gemini 3.6 Flash, session-scoped CRUD & soft-delete endpoints, cryptographic device fingerprint verification (`X-Device-Token`), identity-keyed rate limiting, and a locked-down Backend Service Gateway architecture in Supabase.

### Key Technology Stack
- **Framework**: FastAPI (Python 3.10+) with Uvicorn ASGI server
- **Auth & Rate Limiting**: `src/Auth/` package (`Identity` model, `X-Device-Token` verification via constant-time `secrets.compare_digest`, `SlidingWindowRateLimiter` engine)
- **Backend Service Gateway Architecture**: Supabase public `anon` access is **100% revoked/blocked**; FastAPI connects exclusively via the `service_role` key
- **Database Migrations & RLS**: Idempotent SQL scripts in `migration/` (`00_teardown_all.sql`, `01_schema_tables.sql`, `02_indexes_triggers.sql`, `03_rls_policies.sql`, `04_grants_permissions.sql`)
- **AI Extraction & Chat**: `google-genai` SDK (`gemini-3.6-flash` Multimodal Vision & RAG) with XML prompt boundary isolation and `>=0.8` document type confidence validation
- **Containerization**: Docker & Docker Compose (`docker-compose.yml`, `Dockerfile`, `.dockerignore`)
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord`, `UserRecord`, `DeviceRecord`, `ConversationRecord`, `ChatMessageRecord`)
- **Cloud Database & Storage**: Supabase (`supabase-py` `AsyncClient`) for Postgres DB, Storage, and Vector search
- **Architecture**: Layered Architecture with per-model repository pattern (`Receipts`, `Users`, `Devices`, `Conversations`)
- **Testing**: Automated Pytest suite (54 tests in `test/`) with `test-engineer` and `security-advisor` subagents
- **Configuration**: Pydantic `BaseSettings` & `python-dotenv`

---
V
## ✨ Features Breakdown

### Implemented Features
- **Identity-Keyed Rate Limiting & DoS Protection** (`src/Auth/rate_limiter.py`):
  - Enforces high-precision **Sliding Window Counter** rate limits across all routes.
  - Keyed by client identity (`X-Device-ID`, falling back to client IP) to prevent shared NAT/Wi-Fi choking.
  - Returns standard `HTTP 429 Too Many Requests` with `Retry-After`, `X-RateLimit-Limit`, and `X-RateLimit-Remaining` HTTP response headers.
  - Configurable limits: Scan (5/min), Chat (10/min), Auth/Register (10/min), CRUD (60/min), Health (120/min).
- **Backend Service Gateway & Supabase Security**:
  - All public `anon` access to Supabase PostgREST is completely revoked (`REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;`).
  - Database access is restricted strictly to FastAPI via the secret `service_role` key (`SUPABASE_KEY`).
  - Idempotent migration scripts provided in `migration/` with teardown/rollback capabilities (`00_teardown_all.sql`).
- **Device Management & Soft-Delete** (`/api/v1/devices`):
  - `POST /api/v1/devices/register`: Idempotent registration of hardware `device_id` and secret `device_token`. Fails closed (`401`) on token mismatch.
  - `GET /api/v1/devices/me`: Retrieves current device registration record.
  - `POST /api/v1/devices/link`: Links device to user account and atomically migrates orphan guest receipts & conversations.
  - `DELETE /api/v1/devices/me`: Soft-deletes calling device registration record (`deleted_at = now()`).
- **User Authentication & Soft-Delete** (`/api/v1/user`):
  - `POST /api/v1/user/create`: Registers a new user account (PBKDF2/SHA-256 server-side password hash).
  - `POST /api/v1/user/login`: Authenticates credentials in constant time to prevent timing attacks.
  - `GET /api/v1/user/me`: Retrieves authenticated user profile (session-scoped).
  - `DELETE /api/v1/user/me`: Soft-deletes user account and automatically unlinks active device sessions to prevent zombie logins.
- **Multimodal AI Receipt Extraction & Document Validation** (`POST /api/v1/scan/parse`):
  - Accepts multipart/form-data image uploads (`.png`, `.jpg`, `.jpeg`, `.webp`).
  - Requires device identity headers (`X-Device-ID`, `X-Device-Token`), 10MB file ceiling, and `confidence_score >= 0.8`.
  - Directly feeds image bytes to Gemini 3.6 Flash with strict JSON schema enforcement.
  - Extracts merchant name, line items, subtotal, tax, total amount, currency, ISO 8601 date, raw OCR text, and category inference.
- **Session-Scoped Receipt CRUD & Soft-Delete** (`/api/v1/receipts`):
  - `GET /api/v1/receipts/`: Retrieves all non-deleted receipts owned by session identity.
  - `GET /api/v1/receipts/{receipt_id}`: Retrieves a single receipt by UUID, enforcing session ownership.
  - `POST /api/v1/receipts/`: Creates a single receipt record bound to session identity.
  - `POST /api/v1/receipts/batch`: Batch-creates up to 100 receipts bound to session identity.
  - `DELETE /api/v1/receipts/{receipt_id}`: Soft-deletes receipt by UUID (session ownership enforced).
- **Personalized RAG AI Chat Assistant & Soft-Delete** (`/api/v1/chat`):
  - `POST /api/v1/chat/create`: Creates a new AI conversation (enforces max 10 cap per identity via PostgreSQL trigger).
  - `GET /api/v1/chat/list`: Lists conversations owned by session identity, newest first.
  - `GET /api/v1/chat/history`: Fetches chunked, paginated message history.
  - `POST /api/v1/chat/query`: Sends query to Gemini 3.6 Flash using identity-scoped receipt history for RAG context (protected with XML prompt boundary isolation).
  - `DELETE /api/v1/chat/{conversation_id}`: Soft-deletes a conversation by UUID (session ownership enforced).
- **Explicit HTTP 422 Error Handling**: Custom exception handlers in `main.py` intercept any request payload schema mismatches or invalid data types and return clean `HTTP 422 Unprocessable Entity` responses.
- **Health Check & Diagnostics** (`GET /api/v1/health/`): Returns API operational status and environment setting.

---

## 🛠️ Header Authentication Contract

All protected endpoints require the following HTTP headers:

| Header Name | Type | Required? | Description |
|---|---|---|---|
| `X-Device-ID` | `string` | **Yes** | Unique hardware device identifier string (e.g. `MB-12345`). |
| `X-Device-Token` | `string` | **Yes** | Secret cryptographic device fingerprint token generated on first app boot. |
| `X-User-ID` | `string` | Optional | User UUID string (verified against DB ground truth when signed in). |

---

## 🏃 Running the Application & Test Suite

### Running the Application

```powershell
.\run.ps1
```

### Running with Docker Compose (Standalone Container)

```bash
# Build and start container in background
docker compose up -d --build

# View container logs
docker compose logs -f

# Stop container
docker compose down
```

Or manually:
```bash
uvicorn main:app --host 0.0.0.0 --port 8085 --reload
```

### Running the Pytest Test Suite

```powershell
.venv\Scripts\pytest.exe -v test/
```

---

## 🌐 API Documentation & Interactive Docs

Once running, access the interactive API documentation at:
- **Swagger UI**: [http://localhost:8085/docs](http://localhost:8085/docs)
- **ReDoc**: [http://localhost:8085/redoc](http://localhost:8085/redoc)
- **OpenAPI Schema**: [http://localhost:8085/openapi.json](http://localhost:8085/openapi.json)
