-- 00_teardown_all.sql
-- Development Tear-Down / Rollback Script (Reverse Dependency Order)
-- WARNING: Executing this script drops all RLS policies, triggers, RPC functions, indexes, and tables!

-- ── 1. DROP RLS POLICIES ACROSS ALL TABLES ───────────────────────────────────
DROP POLICY IF EXISTS service_role_only ON storage_deletion_queue;
DROP POLICY IF EXISTS service_role_only ON deletion_audit_log;
DROP POLICY IF EXISTS service_role_only ON user_keys;

DROP POLICY IF EXISTS forget_password_update_policy ON forget_password;
DROP POLICY IF EXISTS forget_password_insert_policy ON forget_password;
DROP POLICY IF EXISTS forget_password_select_policy ON forget_password;
DROP POLICY IF EXISTS "Allow anon and authenticated to update forget_password" ON forget_password;
DROP POLICY IF EXISTS "Allow anon and authenticated to select forget_password" ON forget_password;
DROP POLICY IF EXISTS "Allow anon and authenticated to insert forget_password" ON forget_password;

DROP POLICY IF EXISTS chat_messages_insert_policy ON chat_messages;
DROP POLICY IF EXISTS chat_messages_select_policy ON chat_messages;
DROP POLICY IF EXISTS conversations_update_policy ON conversations;
DROP POLICY IF EXISTS conversations_insert_policy ON conversations;
DROP POLICY IF EXISTS conversations_select_policy ON conversations;
DROP POLICY IF EXISTS receipts_update_policy ON receipts;
DROP POLICY IF EXISTS receipts_insert_policy ON receipts;
DROP POLICY IF EXISTS receipts_select_policy ON receipts;
DROP POLICY IF EXISTS devices_update_policy ON devices;
DROP POLICY IF EXISTS devices_insert_policy ON devices;
DROP POLICY IF EXISTS devices_select_policy ON devices;
DROP POLICY IF EXISTS users_update_policy ON users;
DROP POLICY IF EXISTS users_insert_policy ON users;
DROP POLICY IF EXISTS users_select_policy ON users;

-- ── 2. DISABLE ROW LEVEL SECURITY ACROSS ALL TABLES ──────────────────────────
ALTER TABLE IF EXISTS storage_deletion_queue DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS deletion_audit_log DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS user_keys DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS forget_password DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS chat_messages DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS conversations DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS receipts DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS devices DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS users DISABLE ROW LEVEL SECURITY;

-- ── 3. DROP TRIGGERS ─────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS update_user_keys_updated_at ON user_keys;
DROP TRIGGER IF EXISTS update_conversations_updated_at ON conversations;
DROP TRIGGER IF EXISTS update_receipts_updated_at ON receipts;
DROP TRIGGER IF EXISTS update_devices_updated_at ON devices;
DROP TRIGGER IF EXISTS update_users_updated_at ON users;
DROP TRIGGER IF EXISTS chat_messages_update_conversation ON chat_messages;
DROP TRIGGER IF EXISTS check_conversation_cap ON conversations;

-- ── 4. DROP TRIGGER & RPC FUNCTIONS ──────────────────────────────────────────
DROP FUNCTION IF EXISTS link_device_and_migrate_guest_data(TEXT, TEXT, UUID);
DROP FUNCTION IF EXISTS soft_delete_user(UUID);
DROP FUNCTION IF EXISTS set_updated_at_column();
DROP FUNCTION IF EXISTS update_conversation_updated_at();
DROP FUNCTION IF EXISTS enforce_max_conversations();

-- ── 5. DROP INDEXES ──────────────────────────────────────────────────────────
DROP INDEX IF EXISTS idx_storage_del_q_status;
DROP INDEX IF EXISTS idx_storage_del_q_user;
DROP INDEX IF EXISTS idx_deletion_audit_log_email_hash;
DROP INDEX IF EXISTS idx_deletion_audit_log_user;
DROP INDEX IF EXISTS idx_users_email_verified;
DROP INDEX IF EXISTS idx_forget_password_token;
DROP INDEX IF EXISTS idx_forget_password_user;
DROP INDEX IF EXISTS idx_conversations_guest_migration;
DROP INDEX IF EXISTS idx_receipts_updated_at;
DROP INDEX IF EXISTS idx_receipts_guest_migration;
DROP INDEX IF EXISTS idx_chat_messages_conv;
DROP INDEX IF EXISTS idx_conversations_identity;
DROP INDEX IF EXISTS idx_receipts_identity;
DROP INDEX IF EXISTS idx_devices_user;
DROP INDEX IF EXISTS idx_devices_hardware;
DROP INDEX IF EXISTS idx_devices_trial_consumed;
DROP INDEX IF EXISTS idx_users_active_google_id;
DROP INDEX IF EXISTS idx_users_active_email;
DROP INDEX IF EXISTS idx_users_active_username;
DROP INDEX IF EXISTS idx_users_2fa_enabled;
DROP INDEX IF EXISTS idx_users_mobile_hash;
DROP INDEX IF EXISTS idx_users_mobile;
DROP INDEX IF EXISTS idx_users_google_id;
DROP INDEX IF EXISTS idx_users_email;
DROP INDEX IF EXISTS idx_users_username;

-- ── 6. DROP TABLES (IN REVERSE FOREIGN KEY DEPENDENCY ORDER) ─────────────────
DROP TABLE IF EXISTS storage_deletion_queue CASCADE;
DROP TABLE IF EXISTS deletion_audit_log CASCADE;
DROP TABLE IF EXISTS user_keys CASCADE;
DROP TABLE IF EXISTS forget_password CASCADE;
DROP TABLE IF EXISTS chat_messages CASCADE;
DROP TABLE IF EXISTS conversations CASCADE;
DROP TABLE IF EXISTS receipts CASCADE;
DROP TABLE IF EXISTS devices CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- ── 7. DROP CUSTOM TYPES ─────────────────────────────────────────────────────
DROP TYPE IF EXISTS user_tier CASCADE;
