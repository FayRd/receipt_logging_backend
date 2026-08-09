# Backend To-Do Audit & Implementation Proposal

This document summarizes the current implementation status of backend requirements and details the technical proposals for missing or incomplete features in `receipt_logging_backend`.

---

## 1. Executive Summary & Audit Matrix

| Feature Requirement | Status | Existing Implementation | Gaps & Required Changes |
| :--- | :---: | :--- | :--- |
| **1. Job Queues** | ⚠️ Incomplete | In-memory `FastAPI.BackgroundTasks` & basic Redis hashes for `/receipts/bulk`. | Lacks durable task queue worker (e.g. ARQ/Celery), dead-letter handling, persistent retries, and job queue integration for single `/scan/parse-once`. |
| **2. SSE Bulk Batch Streaming** | ✅ Completed | `/api/v1/receipts/bulk/{batch_id}/stream` | Direct JSON payload streaming in `batch_complete` event, Dual Auth (Headers & Query params `?device_id=...&device_token=...`), and 10-minute TTL key expiration. |
| **3. Server-side Validation** | ⚠️ Partial | Basic Pydantic models & file size check. | Lacks magic-bytes file signature verification, strict query string length/sanitization, and comprehensive payload boundary checks. |
| **4. Server-side End-to-End Encryption** | ❌ Not Started | Standard JSON over HTTPS. | Missing AES-GCM / NaCl middleware to decrypt incoming encrypted payloads and encrypt outgoing JSON API responses. |
| **5. Supabase Encryption-at-Rest** | ❌ Not Started | Plaintext columns in PostgreSQL tables. | Missing PostgreSQL `pgcrypto` column-level encryption (CLE) or Supabase Vault key management for sensitive receipt data. |
| **6. Bulk Processing (`/scan/parse-many`)** | ⚠️ Partial | `/api/v1/receipts/bulk` endpoint exists. | Needs endpoint contract update to `/scan/parse-many` and integration with the centralized job queue engine. |
| **7. Dynamic Tier Rate Limiting** | ❌ Not Started | Fixed per-device IP/ID sliding window limiter. | Lacks user role inspection (`anonymous`, `authenticated`, `paid`) to enforce tier-based dynamic quota limits. |

---

## 2. Technical Proposals for Incomplete Features

### Proposal 1: Distributed Job Queue Engine (`ARQ` / Redis Streams)
* **Goal**: Replace in-memory background tasks with a durable, asynchronous task queue for single (`/scan/parse-once`) and bulk (`/scan/parse-many`) receipt parsing.
* **Proposed Architecture**:
  - Integrate **ARQ** (Async Redis Queue) for non-blocking task execution and automatic retries.
  - Implement `/api/v1/scan/parse-once`: Accepts image, queues job, returns `job_id` for client polling or SSE tracking.
  - Implement `/api/v1/scan/parse-many`: Accepts array of images, creates batch queue, returns `batch_id` and list of `job_id`s.
  - Workers stream real-time progress (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`) to Redis.

### Proposal 2: Robust Server-side Payload & File Validation
* **Goal**: Prevent malicious file uploads, injection attacks, and invalid payloads.
* **Proposed Implementation**:
  - **Magic-Bytes Validation**: Inspect file headers (`\xFF\xD8\xFF` for JPEG, `\x89PNG` for PNG, `RIFF...WEBP` for WEBP) rather than trusting MIME `Content-Type` headers.
  - **Strict Field Validation**: Add Pydantic field constraints (`MinLen`, `MaxLen`, regex pattern matching) on chat queries, user inputs, and batch parameter arrays.
  - **Sanitization**: Strip control characters and sanitize incoming prompt inputs before passing to Gemini AI services.

### Proposal 3: Server-side Payload Encryption Middleware
* **Goal**: Provide end-to-end payload encryption between client and backend.
* **Proposed Implementation**:
  - Implement custom FastAPI Middleware (`EncryptionMiddleware`) using **AES-256-GCM** or **libsodium (NaCl)**.
  - **Inbound**: Intercept incoming request body, decrypt using shared session key derived from device authentication headers, pass decrypted JSON to endpoint route.
  - **Outbound**: Intercept outgoing `JSONResponse`, encrypt payload with AES-GCM, set `Content-Type: application/octet-stream` or `application/encrypted-json`.

### Proposal 4: Supabase Column-Level Encryption at Rest
* **Goal**: Protect sensitive financial information (total amounts, merchant names, raw OCR text) at rest in Supabase PostgreSQL database.
* **Proposed SQL Migrations**:
  - Enable `pgcrypto` extension in Supabase PostgreSQL:
    ```sql
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    ```
  - Store sensitive fields as `BYTEA` encrypted via `pgp_sym_encrypt(value, key)` or Supabase Vault transparent column encryption.
  - Create database views / functions for secure decryption during server read operations.

### Proposal 5: Endpoint Restructuring (`/scan/parse-once` & `/scan/parse-many`)
* **Goal**: Align backend routing contracts with frontend client expectations.
* **Proposed Routes**:
  - `POST /api/v1/scan/parse-once`: Single receipt processing via job queue.
  - `POST /api/v1/scan/parse-many`: Bulk receipt processing (2-10 receipts) via batch job queue.
  - `GET /api/v1/scan/jobs/{job_id}`: Single job status and result fetch.
  - `GET /api/v1/scan/batches/{batch_id}`: Batch status and aggregated results fetch.
  - `GET /api/v1/scan/batches/{batch_id}/stream`: SSE endpoint for real-time progress events.

### Proposal 6: Tier-Based Rate Limiting (`anonymous`, `authenticated`, `paid`)
* **Goal**: Restrict API usage dynamically based on user identity tier.
* **Proposed Rate Limiter Overhaul**:
  - Update `SlidingWindowRateLimiter` in `src/Auth/rate_limiter.py`.
  - Inspect `Identity` dependency context (`user_role` or claims):
    - **Anonymous**: 5 requests / min (IP-based).
    - **Authenticated Free**: 20 requests / min (User/Device-based).
    - **Paid Tier**: 100+ requests / min (User-based).
  - Return dynamic headers `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
