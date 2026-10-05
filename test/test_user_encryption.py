#!/usr/bin/env python3
"""
Unit tests for user column encryption, blind indexing, and backup scrubbing.
Covers:
- encrypt_json with list payloads and roundtrip decryption.
- compute_mobile_hash consistency and normalization.
- _decrypt_user_row decrypting custom_categories, preferences, country_code, mobile_number with DEK and global key.
- get_by_email_or_mobile with blind index hash query.
- Deletion tombstoning and backup scrubbing zeroing out all 4 fields.
"""

import csv
import json
import os
import secrets
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.Infrastructure.crypto import (
    CryptoEngine,
    get_crypto_engine,
    encrypt_text_with_dek,
    decrypt_text_with_dek,
    encrypt_json_with_dek,
    decrypt_json_with_dek,
    compute_mobile_hash,
)
from src.Models.Users.user_repository import UserRepository
from src.Services.backup_scrubber import scrub_users_csv
from src.config import get_settings


# ── 1. TEST ENCRYPT_JSON WITH LIST PAYLOADS ───────────────────────────────────

def test_encrypt_json_with_list_payloads():
    """Verify CryptoEngine.encrypt_json supports list payloads and roundtrips accurately."""
    crypto = get_crypto_engine()
    categories = ["groceries", "restaurants", "transport", "utilities"]

    # Encrypt list
    encrypted = crypto.encrypt_json(categories)
    assert isinstance(encrypted, dict)
    assert encrypted.get("_enc") in ("v1", "v2")
    assert "iv" in encrypted
    assert "tag" in encrypted
    assert "data" in encrypted

    # Decrypt list
    decrypted = crypto.decrypt_json(encrypted)
    assert decrypted == categories

    # Idempotency check: encrypting already-encrypted envelope returns as-is
    double_encrypted = crypto.encrypt_json(encrypted)
    assert double_encrypted == encrypted

    # Empty list
    empty_enc = crypto.encrypt_json([])
    assert isinstance(empty_enc, dict)
    assert crypto.decrypt_json(empty_enc) == []

    # Complex list of dicts
    complex_list = [
        {"id": 1, "name": "Food & Dining", "icon": "food"},
        {"id": 2, "name": "Healthcare", "icon": "health"},
    ]
    complex_enc = crypto.encrypt_json(complex_list)
    assert crypto.decrypt_json(complex_enc) == complex_list


def test_encrypt_json_with_dek_list_payloads():
    """Verify encrypt_json_with_dek supports list payloads."""
    dek = secrets.token_bytes(32)
    categories = ["shopping", "travel", "entertainment"]

    encrypted = encrypt_json_with_dek(categories, dek)
    assert isinstance(encrypted, dict)
    assert encrypted.get("_enc") in ("v1", "v2")

    decrypted = decrypt_json_with_dek(encrypted, dek)
    assert decrypted == categories


# ── 2. TEST COMPUTE_MOBILE_HASH CONSISTENCY ───────────────────────────────────

def test_compute_mobile_hash_consistency():
    """Verify compute_mobile_hash produces consistent normalized hashes."""
    key = "test_data_encryption_key_32_bytes_len!!"

    # Format variations of the same phone number must yield the exact same hash
    h1 = compute_mobile_hash("+1 (555) 123-4567", key)
    h2 = compute_mobile_hash("15551234567", key)
    h3 = compute_mobile_hash("  +1-555-123-4567  ", key)
    assert h1 is not None
    assert h1 == h2 == h3

    # International phone number
    h_my1 = compute_mobile_hash("+60 12-345 6789", key)
    h_my2 = compute_mobile_hash("60123456789", key)
    assert h_my1 == h_my2

    # Different numbers must yield different hashes
    h_diff = compute_mobile_hash("+1 555 987-6543", key)
    assert h1 != h_diff

    # Key as bytes vs string equivalence
    key_bytes = key.encode("utf-8")
    assert compute_mobile_hash("+1 555 123-4567", key) == compute_mobile_hash("+1 555 123-4567", key_bytes)

    # Empty and None handling
    assert compute_mobile_hash(None, key) is None
    assert compute_mobile_hash("", key) is None
    assert compute_mobile_hash("   ", key) is None
    assert compute_mobile_hash("no_digits_here", key) is None


# ── 3. TEST _DECRYPT_USER_ROW WITH DEK AND GLOBAL KEY ─────────────────────────

