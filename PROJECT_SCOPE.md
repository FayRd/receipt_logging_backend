# Receipt Logger Backend — Project Scope & Architecture Blueprint

## Overview
The **Receipt Logger Backend** is an AI-powered REST API built with **FastAPI**, **Gemini 3.6 Flash Vision AI**, and **Supabase (Postgres, Storage & pgvector)**. It serves as the cloud intelligent processing engine for the privacy-first Flutter mobile application.

---

## System Architecture & Workflow

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Incoming Mobile HTTP Request                    │
│ Header: X-Device-ID: "MB-12345"       (Required for all protected)     │
│ Header: X-Device-Token: "sec-token"   (Required — device fingerprint) │
│ Header: X-User-ID: "user-uuid"        (Optional verification header)   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│          Identity-Keyed Sliding Window Rate Limiter (rate_limit)       │
│  - Vision Scan: 5 req/min    - AI Chat: 10 req/min                    │
│  - Auth/Register: 10 req/min  - CRUD: 60 req/min                       │
│  - Triggers HTTP 429 Too Many Requests with Retry-After header         │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               FastAPI Security Dependency: get_current_identity        │
│ 1. Validates X-Device-Token against devices table (secrets.compare_digest)│
│ 2. Derives true user_id strictly from database ground truth            │
│ 3. Resolves caller's Identity(user_id="user-uuid", device_id="MB-12345")│
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       │                            │                            │
       ▼                            ▼                            ▼
┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│ [Scan Parsing]       │  │ [Session-Scoped CRUD]│  │ [AI Chat & RAG Engine]│
│ POST /scan/parse     │  │ GET  /receipts/      │  │ POST /chat/create    │
│ Gemini 3.6 Flash     │  │ POST /receipts/      │  │ GET  /chat/list      │
│ Vision AI            │  │ GET  /user/me        │  │ GET  /chat/history   │
│ Confidence >= 0.8    │  │ DELETE /user/me      │  │ POST /chat/query     │
└──────────────────────┘  │ GET  /devices/me     │  │ DELETE /chat/{id}    │
                          │ DELETE /devices/me   │  └──────────────────────┘
                          │ POST /devices/link   │
                          └──────────────────────┘
