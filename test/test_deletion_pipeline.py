import asyncio
import hashlib
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.Infrastructure.database import get_supabase_client, close_supabase_client
from src.Infrastructure.key_vault import key_vault
from src.Infrastructure.redis_service import store_otp
from src.Services.storage_scrubber import scrub_pending_storage_deletions
from src.config import get_settings


def test_full_account_deletion_crypto_shredding_pipeline(client, mock_user_session):
    """Integration test verifying the end-to-end crypto-shredding deletion pipeline:

    1. User created + device linked.
    2. DEK provisioned in user_keys.
    3. Legacy enc_version=0 records and enc_version=1 records inserted.
    4. DELETE /api/v1/user/me called.
    5. User DEK destroyed in user_keys.
    6. enc_version=0 records hard-deleted; enc_version=1 records preserved (ciphertext noise).
    7. Storage deletion queue populated.
    8. Device tombstoned (Option B: hashed device ID, user_id=None).
    9. Identity tombstoned (deleted_at set, deleted_uid@deleted.local).
    10. deletion_audit_log updated with status='complete' and row counts.
    """
    settings = get_settings()
    user_id = mock_user_session["user_id"]
    headers = mock_user_session["headers"]
    device_id = mock_user_session["device_id"]
    email = mock_user_session["email"]

    # 1. Setup DEK and test records in DB
    async def _setup_data():
        await close_supabase_client()
        db = await get_supabase_client()

        # Provision DEK
        dek = await key_vault.provision_user_dek(user_id, db)
        assert dek is not None
        assert len(dek) == 32

        retrieved_dek = await key_vault.get_user_dek(user_id, db)
        assert retrieved_dek == dek

        # Set initial preferences with trial_device_id and non-PII preference
        await db.table("users").update({
            "preferences": {"trial_device_id": device_id, "theme": "dark"}
        }).eq("id", user_id).execute()

        # Mark device as having consumed trial
        from datetime import datetime, timezone
        await db.table("devices").update({
            "trial_consumed_at": datetime.now(timezone.utc).isoformat()
        }).eq("name", device_id).execute()

        # Insert legacy enc_version=0 receipt
        r0 = await db.table("receipts").insert({
            "user_id": user_id,
            "device_id": device_id,
            "receipt": {"merchant": "Legacy Store", "total": 12.50},
            "enc_version": 0,
        }).execute()
        assert r0.data

        # Insert enc_version=1 receipt
        r1 = await db.table("receipts").insert({
            "user_id": user_id,
            "device_id": device_id,
            "receipt": {"_enc": "v1", "cipher": "mock_encrypted_payload"},
            "enc_version": 1,
        }).execute()
        assert r1.data
        r1_id = r1.data[0]["id"]

        # Insert legacy enc_version=0 conversation
        c0 = await db.table("conversations").insert({
            "user_id": user_id,
            "device_id": device_id,
            "title": "Legacy Conversation",
            "enc_version": 0,
        }).execute()
        assert c0.data

        await close_supabase_client()
        return r1_id

    rec_v1_id = asyncio.run(_setup_data())

    # 2. Execute account deletion via API
    response = client.delete("/api/v1/user/me", headers=headers)
    assert response.status_code == 200
    res_json = response.json()
    assert res_json["success"] is True
    assert "cryptographically shredded" in res_json["message"]

    # 3. Verify DB state after deletion
    async def _verify_deletion():
        await close_supabase_client()
        db = await get_supabase_client()

        # DEK destroyed
        dek_after = await key_vault.get_user_dek_or_none(user_id, db)
        assert dek_after is None
        uk_check = await db.table("user_keys").select("*").eq("user_id", user_id).execute()
        assert len(uk_check.data or []) == 0

        # Legacy enc_version=0 records hard-deleted
        v0_receipts = await db.table("receipts").select("*").eq("user_id", user_id).eq("enc_version", 0).execute()
        assert len(v0_receipts.data or []) == 0

        v0_convs = await db.table("conversations").select("*").eq("user_id", user_id).eq("enc_version", 0).execute()
        assert len(v0_convs.data or []) == 0

        # enc_version=1 records preserved (shredded ciphertext noise)
        v1_receipts = await db.table("receipts").select("*").eq("id", rec_v1_id).execute()
        assert len(v1_receipts.data or []) == 1
        await db.table("receipts").delete().eq("id", rec_v1_id).execute()

        # Storage deletion queue entry created
        queue_res = await db.table("storage_deletion_queue").select("*").eq("user_id", user_id).execute()
        assert len(queue_res.data or []) >= 1
        assert queue_res.data[0]["storage_prefix"] == f"{user_id}/"

        # Device tombstone (Option B)
        expected_hash = hashlib.sha256((device_id + settings.data_encryption_key).encode("utf-8")).hexdigest()
        dev_res = await db.table("devices").select("*").eq("name", expected_hash).execute()
        assert len(dev_res.data or []) == 1
        assert dev_res.data[0]["user_id"] is None
        assert dev_res.data[0]["trial_consumed_at"] is not None

        # Verify check_device_trial_used identifies tombstoned device
        from src.Models.Users.user_repository import UserRepository
        repo = UserRepository(db)
        is_used = await repo.check_device_trial_used(device_id)
        assert is_used is True, "Tombstoned device must be identified as trial consumed via hashed device name"

        # User identity tombstone
        user_res = await db.table("users").select("*").eq("id", user_id).execute()
        assert len(user_res.data or []) == 1
        u = user_res.data[0]
        assert u["email"] == f"deleted_{user_id}@deleted.local"
        assert u["username"] == f"deleted_{user_id}"
        assert u["country_code"] is None
        assert u["mobile_number"] is None
        assert u["deleted_at"] is not None
        user_prefs = u.get("preferences") or {}
        assert user_prefs == {}, "All preferences wiped on deletion"
        assert u.get("custom_categories") in ([], None), "Custom categories wiped on deletion"

        # Audit log verification
        audit_res = await db.table("deletion_audit_log").select("*").eq("user_id", user_id).execute()
        assert len(audit_res.data or []) >= 1
        a = audit_res.data[0]
        assert a["status"] == "complete"
        assert a["email_hash"] == hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
        assert a["dek_destroyed_at"] is not None
        assert a["rows_hard_deleted"] >= 2
        assert a["rows_crypto_shredded"] >= 1

        await close_supabase_client()

    asyncio.run(_verify_deletion())


