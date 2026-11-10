-- 01_schema_tables.sql
-- Idempotent schema initialization for all core and compliance database tables

-- ── 0. CUSTOM TYPES ──────────────────────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE user_tier AS ENUM ('free', 'premium', 'dev');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- ── 1. USERS TABLE ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT NOT NULL,
    email TEXT NOT NULL,
    password TEXT,
    google_id TEXT,
    country_code TEXT,
    mobile_number TEXT,
    avatar_image_path TEXT,
    custom_categories JSONB DEFAULT '[]'::jsonb,
    preferences JSONB DEFAULT '{}'::jsonb,
    email_verified_at TIMESTAMPTZ,
    mobile_verified_at TIMESTAMPTZ,
    tier user_tier NOT NULL DEFAULT 'free',
    is_2fa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    enc_version SMALLINT NOT NULL DEFAULT 0,
    mobile_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'users' AND column_name = 'google_id'
    ) THEN
        ALTER TABLE users ADD COLUMN google_id TEXT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'users' AND column_name = 'is_2fa_enabled'
    ) THEN
        ALTER TABLE users ADD COLUMN is_2fa_enabled BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'users' AND column_name = 'enc_version'
    ) THEN
        ALTER TABLE users ADD COLUMN enc_version SMALLINT NOT NULL DEFAULT 0;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'users' AND column_name = 'mobile_hash'
    ) THEN
        ALTER TABLE users ADD COLUMN mobile_hash TEXT;
    END IF;
    ALTER TABLE users ALTER COLUMN password DROP NOT NULL;
END $$;

-- ── 2. DEVICES TABLE ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    device_token_hash TEXT NOT NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    trial_consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'devices' AND column_name = 'trial_consumed_at'
    ) THEN
        ALTER TABLE devices ADD COLUMN trial_consumed_at TIMESTAMPTZ;
    END IF;
END $$;

-- ── 3. RECEIPTS TABLE ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    receipt JSONB NOT NULL,
    receipt_image_path TEXT,
    enc_version SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'receipts' AND column_name = 'enc_version'
    ) THEN
        ALTER TABLE receipts ADD COLUMN enc_version SMALLINT NOT NULL DEFAULT 0;
    END IF;
END $$;

-- ── 4. CONVERSATIONS TABLE ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT 'New Conversation',
    enc_version SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'conversations' AND column_name = 'enc_version'
    ) THEN
        ALTER TABLE conversations ADD COLUMN enc_version SMALLINT NOT NULL DEFAULT 0;
    END IF;
END $$;

-- ── 5. CHAT MESSAGES TABLE ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender TEXT NOT NULL CHECK (sender IN ('user', 'assistant')),
    content TEXT NOT NULL,
    enc_version SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chat_messages' AND column_name = 'enc_version'
    ) THEN
        ALTER TABLE chat_messages ADD COLUMN enc_version SMALLINT NOT NULL DEFAULT 0;
    END IF;
END $$;

-- ── 6. FORGET PASSWORD REQUESTS TABLE ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS forget_password (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT,
    mobile_number TEXT,
    otp_hash TEXT NOT NULL,
    reset_token_hash TEXT,
    attempts_count INT NOT NULL DEFAULT 0,
    is_used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- ── 7. PER-USER DATA ENCRYPTION KEY (DEK) TABLE ─────────────────────────────
CREATE TABLE IF NOT EXISTS user_keys (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    encrypted_dek TEXT NOT NULL,
    kek_version SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 8. COMPLIANCE AUDIT TRAIL TABLE ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deletion_audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    username TEXT,
    email_hash TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dek_destroyed_at TIMESTAMPTZ,
    storage_scrubbed_at TIMESTAMPTZ,
    rows_hard_deleted INT DEFAULT 0,
    rows_crypto_shredded INT DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
);

-- ── 9. IDEMPOTENT ASYNC STORAGE SCRUB QUEUE TABLE ────────────────────────────
CREATE TABLE IF NOT EXISTS storage_deletion_queue (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    storage_prefix TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    attempt_count INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
);
