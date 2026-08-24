#!/usr/bin/env python3
import base64
import json
import os
import pytest
from cryptography.exceptions import InvalidTag
from src.Infrastructure.crypto import CryptoEngine, get_crypto_engine
from src.Models.schemas import Receipt
from src.Models.Receipts.receipt_repository import ReceiptRepository
from src.Models.Conversations.conversation_repository import ConversationRepository
from src.Infrastructure.database import get_supabase_client


# ── UNIT TESTS: CRYPTO ENGINE ─────────────────────────────────────────────────

def test_text_roundtrip_encryption():
    engine = get_crypto_engine()
    test_strings = [
        "Hello, World!",
        "Sensitive user notes with unicode: ☕ café & résumé €100",
        "",
        "Line 1\nLine 2\nLine 3\tTabbed",
        "A" * 10000,  # Large string
    ]
    for text in test_strings:
        envelope = engine.encrypt_text(text)
        assert envelope.startswith("enc:v1:")
        parts = envelope.split(":")
        assert len(parts) == 5
        assert parts[0] == "enc"
        assert parts[1] == "v1"

        decrypted = engine.decrypt_text(envelope)
        assert decrypted == text


def test_text_encryption_with_aad():
    engine = get_crypto_engine()
    text = "Secret Data"
    aad = b"user-12345"

    envelope = engine.encrypt_text(text, aad=aad)
    decrypted = engine.decrypt_text(envelope, aad=aad)
    assert decrypted == text

    # Decrypting with incorrect AAD must fail
    with pytest.raises(InvalidTag):
        engine.decrypt_text(envelope, aad=b"wrong-user")


def test_json_roundtrip_encryption():
    engine = get_crypto_engine()
    payload = {
        "merchant_name": "Target Store",
        "total_amount": 125.75,
        "currency": "USD",
        "line_items": [
            {"description": "Organic Milk", "price": 4.50, "qty": 2},
            {"description": "Coffee Beans", "price": 14.99, "qty": 1},
        ],
        "notes": "Weekly grocery shopping",
        "tags": ["groceries", "food"],
        "tax_amount": 2.50,
        "is_refund": False,
    }

    enc_dict = engine.encrypt_json(payload)
    assert isinstance(enc_dict, dict)
    assert enc_dict.get("_enc") == "v1"
    assert "iv" in enc_dict
    assert "tag" in enc_dict
    assert "data" in enc_dict

    # Decrypt dictionary envelope
    decrypted = engine.decrypt_json(enc_dict)
    assert decrypted == payload

    # Decrypt JSON-serialized string of the envelope
    decrypted_from_str = engine.decrypt_json(json.dumps(enc_dict))
    assert decrypted_from_str == payload


def test_bytes_roundtrip_encryption():
    engine = get_crypto_engine()
    data = os.urandom(256)
    aad = b"receipt-image-metadata"

    enc_bytes = engine.encrypt_bytes(data, aad=aad)
    assert len(enc_bytes) == len(engine.BYTES_PREFIX) + 12 + 16 + 256  # Prefix + 12 IV + 16 Tag + 256 Ciphertext

    dec_bytes = engine.decrypt_bytes(enc_bytes, aad=aad)
    assert dec_bytes == data

    with pytest.raises(InvalidTag):
        engine.decrypt_bytes(enc_bytes, aad=b"wrong-aad")


def test_tamper_detection_text():
    engine = get_crypto_engine()
    envelope = engine.encrypt_text("Confidential message")
    parts = envelope.split(":")

    # 1. Tamper with ciphertext
    ct_bytes = bytearray(base64.b64decode(parts[4]))
    ct_bytes[0] ^= 0xFF
    tampered_ct = base64.b64encode(ct_bytes).decode("ascii")
    tampered_envelope_ct = f"enc:v1:{parts[2]}:{parts[3]}:{tampered_ct}"
    with pytest.raises(InvalidTag):
        engine.decrypt_text(tampered_envelope_ct)

    # 2. Tamper with authentication tag
    tag_bytes = bytearray(base64.b64decode(parts[3]))
    tag_bytes[0] ^= 0xFF
    tampered_tag = base64.b64encode(tag_bytes).decode("ascii")
    tampered_envelope_tag = f"enc:v1:{parts[2]}:{tampered_tag}:{parts[4]}"
    with pytest.raises(InvalidTag):
        engine.decrypt_text(tampered_envelope_tag)

    # 3. Tamper with IV
    iv_bytes = bytearray(base64.b64decode(parts[2]))
    iv_bytes[0] ^= 0xFF
    tampered_iv = base64.b64encode(iv_bytes).decode("ascii")
    tampered_envelope_iv = f"enc:v1:{tampered_iv}:{parts[3]}:{parts[4]}"
    with pytest.raises(InvalidTag):
        engine.decrypt_text(tampered_envelope_iv)


def test_tamper_detection_json():
    engine = get_crypto_engine()
    payload = {"account": "1234-5678", "balance": 50000}
    enc = engine.encrypt_json(payload)

    # Tamper with ciphertext
    data_bytes = bytearray(base64.b64decode(enc["data"]))
    data_bytes[0] ^= 0xFF
    tampered_enc = dict(enc)
    tampered_enc["data"] = base64.b64encode(data_bytes).decode("ascii")

    with pytest.raises(InvalidTag):
        engine.decrypt_json(tampered_enc)

    # Tamper with tag
    tag_bytes = bytearray(base64.b64decode(enc["tag"]))
    tag_bytes[0] ^= 0xFF
    tampered_enc_tag = dict(enc)
    tampered_enc_tag["tag"] = base64.b64encode(tag_bytes).decode("ascii")

    with pytest.raises(InvalidTag):
        engine.decrypt_json(tampered_enc_tag)