def test_account_deletion_2fa_verification(client, mock_user_session):
    """Test that if 2FA is enabled, DELETE /api/v1/user/me enforces X-2FA-OTP header."""
    user_id = mock_user_session["user_id"]
    email = mock_user_session["email"]
    headers = dict(mock_user_session["headers"])

    async def _enable_2fa():
        await close_supabase_client()
        db = await get_supabase_client()
        await db.table("users").update({"is_2fa_enabled": True}).eq("id", user_id).execute()
        await close_supabase_client()

    asyncio.run(_enable_2fa())

    # 1. Call delete without 2FA header -> HTTP 403 Forbidden
    res_no_2fa = client.delete("/api/v1/user/me", headers=headers)
    assert res_no_2fa.status_code == 403
    assert res_no_2fa.headers.get("X-2FA-Required") == "true"

    # 2. Call delete with invalid 2FA OTP -> HTTP 400
    headers_bad_otp = dict(headers)
    headers_bad_otp["X-2FA-OTP"] = "000000"
    res_bad_otp = client.delete("/api/v1/user/me", headers=headers_bad_otp)
    assert res_bad_otp.status_code == 400

    # 3. Store valid 2FA action OTP in Redis
    valid_otp = "852963"
    store_otp(user_id, "2fa_action", email, valid_otp)

    # 4. Call delete with valid 2FA OTP -> HTTP 200 Success
    headers_good_otp = dict(headers)
    headers_good_otp["X-2FA-OTP"] = valid_otp
    res_good_otp = client.delete("/api/v1/user/me", headers=headers_good_otp)
    assert res_good_otp.status_code == 200
    assert res_good_otp.json()["success"] is True