```

---

## API Endpoints & Implementation Status

| # | Endpoint Route | HTTP Method | Rate Limit (req/min) | Header Requirements | Description & Core Logic | Implementation Status |
|---|---|---|---|---|---|---|
| 1 | `/api/v1/health/` | `GET` | 120 | None | Health check, environment details, system status | **Implemented** |
| 2 | `/api/v1/devices/register` | `POST` | 10 | None | Idempotent device registration & fingerprint token refresh | **Implemented** |
| 3 | `/api/v1/devices/me` | `GET` | 60 | `X-Device-ID`, `X-Device-Token` | Retrieves device registration record for current hardware ID | **Implemented** |
| 4 | `/api/v1/devices/me` | `DELETE` | 60 | `X-Device-ID`, `X-Device-Token` | Soft-deletes calling device record (`deleted_at = now()`) | **Implemented** |
| 5 | `/api/v1/devices/link` | `POST` | 10 | `X-Device-ID`, `X-Device-Token` | Links device to user account & migrates orphan guest data | **Implemented** |
| 6 | `/api/v1/user/create` | `POST` | 10 | None | Public registration for new user account (PBKDF2 password hash) | **Implemented** |
| 7 | `/api/v1/user/login` | `POST` | 10 | None | Public user login authentication | **Implemented** |
| 8 | `/api/v1/user/me` | `GET` | 60 | `X-Device-ID`, `X-Device-Token` | Retrieves authenticated user profile (requires user session) | **Implemented** |
| 9 | `/api/v1/user/me` | `DELETE` | 60 | `X-Device-ID`, `X-Device-Token` | Soft-deletes user account & unlinks all active devices | **Implemented** |
| 10 | `/api/v1/scan/parse` | `POST` | 5 | `X-Device-ID`, `X-Device-Token` | Multimodal AI parsing via Gemini 3.6 Flash (10MB ceiling, >=0.8 confidence) | **Implemented** |
| 11 | `/api/v1/receipts/` | `GET` | 60 | `X-Device-ID`, `X-Device-Token` | Gets all non-deleted receipts owned by session identity | **Implemented** |
| 12 | `/api/v1/receipts/{receipt_id}` | `GET` | 60 | `X-Device-ID`, `X-Device-Token` | Gets single receipt by UUID (session ownership enforced) | **Implemented** |
| 13 | `/api/v1/receipts/` | `POST` | 60 | `X-Device-ID`, `X-Device-Token` | Creates a receipt record bound to session identity | **Implemented** |
| 14 | `/api/v1/receipts/batch` | `POST` | 60 | `X-Device-ID`, `X-Device-Token` | Batch creates up to 100 receipts bound to session identity | **Implemented** |
| 15 | `/api/v1/receipts/{receipt_id}` | `DELETE` | 60 | `X-Device-ID`, `X-Device-Token` | Soft-deletes receipt by UUID (session ownership enforced) | **Implemented** |
| 16 | `/api/v1/chat/create` | `POST` | 60 | `X-Device-ID`, `X-Device-Token` | Creates new AI conversation (max 10 limit per identity) | **Implemented** |
| 17 | `/api/v1/chat/list` | `GET` | 60 | `X-Device-ID`, `X-Device-Token` | Lists conversations owned by session identity | **Implemented** |
| 18 | `/api/v1/chat/history` | `GET` | 60 | `X-Device-ID`, `X-Device-Token` | Gets conversation message history (chunked limits) | **Implemented** |
| 19 | `/api/v1/chat/query` | `POST` | 10 | `X-Device-ID`, `X-Device-Token` | Conversational AI query over receipt history with RAG | **Implemented** |
| 20 | `/api/v1/chat/{conversation_id}` | `DELETE` | 60 | `X-Device-ID`, `X-Device-Token` | Soft-deletes conversation by UUID (session ownership enforced) | **Implemented** |

---

## Security Architecture & Threat Model

The Receipt Logger Backend enforces a multi-layered defense-in-depth security model:

### 1. Identity-Keyed Sliding Window Rate Limiter (`src/Auth/rate_limiter.py`)
- **Sliding Window Counter**: High-precision rolling window rate limiter that eliminates clock boundary burst attacks.
- **Identity Isolation**: Rate limits are keyed by `X-Device-ID` (with fallback to client IP), ensuring mobile devices on shared NAT/Wi-Fi networks do not throttle each other.
- **Standard HTTP 429 Response**: Returns `Retry-After`, `X-RateLimit-Limit`, and `X-RateLimit-Remaining` HTTP response headers.

### 2. Backend Service Gateway Model (Supabase Public Access Lockdown)
- **Zero Public PostgREST Access**: Public `anon` access to Supabase is **100% revoked and blocked** (`REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;`). Any direct HTTP query to Supabase PostgREST using the `anon` key is rejected immediately (`42501 Permission Denied`).
- **Single Trusted Gateway**: The database can **only** be accessed via the FastAPI backend using the secret `service_role` key (`SUPABASE_KEY`) or through the private Supabase Web Dashboard.

### 3. Header-Based Ground-Truth Identity Resolution (`src/Auth/identity.py`)
- **Constant-Time Token Comparison**: Device authentication compares client-supplied `X-Device-Token` against stored tokens using `secrets.compare_digest` to prevent cryptographic timing attacks.
- **Ground-Truth User Derivation**: Caller `user_id` is derived strictly from database records (`device.user_id`). Client-supplied `X-User-ID` headers are validated against ground truth and rejected (`401 Unauthorized`) on mismatch, preventing identity spoofing.

### 4. Database Row-Level Security (RLS) & Atomic Triggers (`migration/`)
- **RLS Across All Tables**: All 5 tables (`users`, `devices`, `receipts`, `conversations`, `chat_messages`) have RLS enabled (`ALTER TABLE ... ENABLE ROW LEVEL SECURITY;`) with explicit policies for `service_role` and `authenticated`.
- **Atomic 10-Conversation Cap**: Enforced at the PostgreSQL level via `BEFORE INSERT` trigger function (`enforce_max_conversations()`), preventing TOCTOU race conditions.
- **Cascading Session Termination**: User deletion (`DELETE /user/me`) executes `soft_delete_user` `SECURITY DEFINER` RPC function, setting `users.deleted_at = now()` and clearing `devices.user_id = NULL` to immediately terminate active sessions.

### 5. AI & Prompt Injection Hardening (`src/Services/`)
- **XML Boundary Isolation**: RAG receipt context is enclosed in XML tags (`<receipt_context> ... </receipt_context>`) with strict instructions to treat inner text as data, preventing prompt injection tag-breakout.
- **Document Type Validation**: `POST /scan/parse` requires Gemini 3.6 Flash `confidence_score >= 0.8` for receipts/financial statements before parsing.
- **Input Sanitization**: User messages and receipt merchant strings are sanitized (`<` and `>` escaped).

### 6. Network & DoS Protection (`main.py`, `src/API/v1/scan.py`)
- **Restricted CORS Policy**: CORS allows only localhost (`127.0.0.1`), local network IPs (`192.168.x.x`, `10.x.x.x`), and Tailscale domains (`100.x.x.x`, `*.ts.net`).
- **File Upload Ceiling**: `POST /api/v1/scan/parse` requires device identity and enforces a 10MB upload ceiling to prevent credit exhaustion DoS.

---

## Soft-Delete & Data Isolation Architecture

- **User Soft-Delete (`DELETE /api/v1/user/me`)**:
  - Requires user authentication (`require_user_identity`).
  - Sets `users.deleted_at = now()`.
  - Cascades device unlinking (`devices.user_id = NULL`), immediately terminating active sessions and reverting user's devices to guest mode to prevent "zombie" sessions.
- **Device Soft-Delete (`DELETE /api/v1/devices/me`)**:
  - Requires hardware token verification (`X-Device-Token`).
  - Sets `devices.deleted_at = now()`.
- **Chat Conversation Soft-Delete (`DELETE /api/v1/chat/{conversation_id}`)**:
  - Enforces `Identity` filter. Callers can **only** soft-delete conversations they own.
  - Returns `HTTP 404 Not Found` if conversation is unowned or missing.

---

## Security & Verification Standards

- **`200 OK`**: Successful read, update, soft-delete, or scan parsing.
- **`201 Created`**: Successful creation of user, device registration, receipt(s), or conversation.
- **`400 Bad Request`**: Missing required headers, empty parameters, file size exceeding 10MB limit, or conversation limit exceeded (max 10).
- **`401 Unauthorized`**: Unregistered device, invalid `device_token`, or missing user auth on `/me` routes.
- **`403 Forbidden`**: Cross-device link attempt (`body.device_id != identity.device_id`).
- **`404 Not Found`**: Resource does not exist, already deleted, or not owned by calling identity.
- **`409 Conflict`**: Username already taken on registration.
- **`422 Unprocessable Entity`**: Payload schema validation mismatch or malformed JSON data.
- **`429 Too Many Requests`**: Rate limit exceeded (includes `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining` headers).

---

## Technology Stack

- **Framework**: FastAPI 0.140+ with Uvicorn ASGI server
- **Authentication & Rate Limiting**: `src/Auth/` package (`Identity` model, `get_current_identity` dependency, `SlidingWindowRateLimiter` engine, constant-time `secrets.compare_digest` device token verification)
- **Backend Service Gateway Architecture**: Supabase `anon` access revoked; FastAPI connects via `service_role` key
- **Database Migrations & RLS**: Idempotent SQL scripts in `migration/` (`00_teardown_all.sql`, `01_schema_tables.sql`, `02_indexes_triggers.sql`, `03_rls_policies.sql`, `04_grants_permissions.sql`)
- **AI / LLM Engine**: `google-genai` SDK (`gemini-3.6-flash` Vision & RAG) with XML prompt boundary isolation
- **Containerization**: Docker & Docker Compose (`docker-compose.yml`, `Dockerfile`, `.dockerignore`)
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord`, `UserRecord`, `DeviceRecord`, `ConversationRecord`, `ChatMessageRecord`)
- **Database & Cloud Storage**: Supabase (`supabase-py`) `AsyncClient` for Postgres DB, Storage Buckets, and `pgvector`
- **Architecture Pattern**: Thin Routers + Repository Pattern per Model (`Receipts`, `Users`, `Devices`, `Conversations`)
- **Testing & Quality Assurance**: Automated Pytest suite (54 tests in `test/`) with `test-engineer` subagent and `security-advisor` vulnerability auditor
- **Configuration**: Pydantic `BaseSettings` (`python-dotenv`)
