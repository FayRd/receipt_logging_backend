-- 04_grants_permissions.sql
-- Lock down public access: Revoke ALL privileges from anon, grant privileges to service_role and authenticated

-- 1. Revoke ALL public anon access
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon;
REVOKE ALL ON SCHEMA public FROM anon;

-- 2. Grant Usage on Public Schema to service_role and authenticated
GRANT USAGE ON SCHEMA public TO service_role, authenticated;

-- 3. Grant Table-Level DML Privileges strictly to service_role and authenticated
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE users TO service_role, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE devices TO service_role, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE receipts TO service_role, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversations TO service_role, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE chat_messages TO service_role, authenticated;

-- 4. Grant Sequence Privileges
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role, authenticated;

-- 5. Set Default Privileges for future tables in public schema
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO service_role, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO service_role, authenticated;