@pytest.mark.anyio
async def test_decrypt_user_row_with_dek():
    """Test _decrypt_user_row decrypts all 4 fields using per-user DEK when enc_version=1."""
    mock_db = AsyncMock()
    repo = UserRepository(mock_db)

    user_id = "test-user-dek-1234"
    dek = secrets.token_bytes(32)

    # Mock KeyVault to return user's DEK
    mock_kv = MagicMock()
    mock_kv.get_user_dek_or_none = AsyncMock(return_value=dek)
    repo.key_vault = mock_kv

    raw_cats = ["food", "groceries", "gas"]
    raw_prefs = {"currency": "MYR", "theme": "dark", "notifications": True}
    raw_cc = "+60"
    raw_mob = "123456789"

    # Encrypted fields
    enc_cats = encrypt_json_with_dek(raw_cats, dek)
    enc_prefs = encrypt_json_with_dek(raw_prefs, dek)
    enc_cc = encrypt_text_with_dek(raw_cc, dek)
    enc_mob = encrypt_text_with_dek(raw_mob, dek)

    encrypted_row = {
        "id": user_id,
        "username": "alice",
        "email": "alice@example.com",
        "country_code": enc_cc,
        "mobile_number": enc_mob,
        "mobile_hash": "some_blind_index_hash",
        "custom_categories": enc_cats,
        "preferences": enc_prefs,
        "enc_version": 1,
    }

    decrypted = await repo._decrypt_user_row(encrypted_row)
    assert decrypted is not None
    assert decrypted["custom_categories"] == raw_cats
    assert decrypted["preferences"] == raw_prefs
    assert decrypted["country_code"] == raw_cc
    assert decrypted["mobile_number"] == raw_mob
    # mobile_hash should not be leaked in safe decrypted view
    assert "mobile_hash" not in decrypted


@pytest.mark.anyio
async def test_decrypt_user_row_with_global_key():
    """Test _decrypt_user_row decrypts all 4 fields using global key when enc_version=0."""
    mock_db = AsyncMock()
    repo = UserRepository(mock_db)
    crypto = repo.crypto

    user_id = "test-user-global-5678"
    mock_kv = MagicMock()
    mock_kv.get_user_dek_or_none = AsyncMock(return_value=None)
    repo.key_vault = mock_kv

    raw_cats = ["travel", "hotel"]
    raw_prefs = {"currency": "USD", "is_in_trial": False}
    raw_cc = "+1"
    raw_mob = "5559876543"

    enc_cats = crypto.encrypt_json(raw_cats)
    enc_prefs = crypto.encrypt_json(raw_prefs)
    enc_cc = crypto.encrypt_text(raw_cc)
    enc_mob = crypto.encrypt_text(raw_mob)

    encrypted_row = {
        "id": user_id,
        "username": "bob",
        "email": "bob@example.com",
        "country_code": enc_cc,
        "mobile_number": enc_mob,
        "mobile_hash": "another_hash",
        "custom_categories": enc_cats,
        "preferences": enc_prefs,
        "enc_version": 0,
    }

    decrypted = await repo._decrypt_user_row(encrypted_row)
    assert decrypted is not None
    assert decrypted["custom_categories"] == raw_cats
    assert decrypted["preferences"] == raw_prefs
    assert decrypted["country_code"] == raw_cc
    assert decrypted["mobile_number"] == raw_mob
    assert "mobile_hash" not in decrypted


@pytest.mark.anyio
async def test_decrypt_user_row_plaintext_passthrough():
    mock_db = AsyncMock()
    repo = UserRepository(mock_db)
    mock_kv = MagicMock()
    mock_kv.get_user_dek_or_none = AsyncMock(return_value=None)
    repo.key_vault = mock_kv

    plain_row = {
        "id": "legacy-plain-user",
        "username": "charlie",
        "email": "charlie@example.com",
        "country_code": "+44",
        "mobile_number": "7700900077",
        "custom_categories": ["books", "games"],
        "preferences": {"theme": "light"},
        "enc_version": 0,
    }

    decrypted = await repo._decrypt_user_row(plain_row)
    assert decrypted is not None
    assert decrypted["custom_categories"] == ["books", "games"]
    assert decrypted["preferences"] == {"theme": "light"}
    assert decrypted["country_code"] == "+44"
    assert decrypted["mobile_number"] == "7700900077"


# ── 4. TEST GET_BY_EMAIL_OR_MOBILE WITH BLIND INDEX HASH ─────────────────────

