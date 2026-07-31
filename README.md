# Receipt Logger Backend

An AI-powered FastAPI backend service for receipt scanning, structured data extraction using **Google Gemini 3.6 Flash Vision AI**, session-scoped identity security, and cloud database synchronization for the **Receipt Logger** mobile application.

---

## 🚀 Application Summary

The **Receipt Logger Backend** provides high-speed, intelligent multimodal receipt parsing and data management for the privacy-first mobile client. It accepts receipt image uploads, processes them directly with Gemini 3.6 Flash Vision AI using strict structured Pydantic schemas, and returns validated JSON containing merchant info, line items, totals, dates, and categories in ~1.5 seconds. It also provides a full set of session-scoped CRUD endpoints for storing and managing receipts in Supabase with cryptographic device fingerprint verification (`X-Device-Token`).

### Key Technology Stack
- **Framework**: FastAPI (Python 3.10+) with Uvicorn ASGI server
- **Auth & Security**: `src/Auth/` package (`Identity` model, `X-Device-Token` verification via constant-time `secrets.compare_digest`)
- **AI Extraction**: `google-genai` SDK (`gemini-3.6-flash` Multimodal Vision)
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`, `ReceiptRecord`, `UserRecord`, `DeviceRecord`)
- **Cloud Database & Storage**: Supabase (`supabase-py` `AsyncClient`) for Postgres DB, Storage, and Vector search
- **Architecture**: Layered Architecture with per-model repository pattern (`src/Models/Receipts/`, `src/Models/Users/`, `src/Models/Devices/`)
- **Configuration**: Pydantic `BaseSettings` & `python-dotenv`

---

## ✨ Features Breakdown

### Implemented Features
- **Device Registration & Token Verification** (`/api/v1/devices`):
  - `POST /api/v1/devices/register`: Idempotent registration of hardware `device_id` and secret `device_token`.
  - `GET /api/v1/devices/me`: Retrieves current device registration record.
  - `POST /api/v1/devices/link`: Links/unlinks current device to a user account.
- **User Authentication** (`/api/v1/user`):
  - `POST /api/v1/user/create`: Registers a new user account (PBKDF2/SHA-256 server-side password hash).
  - `POST /api/v1/user/login`: Authenticates credentials in constant time to prevent timing attacks.
  - `GET /api/v1/user/me`: Retrieves authenticated user profile (session-scoped).
- **Multimodal AI Receipt Extraction** (`POST /api/v1/scan/parse`):
  - Accepts multipart/form-data image uploads (`.png`, `.jpg`, `.jpeg`, `.webp`).
  - Directly feeds image bytes to Gemini 3.6 Flash with strict JSON schema enforcement.
  - Extracts merchant name, line items, subtotal, tax, total amount, currency, ISO 8601 date, raw OCR text, and category inference.
- **Session-Scoped Receipt CRUD Endpoints** (`/api/v1/receipts`):
  - `GET /api/v1/receipts/`: Retrieves all non-deleted receipts owned by session identity (user or guest device).
  - `GET /api/v1/receipts/{receipt_id}`: Retrieves a single receipt by UUID, enforcing session ownership.
  - `POST /api/v1/receipts/`: Creates a single receipt record bound to session identity.
  - `POST /api/v1/receipts/batch`: Batch-creates up to 100 receipts bound to session identity in a single Supabase call.
  - `DELETE /api/v1/receipts/{receipt_id}`: Soft-deletes a receipt by setting `deleted_at` timestamp.
- **Explicit HTTP 422 Error Handling**: Custom exception handlers in `main.py` intercept any request payload schema mismatches or invalid data types and return clean `HTTP 422 Unprocessable Entity` responses.
- **Health Check & Diagnostics** (`GET /api/v1/health/`): Returns API operational status and environment setting.

---

## 🛠️ Header Authentication Contract

All protected endpoints require the following HTTP headers:

| Header Name | Type | Required? | Description |
|---|---|---|---|
| `X-Device-ID` | `string` | **Yes** | Unique hardware device identifier string (e.g. `MB-12345`). |
| `X-Device-Token` | `string` | **Yes** | Secret cryptographic device fingerprint token generated on first app boot. |
| `X-User-ID` | `string` | Optional | Authenticated user UUID string (supplied when signed in). |

---

## 🏃 Running the Application

### Option A: Using the PowerShell Script (Recommended for Windows)

Run the included `run.ps1` script to start the server on port **8085**:

```powershell
.\run.ps1
```

---

### Option B: Manual Command Line

```bash
uvicorn main:app --host 0.0.0.0 --port 8085 --reload
```

---

## 🌐 API Documentation & Interactive Docs

Once running, access the interactive API documentation at:
- **Swagger UI**: [http://localhost:8085/docs](http://localhost:8085/docs)
- **ReDoc**: [http://localhost:8085/redoc](http://localhost:8085/redoc)
- **OpenAPI Schema**: [http://localhost:8085/openapi.json](http://localhost:8085/openapi.json)
