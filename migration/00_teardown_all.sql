-- 00_teardown_all.sql
-- Development Tear-Down / Rollback Script (Reverse Dependency Order)
-- WARNING: Executing this script drops all RLS policies, triggers, RPC functions, indexes, and tables!

-- 1. Drop RLS Policies across all 6 tables
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

-- 2. Disable RLS across all 6 tables
ALTER TABLE IF EXISTS forget_password DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS chat_messages DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS conversations DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS receipts DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS devices DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS users DISABLE ROW LEVEL SECURITY;

-- 3. Drop Triggers
DROP TRIGGER IF EXISTS chat_messages_update_conversation ON chat_messages;
DROP TRIGGER IF EXISTS check_conversation_cap ON conversations;

-- 4. Drop Trigger & RPC Functions
DROP FUNCTION IF EXISTS link_device_and_migrate_guest_data(TEXT, TEXT, UUID);
DROP FUNCTION IF EXISTS update_conversation_updated_at();
DROP FUNCTION IF EXISTS enforce_max_conversations();
DROP FUNCTION IF EXISTS soft_delete_user(UUID);

-- 5. Drop Indexes
DROP INDEX IF EXISTS idx_forget_password_token;
DROP INDEX IF EXISTS idx_forget_password_user;
DROP INDEX IF EXISTS idx_conversations_guest_migration;
DROP INDEX IF EXISTS idx_receipts_guest_migration;
DROP INDEX IF EXISTS idx_chat_messages_conv;
DROP INDEX IF EXISTS idx_conversations_identity;
DROP INDEX IF EXISTS idx_receipts_identity;
DROP INDEX IF EXISTS idx_devices_user;
DROP INDEX IF EXISTS idx_devices_hardware;
DROP INDEX IF EXISTS idx_users_mobile;
DROP INDEX IF EXISTS idx_users_email;
DROP INDEX IF EXISTS idx_users_username;

-- 6. Drop Tables in Reverse Foreign Key Dependency Order
DROP TABLE IF EXISTS forget_password CASCADE;
DROP TABLE IF EXISTS chat_messages CASCADE;
DROP TABLE IF EXISTS conversations CASCADE;
DROP TABLE IF EXISTS receipts CASCADE;
DROP TABLE IF EXISTS devices CASCADE;
DROP TABLE IF EXISTS users CASCADE;
