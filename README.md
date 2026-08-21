# Receipt Logger Backend

FastAPI backend service providing multimodal receipt data extraction, asynchronous bulk processing with Redis job queues, Server-Sent Events (SSE) streaming, RAG conversational financial assistance, session-scoped identity resolution, sliding window rate limiting, and database security gateway for the Receipt Logger mobile application.

---

## Architecture Summary

The service acts as a secure backend gateway to Supabase PostgreSQL, Redis, and multimodal AI providers (Google GenAI SDK and OpenRouter). Public anonymous database access to Supabase PostgREST is completely revoked; all database operations are mediated through FastAPI using the `service_role` credential.

Receipt scanning is executed asynchronously through a Redis-backed batch worker supporting 1 to 10 concurrent image uploads with real-time SSE progress streaming. AI extraction and RAG chat support both Google Gemini and OpenRouter models with interchangeable provider dispatch, retry jitter, confidence scoring, and Pydantic validation.

---

## Key Technology Stack

- **Core Framework**: FastAPI (Python 3.10+) running on Uvicorn ASGI server.
- **AI Multi-Provider Layer**:
  - **Google GenAI SDK (`google-genai`)**: Direct integration with Gemini multimodal models (`gemini-3.6-flash`, `gemini-2.5-flash`) using structured response schemas.
  - **OpenRouter REST Integration (`httpx`)**: OpenAI-compatible chat completions endpoint (`https://openrouter.ai/api/v1`) supporting multimodal vision models (`google/gemini-2.5-flash`, `openai/gpt-4o`) and text chat models.
- **Asynchronous Batch Processing & SSE**:
  - Redis-backed job storage and state management (`redis-py`).
  - Worker pipeline processing up to 10 images per batch with 5-minute timeout tracking, exponential backoff with randomized jitter (up to 3 retries), fast-fail batch halt on first-job 429/500 errors, and per-job confidence score validation threshold (`>= 0.80`).
  - Real-time Server-Sent Events streaming via `sse-starlette` / `StreamingResponse`.
- **Authentication & Security**:
  - `src/Auth/` subsystem implementing constant-time token comparison (`secrets.compare_digest`), hardware device registration, PBKDF2/SHA-256 password hashing, and dual-mode (`user` / `guest`) session identity resolution.
  - Custom `SlidingWindowRateLimiter` keyed by verified caller identity or client IP.
- **Data Validation & Resiliency**:
  - Pydantic v2 schemas (`Receipt`, `LineItem`, `UserRecord`, `DeviceRecord`, `ConversationRecord`, etc.).
  - `mode="before"` field validators providing automatic fallback coercion for null/empty values returned by LLMs (`merchant_name` -> `"N/A"`, `total_amount` -> `0.0`, `currency` -> `"USD"`, `date` -> ISO UTC timestamp, `raw_text` -> `""`).
- **Database & Storage**:
  - Supabase PostgreSQL via `supabase-py` `AsyncClient`.
  - Idempotent SQL migrations with RLS policies, indexing, and trigger-enforced caps in `migration/`.
- **Observability & Logging**:
  - Optional OpenTelemetry / Logfire distributed tracing.
  - Module-level structured logging with togglable console and file log outputs.
- **Testing**:
  - Automated test suite (99 tests across 11 modules in `test/`) covering unit, integration, authentication, rate limiting, and failure modes.

---

## Features Breakdown

### 1. Bulk Asynchronous Receipt Scanning (`/api/v1/scan`)

