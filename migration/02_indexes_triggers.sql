-- 02_indexes_triggers.sql
-- Idempotent performance indexes, trigger functions, and RPC helper functions

-- ── 1. PERFORMANCE INDEXES ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_users_username ON users (LOWER(username)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_users_email ON users (LOWER(email)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_users_email_verified ON users (email_verified_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_users_mobile ON users (mobile_number) WHERE deleted_at IS NULL AND mobile_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_devices_hardware ON devices (name) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices (user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_receipts_identity ON receipts (device_id, user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_receipts_guest_migration ON receipts (device_id) WHERE user_id IS NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_receipts_updated_at ON receipts (updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_identity ON conversations (device_id, user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_guest_migration ON conversations (device_id) WHERE user_id IS NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages (conversation_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_forget_password_user ON forget_password (user_id) WHERE is_used IS FALSE;
CREATE INDEX IF NOT EXISTS idx_forget_password_token ON forget_password (reset_token_hash) WHERE is_used IS FALSE;

-- ── 2. TRIGGER FUNCTION: AUTO-UPDATE updated_at COLUMN ──────────────────────
CREATE OR REPLACE FUNCTION set_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS update_users_updated_at ON users;
CREATE TRIGGER update_users_updated_at
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION set_updated_at_column();

DROP TRIGGER IF EXISTS update_devices_updated_at ON devices;
CREATE TRIGGER update_devices_updated_at
BEFORE UPDATE ON devices
FOR EACH ROW EXECUTE FUNCTION set_updated_at_column();

DROP TRIGGER IF EXISTS update_receipts_updated_at ON receipts;
CREATE TRIGGER update_receipts_updated_at
BEFORE UPDATE ON receipts
FOR EACH ROW EXECUTE FUNCTION set_updated_at_column();

DROP TRIGGER IF EXISTS update_conversations_updated_at ON conversations;
CREATE TRIGGER update_conversations_updated_at
BEFORE UPDATE ON conversations
FOR EACH ROW EXECUTE FUNCTION set_updated_at_column();

-- ── 3. TRIGGER FUNCTION: AUTO-UPDATE CONVERSATION updated_at ON NEW MESSAGE ──
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

-- ── 4. TRIGGER FUNCTION: ENFORCE 10 CONVERSATION CAP AT DATABASE LEVEL ──────
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

-- ── 5. RPC FUNCTION: ATOMIC DEVICE LINK & GUEST DATA MIGRATION ───────────────
CREATE OR REPLACE FUNCTION link_device_and_migrate_guest_data(
    p_device_name TEXT,
    p_device_token_hash TEXT,
    p_user_id UUID
)
RETURNS JSONB AS $$
DECLARE
    v_device_record RECORD;
    v_migrated_receipts INT := 0;
    v_migrated_convs INT := 0;
BEGIN
    -- 1. Verify device exists by name (or UUID id) & token hash matches
    SELECT * INTO v_device_record FROM devices 
    WHERE (name = p_device_name OR id::text = p_device_name) AND deleted_at IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Device not found.';
    END IF;

    IF v_device_record.device_token_hash != p_device_token_hash THEN
        RAISE EXCEPTION 'Invalid device token.';
    END IF;

    -- 2. Update device ownership
    UPDATE devices SET user_id = p_user_id WHERE id = v_device_record.id;

    -- 3. Migrate un-owned guest receipts for this device to the user account
    UPDATE receipts 
    SET user_id = p_user_id 
    WHERE (device_id = v_device_record.name OR device_id = p_device_name) AND user_id IS NULL AND deleted_at IS NULL;
    GET DIAGNOSTICS v_migrated_receipts = ROW_COUNT;

    -- 4. Migrate un-owned guest conversations for this device to the user account
    UPDATE conversations 
    SET user_id = p_user_id 
    WHERE (device_id = v_device_record.name OR device_id = p_device_name) AND user_id IS NULL AND deleted_at IS NULL;
    GET DIAGNOSTICS v_migrated_convs = ROW_COUNT;

    RETURN jsonb_build_object(
        'success', true,
        'migrated_receipts', v_migrated_receipts,
        'migrated_conversations', v_migrated_convs
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ── 6. RPC FUNCTION: SECURE USER SOFT-DELETE WITH SESSION UNLINKING ──────────
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