@pytest.mark.anyio
async def test_get_by_email_or_mobile_with_blind_index_hash():
    """Verify get_by_email_or_mobile computes mobile_hash and queries Supabase by it."""
    mock_db = MagicMock()
    repo = UserRepository(mock_db)

    settings = get_settings()
    phone_query = "+1 (555) 333-4444"
    expected_hash = compute_mobile_hash(phone_query, settings.data_encryption_key)

    # Mock get_by_identifier to return None so it falls through to mobile lookup
    repo.get_by_identifier = AsyncMock(return_value=None)

    # Setup mock query chain
    mock_table = MagicMock()
    mock_db.table.return_value = mock_table
    mock_select = MagicMock()
    mock_table.select.return_value = mock_select
    mock_eq = MagicMock()
    mock_select.eq.return_value = mock_eq
    mock_is = MagicMock()
    mock_eq.is_.return_value = mock_is
    mock_maybe_single = MagicMock()
    mock_is.maybe_single.return_value = mock_maybe_single

    user_record = {
        "id": "user-mobile-match",
        "username": "david",
        "email": "david@example.com",
        "country_code": "+1",
        "mobile_number": "5553334444",
        "mobile_hash": expected_hash,
        "custom_categories": [],
        "preferences": {},
        "enc_version": 0,
    }
    mock_res = MagicMock()
    mock_res.data = user_record
    mock_maybe_single.execute = AsyncMock(return_value=mock_res)

    result = await repo.get_by_email_or_mobile(phone_query)

    # Verify query searched by mobile_hash
    mock_select.eq.assert_any_call("mobile_hash", expected_hash)
    assert result is not None
    assert result["id"] == "user-mobile-match"
    assert result["mobile_number"] == "5553334444"


# ── 5. TEST DELETION TOMBSTONING AND BACKUP SCRUBBING ────────────────────────

def test_backup_scrubber_zeroes_all_4_fields():
    """Verify scrub_users_csv wipes country_code, mobile_number, mobile_hash, custom_categories, and preferences."""
    tmp_root = tempfile.mkdtemp(prefix="test_scrubber_")
    try:
        backup_dir = os.path.join(tmp_root, "backup_20261005_120000")
        os.makedirs(backup_dir, exist_ok=True)

        users_csv = os.path.join(backup_dir, "users.csv")
        manifest_json = os.path.join(backup_dir, "manifest.json")

        target_uid = "deleted-user-uuid-9999"
        other_uid = "active-user-uuid-1111"

        fieldnames = [
            "id", "username", "email", "country_code", "mobile_number",
            "mobile_hash", "custom_categories", "preferences", "avatar_image_path", "deleted_at"
        ]

        rows = [
            {
                "id": target_uid,
                "username": "deleted_user",
                "email": "target@example.com",
                "country_code": "+1",
                "mobile_number": "5550001",
                "mobile_hash": "hash_abc_123",
                "custom_categories": json.dumps(["food", "transport"]),
                "preferences": json.dumps({"currency": "USD"}),
                "avatar_image_path": f"{target_uid}/avatar.png",
                "deleted_at": "",
            },
            {
                "id": other_uid,
                "username": "kept_user",
                "email": "kept@example.com",
                "country_code": "+44",
                "mobile_number": "7700111",
                "mobile_hash": "hash_def_456",
                "custom_categories": json.dumps(["books"]),
                "preferences": json.dumps({"currency": "GBP"}),
                "avatar_image_path": f"{other_uid}/avatar.png",
                "deleted_at": "",
            },
        ]

        with open(users_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        # Create basic manifest
        with open(manifest_json, "w", encoding="utf-8") as f:
            json.dump({"tables": {"users": {"row_count": 2}}}, f)

        # Execute scrubber
        scrubbed_count = scrub_users_csv(target_uid, backup_root=tmp_root)
        assert scrubbed_count == 1

        # Verify target user row was scrubbed
        with open(users_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            updated_rows = list(reader)

        target_row = next(r for r in updated_rows if r["id"] == target_uid)
        assert target_row["country_code"] == ""
        assert target_row["mobile_number"] == ""
        assert target_row["mobile_hash"] == ""
        assert target_row["custom_categories"] == "[]"
        assert target_row["preferences"] == "{}"
        assert target_row["avatar_image_path"] == ""
        assert target_row["deleted_at"] != ""
        assert target_row["email"] == f"deleted_{target_uid}@deleted.local"

        # Verify other user row was left untouched
        kept_row = next(r for r in updated_rows if r["id"] == other_uid)
        assert kept_row["country_code"] == "+44"
        assert kept_row["mobile_number"] == "7700111"
        assert kept_row["mobile_hash"] == "hash_def_456"
        assert kept_row["custom_categories"] == json.dumps(["books"])
        assert kept_row["preferences"] == json.dumps({"currency": "GBP"})

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
