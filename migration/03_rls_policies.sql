-- 03_rls_policies.sql
-- Enable Row Level Security (RLS) across all database tables and restrict to service_role
-- Anonymous public role ('anon') is strictly denied direct DML access to prevent PostgREST data dumping.

-- ── 0. ENABLE ROW LEVEL SECURITY ACROSS ALL TABLES ───────────────────────────
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE forget_password ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_deletion_queue ENABLE ROW LEVEL SECURITY;

-- ── 1. USERS POLICIES ────────────────────────────────────────────────────────
DROP POLICY IF EXISTS users_select_policy ON users;
CREATE POLICY users_select_policy ON users FOR SELECT TO service_role
USING (deleted_at IS NULL);

DROP POLICY IF EXISTS users_insert_policy ON users;
CREATE POLICY users_insert_policy ON users FOR INSERT TO service_role
WITH CHECK (TRUE);

DROP POLICY IF EXISTS users_update_policy ON users;
CREATE POLICY users_update_policy ON users FOR UPDATE TO service_role
USING (deleted_at IS NULL)
WITH CHECK (TRUE);

-- ── 2. DEVICES POLICIES ──────────────────────────────────────────────────────
DROP POLICY IF EXISTS devices_select_policy ON devices;
CREATE POLICY devices_select_policy ON devices FOR SELECT TO service_role
USING (deleted_at IS NULL);

DROP POLICY IF EXISTS devices_insert_policy ON devices;
CREATE POLICY devices_insert_policy ON devices FOR INSERT TO service_role
WITH CHECK (TRUE);

DROP POLICY IF EXISTS devices_update_policy ON devices;
CREATE POLICY devices_update_policy ON devices FOR UPDATE TO service_role
USING (deleted_at IS NULL)
WITH CHECK (TRUE);

-- ── 3. RECEIPTS POLICIES ─────────────────────────────────────────────────────
DROP POLICY IF EXISTS receipts_select_policy ON receipts;
CREATE POLICY receipts_select_policy ON receipts FOR SELECT TO service_role
USING (deleted_at IS NULL);

DROP POLICY IF EXISTS receipts_insert_policy ON receipts;
CREATE POLICY receipts_insert_policy ON receipts FOR INSERT TO service_role
WITH CHECK (TRUE);

DROP POLICY IF EXISTS receipts_update_policy ON receipts;
CREATE POLICY receipts_update_policy ON receipts FOR UPDATE TO service_role
USING (deleted_at IS NULL)
WITH CHECK (TRUE);

-- ── 4. CONVERSATIONS POLICIES ────────────────────────────────────────────────
DROP POLICY IF EXISTS conversations_select_policy ON conversations;
CREATE POLICY conversations_select_policy ON conversations FOR SELECT TO service_role
USING (deleted_at IS NULL);

DROP POLICY IF EXISTS conversations_insert_policy ON conversations;
CREATE POLICY conversations_insert_policy ON conversations FOR INSERT TO service_role
WITH CHECK (TRUE);

DROP POLICY IF EXISTS conversations_update_policy ON conversations;
CREATE POLICY conversations_update_policy ON conversations FOR UPDATE TO service_role
USING (deleted_at IS NULL)
WITH CHECK (TRUE);

-- ── 5. CHAT MESSAGES POLICIES ────────────────────────────────────────────────
DROP POLICY IF EXISTS chat_messages_select_policy ON chat_messages;
CREATE POLICY chat_messages_select_policy ON chat_messages FOR SELECT TO service_role
USING (TRUE);

DROP POLICY IF EXISTS chat_messages_insert_policy ON chat_messages;
CREATE POLICY chat_messages_insert_policy ON chat_messages FOR INSERT TO service_role
WITH CHECK (TRUE);

-- ── 6. FORGET PASSWORD POLICIES ──────────────────────────────────────────────
DROP POLICY IF EXISTS forget_password_select_policy ON forget_password;
CREATE POLICY forget_password_select_policy ON forget_password FOR SELECT TO service_role
USING (TRUE);

DROP POLICY IF EXISTS forget_password_insert_policy ON forget_password;
CREATE POLICY forget_password_insert_policy ON forget_password FOR INSERT TO service_role
WITH CHECK (TRUE);

DROP POLICY IF EXISTS forget_password_update_policy ON forget_password;
CREATE POLICY forget_password_update_policy ON forget_password FOR UPDATE TO service_role
USING (TRUE)
WITH CHECK (TRUE);

-- ── 7. PER-USER ENCRYPTION KEY (DEK) POLICIES ────────────────────────────────
-- Service role only — users cannot self-serve or read raw DEK envelopes via PostgREST
DROP POLICY IF EXISTS service_role_only ON user_keys;
CREATE POLICY service_role_only ON user_keys
    FOR ALL TO service_role
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- ── 8. COMPLIANCE AUDIT TRAIL POLICIES ───────────────────────────────────────
-- Strictly service role access only for compliance log auditing
DROP POLICY IF EXISTS service_role_only ON deletion_audit_log;
CREATE POLICY service_role_only ON deletion_audit_log
    FOR ALL TO service_role
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

-- ── 9. STORAGE DELETION QUEUE POLICIES ───────────────────────────────────────
-- Strictly service role access only for asynchronous storage purge queue
DROP POLICY IF EXISTS service_role_only ON storage_deletion_queue;
CREATE POLICY service_role_only ON storage_deletion_queue
    FOR ALL TO service_role
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
