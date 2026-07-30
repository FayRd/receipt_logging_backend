# Receipt Logger Backend

An AI-powered FastAPI backend service for receipt scanning, structured data extraction using **Google Gemini 1.5 Flash Vision AI**, and cloud database synchronization for the **Receipt Logger** mobile application.

---

## 🚀 Application Summary

The **Receipt Logger Backend** provides high-speed, intelligent multimodal receipt parsing and data management for the privacy-first mobile client. It accepts receipt image uploads, processes them directly with Gemini Vision AI using strict structured Pydantic schemas, and returns validated JSON containing merchant info, line items, totals, dates, and categories in ~1.5 seconds.

### Key Technology Stack
- **Framework**: FastAPI (Python 3.10+) with Uvicorn ASGI server
- **AI Extraction**: `google-genai` SDK (`gemini-1.5-flash` Multimodal Vision)
- **Data Validation**: Pydantic v2 schemas (`Receipt`, `LineItem`, `ScanResponse`)
- **Cloud Database & Storage**: Supabase (`supabase-py`) for Postgres DB, Storage, and Vector search
- **Configuration**: Pydantic `BaseSettings` & `python-dotenv`

---

## ✨ Features Breakdown

### Implemented Features
- **Multimodal AI Receipt Extraction** (`POST /api/v1/scan/parse`):
  - Accepts multipart/form-data image uploads (`.png`, `.jpg`, `.jpeg`, `.webp`).
  - Directly feeds image bytes to Gemini 1.5 Flash with strict JSON schema enforcement.
  - Extracts merchant name, line items, subtotal, tax, total amount, currency, ISO 8601 date, raw OCR text, and category inference.
  - Always returns HTTP 200 with structured `success`, `data`, and `error` payloads (no regex fallback required).
- **Health Check & Diagnostics** (`GET /api/v1/health`): Returns API operational status and current environment setting.
- **CORS & Environment Setup**: Configurable CORS middleware supporting cross-origin mobile app calls.

### Planned Features
- **Supabase Cloud Sync** (`/api/v1/receipts`): Sync local offline Isar receipts to cloud Postgres database.
- **Soft Delete Management**: Soft-delete records via `deleted_at` timestamps.
- **Image Storage Archiving**: Non-blocking background task to archive scanned receipt images in Supabase Storage buckets.
- **RAG Conversational AI Assistant** (`/api/v1/chat/query`): Retrieval-Augmented Generation over receipt database embeddings (`pgvector`) allowing users to ask natural language questions about spending trends and items.

---

## 🛠️ Setup & Installation

### Prerequisites
- Python 3.10 or higher
- PowerShell (Windows) or Bash (macOS/Linux)
- A Google Gemini API Key

---

### Step 1: Create Virtual Environment (`.venv`)

In the project root directory, run:

**Windows (PowerShell):**
```powershell
python -m venv .venv
```

**macOS / Linux:**
```bash
python3 -m venv .venv
```

---

### Step 2: Activate `.venv` & Install Dependencies

**Windows (PowerShell):**
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

---

### Step 3: Environment Configuration (`.env`)

Create a `.env` file in the project root with your credentials:

```env
SUPABASE_URL=https://your-supabase-project.supabase.co
SUPABASE_KEY=your-supabase-anon-key
GEMINI_API_KEY=your-gemini-api-key
ENVIRONMENT=development
ALLOWED_ORIGINS=["*"]
```

---

## 🏃 Running the Application

### Option A: Using the PowerShell Script (Recommended for Windows)

Run the included `run.ps1` script to start the server on port **8085**:

```powershell
.\run.ps1
```

> **Note**: If PowerShell blocks script execution, grant permission once via:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

---

### Option B: Manual Command Line

**Windows / macOS / Linux:**
```bash
uvicorn main:app --host 0.0.0.0 --port 8085 --reload
```

---

## 🌐 API Documentation & Interactive Docs

Once running, access the interactive API documentation at:
- **Swagger UI**: [http://localhost:8085/docs](http://localhost:8085/docs)
- **ReDoc**: [http://localhost:8085/redoc](http://localhost:8085/redoc)
- **OpenAPI Schema**: [http://localhost:8085/openapi.json](http://localhost:8085/openapi.json)

---

## 📁 Project Structure

```
receipt_logging_backend/
├── .venv/                      # Python virtual environment
├── main.py                     # FastAPI application entrypoint & middleware
├── config.py                   # Pydantic BaseSettings environment loader
├── requirements.txt            # Python dependencies
├── run.ps1                     # PowerShell launch script (port 8085)
├── PROJECT_SCOPE.md            # Comprehensive project scope & endpoint specs
├── project_struc.md            # Layered architecture explanation
└── src/                        # Core Application Source Code
    ├── API/v1/                 # Presentation Layer (health, scan, receipts)
    ├── Infrastructure/         # Data providers (database.py)
    ├── Models/                 # Pydantic DTOs & Schemas (schemas.py)
    └── Services/               # AI Extraction Service (extraction_service.py)
```
