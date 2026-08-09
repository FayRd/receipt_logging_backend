# Receipt Logger Backend

An AI-powered FastAPI backend service for receipt scanning, structured data extraction using **Google Gemini 3.6 Flash Vision AI**, RAG AI Chat assistant, session-scoped identity security, Sliding Window Counter rate limiting, Backend Service Gateway database security, soft-delete data lifecycle management, and cloud database synchronization for the **Receipt Logger** mobile application.

---

## Application Summary

The **Receipt Logger Backend** provides high-speed, intelligent multimodal receipt parsing, conversational AI financial assistance, and secure data management for the privacy-first mobile client. It accepts receipt image uploads, processes them directly with Gemini 3.6 Flash Vision AI using strict structured Pydantic schemas, and returns validated JSON containing merchant info, line items, totals, dates, and categories in ~1.5 seconds. It also features a personalized RAG AI Chat assistant powered by Gemini 3.6 Flash, session-scoped CRUD & soft-delete endpoints, cryptographic device fingerprint verification (`X-Device-Token`), identity-keyed rate limiting, and a locked-down Backend Service Gateway architecture in Supabase.

### Key Technology Stack
- **Framework**: FastAPI (Python 3.10+) with Uvicorn ASGI server
- **Auth & Rate Limiting**: `src/Auth/` package (`Identity` model, `X-Device-Token` verification via constant-time `secrets.compare_digest`, `SlidingWindowRateLimiter` engine)
- **Backend Service Gateway Architecture**: Supabase public `anon` access is **100% revoked/blocked**; FastAPI connects exclusively via the `service_role` key
- **Database Migrations & RLS**: Idempotent SQL scripts in [`migration/`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration) (`00_teardown_all.sql`, `01_schema_tables.sql`, `02_indexes_triggers.sql`, `03_rls_policies.sql`, `04_grants_permissions.sql`)
- **AI Extraction & Chat**: `google-genai` SDK (`gemini-3.6-flash` Multimodal Vision & RAG) with XML prompt boundary isolation and `>=0.8` document type confidence validation
- **Containerization**: Docker & Docker Compose (`docker-compose.yml`, `Dockerfile`, `.dockerignore`)
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord`, `UserRecord`, `DeviceRecord`, `ConversationRecord`, `ChatMessageRecord`)
- **Cloud Database & Storage**: Supabase (`supabase-py` `AsyncClient`) for Postgres DB, Storage, and Vector search
- **Architecture**: Layered Architecture with per-model repository pattern (`Receipts`, `Users`, `Devices`, `Conversations`)
- **Testing**: Automated Pytest suite (54 tests in [`test/`](file:///home/ninonakano/Desktop/receipt_logging_backend/test))
- **Configuration**: Pydantic `BaseSettings` & `python-dotenv`

---

## Features Breakdown

### Implemented Features
- **Identity-Keyed Rate Limiting & DoS Protection** ([`src/Auth/rate_limiter.py`](file:///home/ninonakano/Desktop/receipt_logging_backend/src/Auth/rate_limiter.py)):
  - Enforces high-precision **Sliding Window Counter** rate limits across all routes.
  - Keyed by client identity (`X-Device-ID`, falling back to client IP) to prevent shared NAT/Wi-Fi choking.
  - Returns standard `HTTP 429 Too Many Requests` with `Retry-After`, `X-RateLimit-Limit`, and `X-RateLimit-Remaining` HTTP response headers.
  - Configurable limits: Scan (5/min), Chat (10/min), Auth/Register (10/min), CRUD (60/min), Health (120/min).
- **Backend Service Gateway & Supabase Security**:
  - All public `anon` access to Supabase PostgREST is completely revoked (`REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;`).
  - Database access is restricted strictly to FastAPI via the secret `service_role` key (`SUPABASE_KEY`).
  - Idempotent migration scripts provided in [`migration/`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration) with teardown/rollback capabilities (`00_teardown_all.sql`).
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
- **Explicit HTTP 422 Error Handling**: Custom exception handlers in [`main.py`](file:///home/ninonakano/Desktop/receipt_logging_backend/main.py) intercept any request payload schema mismatches or invalid data types and return clean `HTTP 422 Unprocessable Entity` responses.
- **Health Check & Diagnostics** (`GET /api/v1/health/`): Returns API operational status and environment setting.

---

## Header Authentication Contract

All protected endpoints require the following HTTP headers:

| Header Name | Type | Required? | Description |
|---|---|---|---|
| `X-Device-ID` | `string` | **Yes** | Unique hardware device identifier string (e.g. `MB-12345`). |
| `X-Device-Token` | `string` | **Yes** | Secret cryptographic device fingerprint token generated on first app boot. |
| `X-User-ID` | `string` | Optional | User UUID string (verified against DB ground truth when signed in). |

---

## Step-by-Step Guide to Running the Backend

Follow these steps to configure, set up, and run the backend locally or in Docker.

### Step 1: System Prerequisites
Ensure the following tools are installed on your machine:
- **Python 3.10** or higher
- **Redis Server** (running locally on port `6379` or accessible via network URL)
- **Supabase Account & Project** (PostgreSQL instance with `service_role` key access)
- **Google Gemini API Key** (for Vision AI parsing and RAG chat)

### Step 2: Environment Configuration
Create a `.env` file in the root directory by copying [`.env.example`](file:///home/ninonakano/Desktop/receipt_logging_backend/.env.example):

```bash
cp .env.example .env
```

Populate `.env` with your actual service credentials:

```env
SUPABASE_URL=https://your-supabase-project.supabase.co
SUPABASE_KEY=your-supabase-service-role-key
REDIS_CONNECTION_STRING=redis://localhost:6379
GEMINI_API_KEY=your-google-gemini-api-key
ENVIRONMENT=development
ALLOWED_ORIGINS=["*"]
```

> Note: `SUPABASE_KEY` must be the **service_role** secret key because public `anon` access is revoked by migration policy scripts.

### Step 3: Supabase Database Migration
Execute the SQL migration scripts in [`migration/`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration) in sequential order in the Supabase SQL Editor:
1. [`migration/00_teardown_all.sql`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration/00_teardown_all.sql) *(Optional: use only when resetting database schema)*
2. [`migration/01_schema_tables.sql`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration/01_schema_tables.sql) *(Creates core tables, foreign keys, and soft-delete columns)*
3. [`migration/02_indexes_triggers.sql`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration/02_indexes_triggers.sql) *(Creates performance indexes and conversation cap triggers)*
4. [`migration/03_rls_policies.sql`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration/03_rls_policies.sql) *(Configures Row Level Security)*
5. [`migration/04_grants_permissions.sql`](file:///home/ninonakano/Desktop/receipt_logging_backend/migration/04_grants_permissions.sql) *(Revokes public `anon` access and grants permissions to `service_role`)*

### Step 4: Create Virtual Environment & Install Dependencies
Initialize Python virtual environment and install required packages:

```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment (Linux/macOS)
source .venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

### Step 5: Start the Backend Server

#### Option A: Using the Linux Bash Run Script (Recommended)
Make [`run.sh`](file:///home/ninonakano/Desktop/receipt_logging_backend/run.sh) executable and run it:

```bash
chmod +x run.sh
./run.sh
```

#### Option B: Direct Uvicorn CLI Execution
Run Uvicorn directly from the virtual environment:

```bash
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8085 --reload
```

#### Option C: Docker Compose (Standalone Container)
Build and spin up the backend container:

```bash
# Build and start container in background
docker compose up -d --build

# View real-time container logs
docker compose logs -f

# Stop container
docker compose down
```

---

## Running the Pytest Test Suite

Execute the automated unit and integration test suite (54 tests):

```bash
.venv/bin/pytest -v test/
```

---

## API Documentation & Interactive Docs

Once the backend service is running, access interactive API documentation at:
- **Swagger UI**: [http://localhost:8085/docs](http://localhost:8085/docs)
- **ReDoc**: [http://localhost:8085/redoc](http://localhost:8085/redoc)
- **OpenAPI Schema**: [http://localhost:8085/openapi.json](http://localhost:8085/openapi.json)
