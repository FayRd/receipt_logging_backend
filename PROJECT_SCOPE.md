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
│ Structured JSON      │  │ DELETE /user/me      │  │ POST /chat/query     │
└──────────────────────┘  │ GET  /devices/me     │  │ DELETE /chat/{id}    │
                          │ DELETE /devices/me   │  └──────────────────────┘
                          │ POST /devices/link   │
                          └──────────────────────┘
```

---

## API Endpoints & Implementation Status

| # | Endpoint Route | HTTP Method | Header Requirements | Description & Core Logic | Implementation Status |
|---|---|---|---|---|---|
| 1 | `/api/v1/health/` | `GET` | None | Health check, environment details, system status | **Implemented** |
| 2 | `/api/v1/devices/register` | `POST` | None | Idempotent device registration & fingerprint token refresh | **Implemented** |
| 3 | `/api/v1/devices/me` | `GET` | `X-Device-ID`, `X-Device-Token` | Retrieves device registration record for current hardware ID | **Implemented** |
| 4 | `/api/v1/devices/me` | `DELETE` | `X-Device-ID`, `X-Device-Token` | Soft-deletes calling device record (`deleted_at = now()`) | **Implemented** |
| 5 | `/api/v1/devices/link` | `POST` | `X-Device-ID`, `X-Device-Token` | Links device to user account & migrates orphan guest data | **Implemented** |
| 6 | `/api/v1/user/create` | `POST` | None | Public registration for new user account (PBKDF2 password hash) | **Implemented** |
| 7 | `/api/v1/user/login` | `POST` | None | Public user login authentication | **Implemented** |
| 8 | `/api/v1/user/me` | `GET` | `X-Device-ID`, `X-Device-Token` | Retrieves authenticated user profile (requires user session) | **Implemented** |
| 9 | `/api/v1/user/me` | `DELETE` | `X-Device-ID`, `X-Device-Token` | Soft-deletes user account & unlinks all active devices | **Implemented** |
| 10 | `/api/v1/scan/parse` | `POST` | Multipart upload | Extracts structured JSON via Gemini 3.6 Flash Vision API | **Implemented** |
| 11 | `/api/v1/receipts/` | `GET` | `X-Device-ID`, `X-Device-Token` | Gets all non-deleted receipts owned by session identity | **Implemented** |
| 12 | `/api/v1/receipts/{receipt_id}` | `GET` | `X-Device-ID`, `X-Device-Token` | Gets single receipt by UUID (session ownership enforced) | **Implemented** |
| 13 | `/api/v1/receipts/` | `POST` | `X-Device-ID`, `X-Device-Token` | Creates a receipt record bound to session identity | **Implemented** |
| 14 | `/api/v1/receipts/batch` | `POST` | `X-Device-ID`, `X-Device-Token` | Batch creates up to 100 receipts bound to session identity | **Implemented** |
| 15 | `/api/v1/receipts/{receipt_id}` | `DELETE` | `X-Device-ID`, `X-Device-Token` | Soft-deletes receipt by UUID (session ownership enforced) | **Implemented** |
| 16 | `/api/v1/chat/create` | `POST` | `X-Device-ID`, `X-Device-Token` | Creates new AI conversation (max 10 limit per identity) | **Implemented** |
| 17 | `/api/v1/chat/list` | `GET` | `X-Device-ID`, `X-Device-Token` | Lists conversations owned by session identity | **Implemented** |
| 18 | `/api/v1/chat/history` | `GET` | `X-Device-ID`, `X-Device-Token` | Gets conversation message history (chunked limits) | **Implemented** |
| 19 | `/api/v1/chat/query` | `POST` | `X-Device-ID`, `X-Device-Token` | Conversational AI query over receipt history with RAG | **Implemented** |
| 20 | `/api/v1/chat/{conversation_id}` | `DELETE` | `X-Device-ID`, `X-Device-Token` | Soft-deletes conversation by UUID (session ownership enforced) | **Implemented** |

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
- **`400 Bad Request`**: Missing required headers, empty parameters, or conversation limit exceeded (max 10).
- **`401 Unauthorized`**: Unregistered device, invalid `device_token`, or missing user auth on `/me` routes.
- **`403 Forbidden`**: Cross-device link attempt (`body.device_id != identity.device_id`).
- **`404 Not Found`**: Resource does not exist, already deleted, or not owned by calling identity.
- **`409 Conflict`**: Username already taken on registration.
- **`422 Unprocessable Entity`**: Payload schema validation mismatch or malformed JSON data.

---

## Technology Stack

- **Framework**: FastAPI 0.140+ with Uvicorn ASGI server
- **Authentication & Security**: `src/Auth/` package (`Identity` model, `get_current_identity` dependency, constant-time `secrets.compare_digest` device token verification, DB ground-truth identity resolution)
- **AI / LLM Engine**: `google-genai` SDK (`gemini-3.6-flash` Vision & RAG) for multimodal parsing and conversational financial assistance
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord`, `UserRecord`, `DeviceRecord`, `ConversationRecord`, `ChatMessageRecord`)
- **Database & Cloud Storage**: Supabase (`supabase-py`) `AsyncClient` for Postgres DB, Storage Buckets, and `pgvector`
- **Architecture Pattern**: Thin Routers + Repository Pattern per Model (`Receipts`, `Users`, `Devices`, `Conversations`)
- **Testing & Quality Assurance**: Automated Pytest suite (46 tests in `test/`) with `test-engineer` subagent and `security-advisor` vulnerability auditor
- **Configuration**: Pydantic `BaseSettings` (`python-dotenv`)
