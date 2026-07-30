# Receipt Logger Backend — Project Scope & Architecture Blueprint

## Overview
The **Receipt Logger Backend** is an AI-powered REST API built with **FastAPI**, **Gemini 2.0/1.5 Flash Vision AI**, and **Supabase (Postgres, Storage & pgvector)**. It serves as the cloud intelligent processing engine for the privacy-first Flutter mobile application.

---

## System Architecture & Workflow

```
[Flutter Mobile App] 
       │ (1) POST /api/v1/scan/upload (Multipart Image Bytes)
       ▼
[FastAPI Backend] ──(2) Image Bytes + Prompt──> [Google Gemini Flash Vision API]
       │                                                      │
       │ <──(3) Validated Pydantic Receipt JSON ──────────────┘
       │
       ├──(4) Return JSON Response to Client ──> [Flutter App (Isar DB & Review Sheet)]
       │
       └──(5) Async Background Task ──> [Supabase Storage (Image)] & [Supabase DB (pgvector RAG)]
```

---

## Planned API Endpoints & Implementation Status

| # | Endpoint Route | HTTP Method | Description & Core Logic | Implementation Status |
|---|---|---|---|---|
| 1 | `/api/v1/health` | `GET` | Health check, environment details, system status | **Implemented** ([health.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/health.py)) |
| 2 | `/api/v1/scan/upload` | `POST` | Accepts multipart receipt image, extracts structured JSON via Gemini Flash Vision API | **Revamp Pending** ([scan.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/scan.py)) |
| 3 | `/api/v1/receipts/` | `POST` | Sync endpoint for mobile client to upload local Isar receipts to cloud Supabase DB | **Revamp Pending** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 4 | `/api/v1/receipts/` | `GET` | Paginated retrieval of user receipts from Supabase | **Revamp Pending** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 5 | `/api/v1/receipts/{id}` | `GET` | Fetch single receipt by UUID | **Revamp Pending** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 6 | `/api/v1/receipts/{id}` | `DELETE` | Soft-delete receipt record by setting `deleted_at` timestamp | **Revamp Pending** ([receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)) |
| 7 | `/api/v1/chat/query` | `POST` | RAG AI conversational query over user receipt embeddings (`pgvector`) | *Planned* |

---

## Technology Stack

- **Framework**: FastAPI 0.140+ with Uvicorn ASGI server
- **AI / LLM Engine**: `google-genai` SDK (Gemini 2.0 / 1.5 Flash Vision) for multimodal structured output parsing
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`)
- **Database & Cloud Storage**: Supabase (`supabase-py`) for Postgres DB, Storage Buckets, and `pgvector` embeddings
- **Configuration**: Pydantic `BaseSettings` (`python-dotenv`)
- **Background Tasks**: FastAPI `BackgroundTasks` for non-blocking storage uploads & vector indexing

---

## Backend Codebase Audit & Active Files

1. **App Entrypoint** — [main.py](file:///C:/mobile-development/receipt_logging_backend/main.py)
2. **Environment Configuration** — [config.py](file:///C:/mobile-development/receipt_logging_backend/src/config.py)
3. **Data Schemas** — [schemas.py](file:///C:/mobile-development/receipt_logging_backend/src/Models/schemas.py)
4. **AI Extraction Service** — [extraction_service.py](file:///C:/mobile-development/receipt_logging_backend/src/Services/extraction_service.py)
5. **Database Infrastructure** — [database.py](file:///C:/mobile-development/receipt_logging_backend/src/Infrastructure/database.py)
6. **Scan API Router** — [scan.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/scan.py)
7. **Receipts API Router** — [receipts.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/receipts.py)
8. **Health API Router** — [health.py](file:///C:/mobile-development/receipt_logging_backend/src/API/v1/health.py)
