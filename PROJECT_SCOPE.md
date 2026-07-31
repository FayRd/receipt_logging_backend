# Receipt Logger Backend — Project Scope & Architecture Blueprint

## Overview
The **Receipt Logger Backend** is an AI-powered REST API built with **FastAPI**, **Gemini 3.6 Flash Vision AI**, and **Supabase (Postgres, Storage & pgvector)**. It serves as the cloud intelligent processing engine for the privacy-first Flutter mobile application.

---

## System Architecture & Workflow

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Incoming Mobile HTTP Request                    │
│ Header: X-Device-ID: "MB-12345"       (Required for all calls)         │
│ Header: X-Device-Token: "sec-token"   (Required — device fingerprint) │
│ Header: X-User-ID: "user-uuid"        (Optional — present when logged) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               FastAPI Security Dependency: get_current_identity        │
│ 1. Validates X-Device-Token against devices table (secrets.compare_digest)│
│ 2. Resolves caller's Identity(user_id="user-uuid", device_id="MB-12345")│
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                  ┌─────────────────┴─────────────────┐
                  │                                   │
                  ▼                                   ▼
┌───────────────────────────────────┐   ┌───────────────────────────────────┐
│ [Scan Parsing Engine]             │   │ [Session-Scoped CRUD Engine]      │
│ POST /api/v1/scan/parse           │   │ GET  /api/v1/receipts/            │
│ Gemini 3.6 Flash Vision AI        │   │ POST /api/v1/receipts/            │
│ Returns validated JSON            │   │ GET  /api/v1/user/me              │
└───────────────────────────────────┘   │ GET  /api/v1/devices/me           │
                                        └───────────────────────────────────┘
```

---

## API Endpoints & Implementation Status

| # | Endpoint Route | HTTP Method | Header Requirements | Description & Core Logic | Implementation Status |
|---|---|---|---|---|---|
| 1 | `/api/v1/health/` | `GET` | None | Health check, environment details, system status | **Implemented** |
| 2 | `/api/v1/devices/register` | `POST` | None | Idempotent device registration & fingerprint token refresh | **Implemented** |
| 3 | `/api/v1/devices/me` | `GET` | `X-Device-ID`, `X-Device-Token` | Retrieves device registration record for current hardware ID | **Implemented** |
| 4 | `/api/v1/devices/link` | `POST` | `X-Device-ID`, `X-Device-Token` | Links or unlinks current device to a user account | **Implemented** |
| 5 | `/api/v1/user/create` | `POST` | None | Public registration for new user account (PBKDF2 password hash) | **Implemented** |
| 6 | `/api/v1/user/login` | `POST` | None | Public user login authentication | **Implemented** |
| 7 | `/api/v1/user/me` | `GET` | `X-Device-ID`, `X-Device-Token`, `X-User-ID` | Retrieves authenticated user profile | **Implemented** |
| 8 | `/api/v1/scan/parse` | `POST` | Multipart upload | Extracts structured JSON via Gemini 3.6 Flash Vision API | **Implemented** |
| 9 | `/api/v1/receipts/` | `GET` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Gets all non-deleted receipts owned by session identity | **Implemented** |
| 10 | `/api/v1/receipts/{receipt_id}` | `GET` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Gets single receipt by UUID (session ownership enforced) | **Implemented** |
| 11 | `/api/v1/receipts/` | `POST` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Creates a receipt record bound to session identity | **Implemented** |
| 12 | `/api/v1/receipts/batch` | `POST` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Batch creates up to 100 receipts bound to session identity | **Implemented** |
| 13 | `/api/v1/receipts/{receipt_id}` | `DELETE` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Soft-deletes receipt by UUID (session ownership enforced) | **Implemented** |
| 14 | `/api/v1/chat/create` | `POST` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Creates a new AI conversation | *Planned* |
| 15 | `/api/v1/chat/list` | `GET` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Lists user's AI conversations | *Planned* |
| 16 | `/api/v1/chat/history` | `GET` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Gets conversation message history (chunked limits) | *Planned* |
| 17 | `/api/v1/chat/query` | `POST` | `X-Device-ID`, `X-Device-Token`, `X-User-ID`? | Conversational AI query over receipt history | *Planned* |

---

## HTTP Status Code Standards

- **`200 OK`**: Successful read, update, soft-delete, or scan parsing.
- **`201 Created`**: Successful creation of user, device registration, or receipt(s).
- **`400 Bad Request`**: Missing required headers or empty parameter strings.
- **`401 Unauthorized`**: Unregistered device, invalid `device_token`, or missing user auth on `/me` routes.
- **`404 Not Found`**: Resource does not exist or is not owned by the calling identity.
- **`409 Conflict`**: Username already taken on registration.
- **`422 Unprocessable Entity`**: Payload schema validation mismatch or malformed JSON data.
- **`500 Internal Server Error`**: Unexpected server error.

---

## Technology Stack

- **Framework**: FastAPI 0.140+ with Uvicorn ASGI server
- **Authentication & Security**: `src/Auth/` package (`Identity` model, `get_current_identity` dependency, constant-time `secrets.compare_digest` device token verification)
- **AI / LLM Engine**: `google-genai` SDK (`gemini-3.6-flash` Vision) for multimodal structured output parsing
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord`, `UserRecord`, `DeviceRecord`)
- **Database & Cloud Storage**: Supabase (`supabase-py`) `AsyncClient` for Postgres DB, Storage Buckets, and `pgvector`
- **Architecture Pattern**: Thin Routers + Repository Pattern per Model (`src/Models/Receipts/`, `src/Models/Users/`, `src/Models/Devices/`)
- **Configuration**: Pydantic `BaseSettings` (`python-dotenv`)