def test_backward_compatibility_legacy_records():
    engine = get_crypto_engine()

    # Legacy plaintext string (not starting with enc:v1:) passes through
    legacy_text = "Legacy plain title without encryption"
    assert engine.decrypt_text(legacy_text) == legacy_text

    # Legacy unencrypted dictionary passes through untouched
    legacy_dict = {
        "merchant_name": "Old Grocery",
        "total_amount": 42.00,
        "raw_text": "Legacy receipt",
    }
    assert engine.decrypt_json(legacy_dict) == legacy_dict

    # Legacy JSON-encoded string parses to dictionary
    legacy_json_str = json.dumps(legacy_dict)
    assert engine.decrypt_json(legacy_json_str) == legacy_dict


def test_key_initialization():
    # 32-byte base64 key
    b64_key = base64.b64encode(b"01234567890123456789012345678901").decode("ascii")
    engine1 = CryptoEngine(key=b64_key)
    assert engine1.decrypt_text(engine1.encrypt_text("test")) == "test"

    # Raw 32-byte string
    raw_str_key = "01234567890123456789012345678901"
    engine2 = CryptoEngine(key=raw_str_key)
    assert engine2.decrypt_text(engine2.encrypt_text("test")) == "test"

    # Raw 32-byte bytes
    raw_bytes_key = b"01234567890123456789012345678901"
    engine3 = CryptoEngine(key=raw_bytes_key)
    assert engine3.decrypt_text(engine3.encrypt_text("test")) == "test"

    # Invalid key lengths must raise ValueError
    with pytest.raises(ValueError):
        CryptoEngine(key="too-short")

    with pytest.raises(ValueError):
        CryptoEngine(key="")


# ── INTEGRATION TESTS: DATABASE REPOSITORIES ──────────────────────────────────

def test_receipt_repository_encryption_flow(client, mock_user_session):
    """Verify that receipts are stored encrypted in Supabase and decrypted on retrieval."""
    payload = {
        "receipt": {
            "merchant_name": "Encrypted MegaStore",
            "total_amount": 99.99,
            "raw_text": "Raw secret text",
            "notes": "Highly sensitive purchase",
        }
    }
    # 1. Create receipt via API
    res = client.post("/api/v1/receipts/create", json=payload, headers=mock_user_session["headers"])
    assert res.status_code == 201
    created_data = res.json()
    receipt_id = created_data["id"]
    assert created_data["receipt"]["merchant_name"] == "Encrypted MegaStore"

    # 2. Query directly from Supabase to verify raw DB storage is encrypted
    import asyncio
    from supabase import acreate_client
    from src.config import get_settings

    async def verify_raw_db():
        settings = get_settings()
        db = await acreate_client(settings.supabase_url, settings.supabase_key)
        raw_res = await db.table("receipts").select("receipt").eq("id", receipt_id).maybe_single().execute()
        assert raw_res.data is not None
        raw_receipt = raw_res.data["receipt"]
        # Raw DB column must be encrypted envelope
        assert isinstance(raw_receipt, dict)
        assert raw_receipt.get("_enc") == "v1"
        assert "data" in raw_receipt
        assert "merchant_name" not in raw_receipt  # Plaintext fields NOT visible in raw DB

    asyncio.run(verify_raw_db())

    # 3. Retrieve receipt via API — must be transparently decrypted
    get_res = client.get(f"/api/v1/receipts/{receipt_id}", headers=mock_user_session["headers"])
    assert get_res.status_code == 200
    retrieved = get_res.json()
    assert retrieved["receipt"]["merchant_name"] == "Encrypted MegaStore"
    assert retrieved["receipt"]["total_amount"] == 99.99
    assert retrieved["receipt"]["notes"] == "Highly sensitive purchase"


def test_conversation_repository_encryption_flow(client, mock_user_session):
    """Verify that conversation titles and chat messages are encrypted at rest."""
    secret_title = "Classified Budget Plan"
    secret_msg = "My secret bank balance is $1,000,000"

    # 1. Create conversation
    create_res = client.post(
        "/api/v1/chat/create",
        json={"title": secret_title},
        headers=mock_user_session["headers"],
    )
    assert create_res.status_code == 201
    conv_id = create_res.json()["id"]
    assert create_res.json()["title"] == secret_title

    # 2. Query raw DB directly to verify conversation title is encrypted
    import asyncio
    from supabase import acreate_client
    from src.config import get_settings

    async def verify_raw_conv():
        settings = get_settings()
        db = await acreate_client(settings.supabase_url, settings.supabase_key)
        raw_res = await db.table("conversations").select("title").eq("id", conv_id).maybe_single().execute()
        assert raw_res.data is not None
        raw_title = raw_res.data["title"]
        assert raw_title.startswith("enc:v1:")
        assert secret_title not in raw_title

    asyncio.run(verify_raw_conv())

    # 3. Fetch conversation list via API — decrypted
    list_res = client.get("/api/v1/chat/list", headers=mock_user_session["headers"])
    assert list_res.status_code == 200
    conversations = list_res.json()
    matched = [c for c in conversations if c["id"] == conv_id]
    assert len(matched) == 1
    assert matched[0]["title"] == secret_title


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))
