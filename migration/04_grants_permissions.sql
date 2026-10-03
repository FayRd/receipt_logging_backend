-- 04_grants_permissions.sql
-- Configure schema privileges and table-level DML grants strictly for service_role
-- Anonymous role ('anon') is revoked from DML access to enforce backend gateway access control.

-- ── 1. GRANT USAGE ON PUBLIC SCHEMA ──────────────────────────────────────────
GRANT USAGE ON SCHEMA public TO service_role;

-- ── 2. GRANT TABLE-LEVEL DML PRIVILEGES STRICTLY TO service_role ─────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE users TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE devices TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE receipts TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversations TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE chat_messages TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE forget_password TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE user_keys TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE deletion_audit_log TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE storage_deletion_queue TO service_role;

-- ── 3. GRANT SEQUENCE PRIVILEGES ─────────────────────────────────────────────
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- ── 4. SET DEFAULT PRIVILEGES FOR FUTURE TABLES & SEQUENCES ──────────────────
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO service_role;