@pytest.mark.anyio
async def test_storage_scrubber_processing_and_completion():
    """Test that storage_scrubber processes queue items, lists objects, calls remove, and completes."""
    fake_user_id = str(uuid.uuid4())
    mock_db = MagicMock()

    queue_row = {
        "id": 999,
        "user_id": fake_user_id,
        "storage_prefix": f"{fake_user_id}/",
        "attempt_count": 0,
        "status": "pending",
    }

    mock_table = MagicMock()
    mock_select = MagicMock()
    mock_in = MagicMock()
    mock_lt = MagicMock()
    mock_update = MagicMock()
    mock_eq = MagicMock()

    mock_db.table.return_value = mock_table
    mock_table.select.return_value = mock_select
    mock_select.in_.return_value = mock_in
    mock_in.lt.return_value = mock_lt

    select_exec_res = MagicMock()
    select_exec_res.data = [queue_row]
    mock_lt.execute = AsyncMock(return_value=select_exec_res)

    mock_table.update.return_value = mock_update
    mock_update.eq.return_value = mock_eq
    mock_eq.execute = AsyncMock(return_value=MagicMock(data=[]))

    mock_bucket = MagicMock()
    mock_db.storage.from_.return_value = mock_bucket

    async def mock_list(path=""):
        if path == fake_user_id:
            return [
                {"name": "avatar_images", "id": None, "metadata": None},
                {"name": "receipt_images", "id": None, "metadata": None},
            ]
        elif path == f"{fake_user_id}/avatar_images":
            return [{"name": "medium.jpg", "id": "file-1", "metadata": {"size": 100}}]
        elif path == f"{fake_user_id}/receipt_images":
            return [{"name": "rec_001.jpg", "id": "file-2", "metadata": {"size": 200}}]
        return []

    mock_bucket.list = AsyncMock(side_effect=mock_list)
    mock_bucket.remove = AsyncMock(return_value=[{"name": "medium.jpg"}, {"name": "rec_001.jpg"}])

    completed = await scrub_pending_storage_deletions(mock_db, bucket_name="user-data")
    assert completed == 1

    mock_bucket.remove.assert_awaited_once()
    deleted_paths = mock_bucket.remove.call_args[0][0]
    assert f"{fake_user_id}/avatar_images/medium.jpg" in deleted_paths
    assert f"{fake_user_id}/receipt_images/rec_001.jpg" in deleted_paths


@pytest.mark.anyio
async def test_storage_scrubber_marks_failed_after_5_attempts():
    """Test that storage scrubber increments attempt_count and sets status to 'failed' on 5th attempt."""
    fake_user_id = str(uuid.uuid4())
    mock_db = MagicMock()

    queue_row = {
        "id": 888,
        "user_id": fake_user_id,
        "storage_prefix": f"{fake_user_id}/",
        "attempt_count": 4,  # Next attempt will be 5
        "status": "pending",
    }

    mock_table = MagicMock()
    mock_select = MagicMock()
    mock_in = MagicMock()
    mock_lt = MagicMock()
    mock_update = MagicMock()
    mock_eq = MagicMock()

    mock_db.table.return_value = mock_table
    mock_table.select.return_value = mock_select
    mock_select.in_.return_value = mock_in
    mock_in.lt.return_value = mock_lt

    select_exec_res = MagicMock()
    select_exec_res.data = [queue_row]
    mock_lt.execute = AsyncMock(return_value=select_exec_res)

    mock_table.update.return_value = mock_update
    mock_update.eq.return_value = mock_eq
    mock_eq.execute = AsyncMock(return_value=MagicMock(data=[]))

    mock_bucket = MagicMock()
    mock_db.storage.from_.return_value = mock_bucket

    mock_bucket.list = AsyncMock(side_effect=RuntimeError("Storage connection failed"))

    completed = await scrub_pending_storage_deletions(mock_db, bucket_name="user-data")
    assert completed == 0

    update_calls = [c[0][0] for c in mock_table.update.call_args_list]
    assert any(c.get("status") == "failed" for c in update_calls)
