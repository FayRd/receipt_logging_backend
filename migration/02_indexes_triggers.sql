-- 02_indexes_triggers.sql
-- Idempotent performance indexes, trigger functions, and RPC helper functions

-- ── 1. PERFORMANCE INDEXES ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_users_username ON users (LOWER(username)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_users_email ON users (LOWER(email)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_users_mobile ON users (mobile_number) WHERE deleted_at IS NULL AND mobile_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_devices_hardware ON devices (device_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices (user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_receipts_identity ON receipts (device_id, user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_identity ON conversations (device_id, user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages (conversation_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_forget_password_user ON forget_password (user_id) WHERE is_used IS FALSE;
CREATE INDEX IF NOT EXISTS idx_forget_password_token ON forget_password (reset_token_hash) WHERE is_used IS FALSE;

-- ── 2. TRIGGER FUNCTION: AUTO-UPDATE CONVERSATION UPDATED_AT ────────────────
CREATE OR REPLACE FUNCTION update_conversation_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  UPDATE conversations SET updated_at = NOW() WHERE id = NEW.conversation_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS chat_messages_update_conversation ON chat_messages;
CREATE TRIGGER chat_messages_update_conversation
AFTER INSERT ON chat_messages
FOR EACH ROW EXECUTE FUNCTION update_conversation_updated_at();

-- ── 3. TRIGGER FUNCTION: ENFORCE 10 CONVERSATION CAP AT DATABASE LEVEL ──────
CREATE OR REPLACE FUNCTION enforce_max_conversations()
RETURNS TRIGGER AS $$
DECLARE
  v_count INT;
BEGIN
  IF NEW.user_id IS NOT NULL THEN
    SELECT COUNT(*) INTO v_count 
    FROM conversations 
    WHERE user_id = NEW.user_id AND deleted_at IS NULL;
  ELSE
    SELECT COUNT(*) INTO v_count 
    FROM conversations 
    WHERE device_id = NEW.device_id AND user_id IS NULL AND deleted_at IS NULL;
  END IF;

  IF v_count >= 10 THEN
    RAISE EXCEPTION 'Maximum limit of 10 conversations reached for this user/device identity.';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS check_conversation_cap ON conversations;
CREATE TRIGGER check_conversation_cap
BEFORE INSERT ON conversations
FOR EACH ROW EXECUTE FUNCTION enforce_max_conversations();

-- ── 4. RPC FUNCTION: SECURE USER SOFT-DELETE WITH SESSION UNLINKING ──────────
CREATE OR REPLACE FUNCTION soft_delete_user(target_user_id UUID)
RETURNS BOOLEAN AS $$
DECLARE
  v_affected INT;
BEGIN
  UPDATE users 
  SET deleted_at = NOW() 
  WHERE id = target_user_id AND deleted_at IS NULL;
  
  GET DIAGNOSTICS v_affected = ROW_COUNT;

  IF v_affected > 0 THEN
    -- Terminate active sessions by clearing user_id from devices
    UPDATE devices SET user_id = NULL WHERE user_id = target_user_id;
    RETURN TRUE;
  END IF;
  
  RETURN FALSE;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ── 5. FORGET PASSWORD RLS POLICIES & PERMISSIONS ────────────────────────────
ALTER TABLE forget_password ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE ON TABLE forget_password TO anon, authenticated, service_role;

DROP POLICY IF EXISTS "Allow anon and authenticated to insert forget_password" ON forget_password;
CREATE POLICY "Allow anon and authenticated to insert forget_password"
ON forget_password
FOR INSERT
TO anon, authenticated, service_role
WITH CHECK (true);

DROP POLICY IF EXISTS "Allow anon and authenticated to select forget_password" ON forget_password;
CREATE POLICY "Allow anon and authenticated to select forget_password"
ON forget_password
FOR SELECT
TO anon, authenticated, service_role
USING (true);

DROP POLICY IF EXISTS "Allow anon and authenticated to update forget_password" ON forget_password;
CREATE POLICY "Allow anon and authenticated to update forget_password"
ON forget_password
FOR UPDATE
TO anon, authenticated, service_role
USING (true)
WITH CHECK (true);