- `POST /api/v1/scan/parse-many`: Accepts `multipart/form-data` with 1 to 10 image files (`.jpg`, `.jpeg`, `.png`, `.webp`, ceiling 10MB per file). Enqueues jobs in Redis, assigns batch UUID and per-file job UUIDs, starts background worker, and returns `202 Accepted`.
- `GET /api/v1/scan/parse-many/{batch_id}`: Returns batch metadata and current status of all jobs (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`). Data payload is populated only for `COMPLETED` jobs.
- `GET /api/v1/scan/parse-many/{batch_id}/stream`: Real-time SSE stream polling Redis at configured intervals. Emits `job_progress` on status updates, `batch_complete` when all jobs conclude, and `error` if the batch terminates prematurely.
- `POST /api/v1/scan/parse`: Legacy synchronous single-image parse endpoint (marked deprecated).
- **Extraction Engine Guarantees**:
  - Provider auto-selection (`gemini` or `openrouter`).
  - Exponential backoff with full jitter on 429/500/503 provider errors (up to 3 retries, delay formula: `min(1.0 * 2^attempt * uniform(0.5, 1.5), 10.0)`).
  - Fast-fail batch termination: If the first job in a batch fails with provider overload (429/500), all remaining jobs are marked `FAILED` immediately with a sanitized error message to prevent cascading load.
  - Confidence scoring validation: Documents with `confidence_score < 0.80` are marked `FAILED` per-job with validation notes, without halting the remainder of the batch.

### 2. Conversational RAG Financial Assistant (`/api/v1/chat`)

- `POST /api/v1/chat/query`: Main conversational query route supporting two operation modes:
  - **User Mode (`X-Request-Type: user`)**: Retrieves caller's recent logged receipts from Supabase, formats sanitized `<receipt_context>` blocks, and injects conversation history. Automatically initializes a new conversation in Supabase if `conversation_id` is omitted on first turn.
  - **Guest Mode (`X-Request-Type: guest`)**: Stateless execution using client-provided `recent_receipts` and `conversation_history` without persisting data to cloud storage.
- `POST /api/v1/chat/create`: Creates a new conversation in Supabase (enforces active cap of 10 conversations per identity via PostgreSQL trigger).
- `GET /api/v1/chat/list`: Lists non-deleted conversations owned by the caller identity, ordered newest first.
- `GET /api/v1/chat/history`: Retrieves chronological message turns for a given `conversation_id`.
- `PATCH /api/v1/chat/{conversation_id}`: Updates conversation title. Ownership verified.
- `DELETE /api/v1/chat/{conversation_id}`: Soft-deletes conversation and cascades soft-delete to associated messages.

### 3. User Authentication, Profile & Password Reset (`/api/v1/user`)

- `POST /api/v1/user/create`: Registers a new user with email, username, pre-encrypted password, optional mobile contact, and optional custom categories. Server hashes password with PBKDF2/SHA-256.
- `POST /api/v1/user/login`: Authenticates username or email with password in constant time.
- `GET /api/v1/user/me`: Retrieves profile of authenticated caller.
- `PATCH /api/v1/user/me`: Updates user email, mobile number, avatar path, or custom categories (maximum 8 categories).
- `DELETE /api/v1/user/me`: Soft-deletes user account and unlinks linked device sessions.
- `POST /api/v1/user/password-reset/initiate`: Generates single-use 6-digit OTP for email or mobile number with 10-minute expiry.
- `POST /api/v1/user/password-reset/verify-otp`: Validates OTP and returns a single-use `reset_token` with 15-minute expiry.
- `POST /api/v1/user/password-reset/new`: Sets new password using validated `reset_token`.

### 4. Device Registration & Hardware Linking (`/api/v1/devices`)

- `POST /api/v1/devices/register`: Idempotently registers hardware `device_id` and secret `device_token` (stored as SHA-256 hash). Fails closed (`401`) on token mismatch.
- `GET /api/v1/devices/me`: Retrieves device record for authenticated device token.
- `POST /api/v1/devices/link`: Links device to authenticated user account. Atomically migrates orphan guest receipts and conversations created by the device to the user account.
- `DELETE /api/v1/devices/me`: Soft-deletes device registration record.

### 5. Session-Scoped Receipt CRUD (`/api/v1/receipts`)

- `GET /api/v1/receipts/`: Retrieves non-deleted receipts owned by the resolved session identity.
- `POST /api/v1/receipts/`: Creates a single receipt record bound to session identity.
- `POST /api/v1/receipts/batch`: Creates up to 100 receipt records in a single database round-trip.
- `GET /api/v1/receipts/{receipt_id}`: Retrieves receipt by UUID, verifying session ownership.
- `PATCH /api/v1/receipts/{receipt_id}`: Updates receipt payload data. Ownership metadata is immutable.
- `DELETE /api/v1/receipts/{receipt_id}`: Soft-deletes receipt by UUID.

### 6. Rate Limiting & Health Diagnostics

- `GET /api/v1/health/`: Returns operational status, environment string, and version.
- **Rate Limit Buckets**:
  - Scanning (`POST /scan/*`): 5 requests/minute.
  - Chat (`POST /chat/*`): 10 requests/minute.
  - Auth (`POST /user/*`, `POST /devices/*`): 10 requests/minute.
  - CRUD (`/receipts/*`): 60 requests/minute.
  - Health check: 120 requests/minute.

---

## Header Authentication Contract

All protected endpoints enforce constant-time cryptographic verification. Four dependency schemes are used across routes:

### Scheme 1: Scoped Identity (`X-Request-Type`)
Used by `POST /api/v1/scan/parse-many`, `GET /api/v1/scan/parse-many/{batch_id}`, `GET /api/v1/scan/parse-many/{batch_id}/stream`, `POST /api/v1/scan/parse`, and `POST /api/v1/chat/query`.

| Header Name | Type | Allowed Values | Mode Rule |
|---|---|---|---|
| `X-Request-Type` | `string` | `"guest"` or `"user"` | Required on all calls. |
| `X-Device-Name` (or `X-Device-ID`) | `string` | Hardware UUID string | Required when `X-Request-Type: guest`. Must be omitted when `user`. |
| `X-Device-Token` | `string` | Secret device token string | Required when `X-Request-Type: guest`. Must be omitted when `user`. |
| `X-User-Name` (or `X-User-ID`) | `string` | Username or email string | Required when `X-Request-Type: user`. Must be omitted when `guest`. |
| `X-User-Token` | `string` | Pre-encrypted password string | Required when `X-Request-Type: user`. Must be omitted when `guest`. |

### Scheme 2: User Authenticated Identity
Used by `/api/v1/user/me`, `/api/v1/receipts/*`, and `/api/v1/chat/*` (excluding `/chat/query`).

| Header Name | Type | Required | Description |
|---|---|---|---|
| `X-User-Name` (or `X-User-ID`) | `string` | Yes | Username or email identifier. |
| `X-User-Token` | `string` | Yes | Pre-encrypted user password hash token. |

### Scheme 3: Device Identity
Used by `/api/v1/devices/me` and `/api/v1/devices/delete`.

| Header Name | Type | Required | Description |
|---|---|---|---|
| `X-Device-Name` (or `X-Device-ID`) | `string` | Yes | Hardware device identifier string. |
| `X-Device-Token` | `string` | Yes | Secret cryptographic device fingerprint token. |

### Scheme 4: Link Bridge Identity
Used by `POST /api/v1/devices/link`.

| Header Name | Type | Required | Description |
|---|---|---|---|
| `X-Device-Name` | `string` | Yes | Hardware device identifier to link. |
| `X-Device-Token` | `string` | Yes | Secret device token for device ownership verification. |
| `X-User-Name` | `string` | Optional | User identifier to link to (omitted for unlinking). |
| `X-User-Token` | `string` | Optional | User password token for user credential verification. |

---

## Developer Guide for `.env` Configuration

Create a `.env` file in the backend project root by copying `.env.example`:

```bash
cp .env.example .env
```

### Environment Variables Reference

| Variable | Type | Default | Description |
|---|---|---|---|
| `SUPABASE_URL` | `string` | *Required* | Supabase project URL (`https://<project-id>.supabase.co`). |
| `SUPABASE_KEY` | `string` | *Required* | Supabase **`service_role`** secret key. Public `anon` keys will fail. |
| `REDIS_CONNECTION_STRING` | `string` | `redis://localhost:6379` | Redis connection URL for batch job queuing and SSE state. |
| `AI_PROVIDER` | `string` | `gemini` | Active AI provider: `gemini` or `openrouter`. |
| `GEMINI_API_KEY` | `string` | `""` | Google AI Studio API key (used when `AI_PROVIDER=gemini`). |
| `GEMINI_VISION_MODEL` | `string` | `gemini-3.6-flash` | Gemini model for receipt vision extraction. |
| `GEMINI_CHAT_MODEL` | `string` | `gemini-3.6-flash` | Gemini model for RAG financial assistant. |
| `OPENROUTER_API_KEY` | `string` | `""` | OpenRouter API key (used when `AI_PROVIDER=openrouter`). |
| `OPENROUTER_BASE_URL` | `string` | `https://openrouter.ai/api/v1` | OpenRouter base API endpoint. |
| `OPENROUTER_VISION_MODEL` | `string` | `google/gemini-2.5-flash` | Multimodal model on OpenRouter for receipt vision extraction. |
| `OPENROUTER_CHAT_MODEL` | `string` | `google/gemini-2.5-flash` | Model on OpenRouter for RAG financial assistant. |
| `CONFIDENCE_THRESHOLD` | `float` | `0.8` | Minimum OCR document validation score required to mark job `COMPLETED`. |
| `MAX_IMAGE_SIZE_BYTES` | `int` | `10485760` | Maximum single image upload limit (10MB). |
| `RAG_RECENT_RECEIPTS_LIMIT`| `int` | `100` | Maximum recent receipts injected into RAG chat context. |
| `RAG_HISTORY_MESSAGES_LIMIT`| `int` | `50` | Maximum message turns retained in conversation window. |
| `MAX_CONVERSATIONS_PER_IDENTITY` | `int` | `10` | Hard cap on active conversations per user/device identity. |
| `SSE_POLL_INTERVAL_SECONDS` | `float` | `1.0` | Redis poll cadence for active SSE streams. |
| `SSE_BATCH_TIMEOUT_SECONDS` | `int` | `300` | Maximum wait timeout for batch processing (5 minutes). |
| `REDIS_JOB_TTL_SECONDS` | `int` | `600` | Expiration TTL for Redis batch keys (10 minutes). |
| `RATE_LIMIT_ENABLED` | `bool` | `true` | Enables sliding window rate limiting. |
| `ENABLE_FILE_LOGGING` | `bool` | `true` | Logs structured events to `LOG_FILE_PATH`. |
| `LOG_FILE_PATH` | `string` | `app.log` | Output path for file logging. |
| `LOG_LEVEL` | `string` | `DEBUG` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `ENABLE_CONSOLE_LOGGING` | `bool` | `true` | Emits colored logs to stdout/stderr. |
| `ENVIRONMENT` | `string` | `development` | Deployment environment name (`development`, `production`). |
| `ALLOWED_ORIGINS` | `list[str]` | `["http://localhost", ...]` | CORS allowed origin domains. |

### Configuration Templates

#### Option A: Google GenAI (Gemini) Setup (Default)
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-service-role-key
REDIS_CONNECTION_STRING=redis://localhost:6379

AI_PROVIDER=gemini
GEMINI_API_KEY=AIzaSy...
GEMINI_VISION_MODEL=gemini-3.6-flash
GEMINI_CHAT_MODEL=gemini-3.6-flash

ENVIRONMENT=development
ALLOWED_ORIGINS=["http://localhost","http://localhost:3000","http://localhost:8085"]
```

#### Option B: OpenRouter Setup
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-service-role-key
REDIS_CONNECTION_STRING=redis://localhost:6379

AI_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_VISION_MODEL=google/gemini-2.5-flash
OPENROUTER_CHAT_MODEL=google/gemini-2.5-flash

ENVIRONMENT=development
ALLOWED_ORIGINS=["http://localhost","http://localhost:3000","http://localhost:8085"]
```

---

## Database Migrations & Supabase Setup

Execute migration scripts in sequence in the Supabase SQL Editor:

1. [`migration/00_teardown_all.sql`](file:///C:/mobile-development/receipt_logging_backend/migration/00_teardown_all.sql): Rollback and reset all tables, types, and functions (destructive).
2. [`migration/01_schema_tables.sql`](file:///C:/mobile-development/receipt_logging_backend/migration/01_schema_tables.sql): Core tables (`users`, `devices`, `receipts`, `conversations`, `chat_messages`), foreign keys, and soft-delete columns.
3. [`migration/02_indexes_triggers.sql`](file:///C:/mobile-development/receipt_logging_backend/migration/02_indexes_triggers.sql): B-tree indexes, unique constraints, and trigger-enforced 10-conversation cap.
4. [`migration/03_rls_policies.sql`](file:///C:/mobile-development/receipt_logging_backend/migration/03_rls_policies.sql): Row Level Security configurations.
5. [`migration/04_grants_permissions.sql`](file:///C:/mobile-development/receipt_logging_backend/migration/04_grants_permissions.sql): Revokes public `anon` access and grants permissions exclusively to `service_role`.

---

## Running the Backend

### Local Environment Setup

```bash
# 1. Create and activate virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start Uvicorn server
uvicorn main:app --host 0.0.0.0 --port 8085 --reload
```

### Docker Deployment

```bash
# Build and start container in background
docker compose up -d --build

# View real-time container logs
docker compose logs -f

# Stop container
docker compose down
```

---

## Test Suite Execution

Run the complete 99-test automated test suite:

```bash
# Run all tests
pytest -v test/

# Run targeted test modules
pytest -v test/test_scan.py test/test_chat.py
```

---

## Interactive API Documentation

When the service is running, interactive API specs are available at:
- **Swagger UI**: [http://localhost:8085/docs](http://localhost:8085/docs)
- **ReDoc**: [http://localhost:8085/redoc](http://localhost:8085/redoc)
- **OpenAPI JSON**: [http://localhost:8085/openapi.json](http://localhost:8085/openapi.json)
