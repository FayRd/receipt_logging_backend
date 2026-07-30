# Receipt Logger Backend — Project Scope & Architecture Blueprint

## Overview
The **Receipt Logger Backend** is an AI-powered REST API built with **FastAPI**, **Gemini 3.6 Flash Vision AI**, and **Supabase (Postgres, Storage & pgvector)**. It serves as the cloud intelligent processing engine for the privacy-first Flutter mobile application.

---

## System Architecture & Workflow

```
[Flutter Mobile App] 
       │ (1) POST /api/v1/scan/parse (Multipart Image + device_id)
       ▼
[FastAPI Backend] ──(2) Image Bytes + System Prompt──> [Google Gemini 3.6 Flash Vision API]
       │                                                              │
       │ <──(3) Validated Pydantic Receipt JSON ──────────────────────┘
       │
       ├──(4) Return JSON Response to Client ──> [Flutter App (Isar DB & Review Sheet)]
       │
       └──(5) POST /api/v1/receipts/ (Create/Batch Sync) ──> [Supabase Postgres DB]
```

---

## API Endpoints & Implementation Status

| # | Endpoint Route | HTTP Method | Description & Core Logic | Implementation Status |
|---|---|---|---|---|
| 1 | `/api/v1/health` | `GET` | Health check, environment details, system status | **Implemented** ([health.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/health.py)) |
| 2 | `/api/v1/scan/parse` | `POST` | Accepts multipart receipt image, extracts structured JSON via Gemini 3.6 Flash Vision API | **Implemented** ([scan.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/scan.py)) |
| 3 | `/api/v1/receipts/user/{user_id}` | `GET` | Gets all non-deleted receipts owned by a specific user UUID | **Implemented** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 4 | `/api/v1/receipts/{receipt_id}` | `GET` | Gets a single receipt by UUID (requires `user_id` query param ownership check) | **Implemented** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 5 | `/api/v1/receipts/` | `POST` | Creates a new receipt row in Supabase associated with owner's `user_id` | **Implemented** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 6 | `/api/v1/receipts/batch` | `POST` | Batch creates up to 100 receipt records for a user in a single DB call | **Implemented** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 7 | `/api/v1/receipts/{receipt_id}` | `DELETE` | Soft-deletes a receipt by setting `deleted_at` timestamp (requires `user_id` query check) | **Implemented** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 8 | `/api/v1/chat/query` | `POST` | RAG AI conversational query over user receipt embeddings (`pgvector`) | *Planned* |

---

## Technology Stack

- **Framework**: FastAPI 0.140+ with Uvicorn ASGI server
- **AI / LLM Engine**: `google-genai` SDK (`gemini-3.6-flash` Vision) for multimodal structured output parsing
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord` with UUID string IDs, `ReceiptCreateRequest`, `ReceiptBatchCreateRequest`)
- **Database & Cloud Storage**: Supabase (`supabase-py`) for Postgres DB, Storage Buckets, and `pgvector` embeddings
- **Architecture Pattern**: Thin Routers + Repository Pattern per Model (`src/Models/Receipts/receipt_repository.py`)
- **Configuration**: Pydantic `BaseSettings` (`python-dotenv`)

---

## Backend Codebase Audit & Active Files

1. **App Entrypoint** — [main.py](file:///C:/mobile-development/receipt_logging_backend/main.py)
2. **Environment Configuration** — [config.py](file:///C:/mobile-development/receipt_logging_backend/src/config.py)
3. **Data Schemas** — [schemas.py](file:///C:/mobile-development/receipt_logging_backend/src/Models/schemas.py)
4. **Receipts Repository** — [receipt_repository.py](file:///C:/mobile-development/receipt_logging_backend/src/Models/Receipts/receipt_repository.py)
5. **AI Extraction Service** — [extraction_service.py](file:///C:/mobile-development/receipt_logging_backend/src/Services/extraction_service.py)
6. **Database Infrastructure** — [database.py](file:///C:/mobile-development/receipt_logging_backend/src/Infrastructure/database.py)
7. **Scan API Router** — [scan.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/scan.py)
8. **Receipts API Router** — [receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)
9. **Health API Router** — [health.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/health.py)
