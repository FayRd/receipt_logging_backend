-- 04_grants_permissions.sql
-- Configure schema privileges and table-level DML grants for service_role, authenticated, and anon

-- 1. Grant Usage on Public Schema
GRANT USAGE ON SCHEMA public TO service_role, authenticated, anon;

-- 2. Grant Table-Level DML Privileges across all 6 database tables
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE users TO service_role, authenticated, anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE devices TO service_role, authenticated, anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE receipts TO service_role, authenticated, anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversations TO service_role, authenticated, anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE chat_messages TO service_role, authenticated, anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE forget_password TO service_role, authenticated, anon;

-- 3. Grant Sequence Privileges
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role, authenticated, anon;

-- 4. Set Default Privileges for future tables in public schema
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO service_role, authenticated, anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO service_role, authenticated, anon;
