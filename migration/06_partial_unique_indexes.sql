-- 06_partial_unique_indexes.sql
-- Idempotent migration to replace table-level unique constraints with partial unique indexes on active users (WHERE deleted_at IS NULL).
-- Allows soft-deleted emails, usernames, and Google IDs to be re-registered safely without 23505 duplicate key violations.

-- 1. Drop old table-level unique constraints if they exist
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_username_key;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_google_id_key;

-- 2. Drop old non-unique or legacy indexes if they exist
DROP INDEX IF EXISTS idx_users_username;
DROP INDEX IF EXISTS idx_users_email;
DROP INDEX IF EXISTS idx_users_google_id;

-- 3. Create partial unique indexes on active (non-deleted) records
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_active_email ON users (LOWER(email)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_active_username ON users (LOWER(username)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_active_google_id ON users (google_id) WHERE deleted_at IS NULL AND google_id IS NOT NULL;
