import base64
import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from cryptography.exceptions import InvalidTag

from src.Infrastructure.key_vault import KeyVault, get_key_vault, KeyNotFoundError
from src.Infrastructure.crypto import (
    CryptoEngine,
    DecryptionError,
    encrypt_text_with_dek,
    decrypt_text_with_dek,
    encrypt_json_with_dek,
    decrypt_json_with_dek,
    safe_decrypt_json_with_dek,
    encrypt_bytes_with_dek,
    decrypt_bytes_with_dek,
    safe_decrypt_text_with_dek,
    safe_decrypt_bytes_with_dek,
)
from src.Auth.identity import Identity
from src.Models.schemas import Receipt
from src.Models.Receipts.receipt_repository import ReceiptRepository
from src.Models.Conversations.conversation_repository import ConversationRepository
from src.Services.image_service import ImageStorageService


# ── UNIT TESTS: KEYVAULT CORE ─────────────────────────────────────────────────

def test_key_vault_initialization():
    # 1. Default initialization uses settings KEK
    kv1 = KeyVault()
    assert kv1._kek_bytes is not None
    assert len(kv1._kek_bytes) == 32

    # 2. Custom 32-byte Base64 key
    b64_key = base64.b64encode(os.urandom(32)).decode("ascii")
    kv2 = KeyVault(kek=b64_key)
    assert len(kv2._kek_bytes) == 32

    # 3. Custom raw 32-byte bytes
    raw_key = os.urandom(32)
    kv3 = KeyVault(kek=raw_key)
    assert kv3._kek_bytes == raw_key

    # 4. Short / empty key raises ValueError
    with pytest.raises(ValueError):
        KeyVault(kek="too-short")

    with pytest.raises(ValueError):
        KeyVault(kek="")


def test_key_vault_generate_dek():
    kv = KeyVault()
    dek1 = kv.generate_dek()
    dek2 = kv.generate_dek()

    assert isinstance(dek1, bytes)
    assert len(dek1) == 32
    assert isinstance(dek2, bytes)
    assert len(dek2) == 32
    assert dek1 != dek2  # High entropy random generation


def test_wrap_and_unwrap_dek_roundtrip():
    kv = KeyVault()
    dek = kv.generate_dek()

    wrapped_b64 = kv.wrap_dek(dek)
    assert isinstance(wrapped_b64, str)

    raw_wrapped = base64.b64decode(wrapped_b64)
    # IV (12B) + Ciphertext (32B) + Tag (16B) = 60 bytes
    assert len(raw_wrapped) == 60

    unwrapped_dek = kv.unwrap_dek(wrapped_b64)
    assert unwrapped_dek == dek


def test_unwrap_dek_tamper_detection():
    kv = KeyVault()
    dek = kv.generate_dek()
    wrapped_b64 = kv.wrap_dek(dek)
    raw = bytearray(base64.b64decode(wrapped_b64))

    # 1. Tamper with IV (first 12 bytes)
    raw_tampered_iv = bytearray(raw)
    raw_tampered_iv[0] ^= 0xFF
    with pytest.raises(DecryptionError):
        kv.unwrap_dek(base64.b64encode(raw_tampered_iv).decode("ascii"))

    # 2. Tamper with Ciphertext (middle 32 bytes)
    raw_tampered_ct = bytearray(raw)
    raw_tampered_ct[20] ^= 0xFF
    with pytest.raises(DecryptionError):
        kv.unwrap_dek(base64.b64encode(raw_tampered_ct).decode("ascii"))

    # 3. Tamper with Tag (last 16 bytes)
    raw_tampered_tag = bytearray(raw)
    raw_tampered_tag[-1] ^= 0xFF
    with pytest.raises(DecryptionError):
        kv.unwrap_dek(base64.b64encode(raw_tampered_tag).decode("ascii"))


def test_unwrap_dek_wrong_kek():
    kek1 = base64.b64encode(os.urandom(32)).decode("ascii")
    kek2 = base64.b64encode(os.urandom(32)).decode("ascii")

    kv1 = KeyVault(kek=kek1)
    kv2 = KeyVault(kek=kek2)

    dek = kv1.generate_dek()
    wrapped = kv1.wrap_dek(dek)

    # Attempt unwrapping with a different KEK
    with pytest.raises(DecryptionError):
        kv2.unwrap_dek(wrapped)


# ── UNIT TESTS: CRYPTO ENGINE DEK METHODS ────────────────────────────────────

def test_crypto_text_with_dek():
    kv = KeyVault()
    dek = kv.generate_dek()
    text = "Secret user message with per-user DEK 🚀"

    envelope = encrypt_text_with_dek(text, dek)
    assert envelope.startswith("enc:v1:")

    decrypted = decrypt_text_with_dek(envelope, dek)
    assert decrypted == text

    # Attempt decrypting with wrong DEK must raise InvalidTag
    wrong_dek = kv.generate_dek()
    with pytest.raises(InvalidTag):
        decrypt_text_with_dek(envelope, wrong_dek)


def test_crypto_json_with_dek():
    kv = KeyVault()
    dek = kv.generate_dek()
    payload = {
        "merchant_name": "Per-User Grocery",
        "total_amount": 75.50,
        "items": ["Apple", "Bread", "Milk"],
    }

    enc = encrypt_json_with_dek(payload, dek)
    assert isinstance(enc, dict)
    assert enc.get("_enc") == "v1"

    decrypted = decrypt_json_with_dek(enc, dek)
    assert decrypted == payload

    # Safe decrypt with fallback on wrong DEK
    wrong_dek = kv.generate_dek()
    fallback = {"error": "decryption_failed"}
    res = safe_decrypt_json_with_dek(enc, wrong_dek, context="test", fallback=fallback)
    assert res == fallback


def test_crypto_bytes_with_dek():
    kv = KeyVault()
    dek = kv.generate_dek()
    raw_bytes = os.urandom(128)

    enc_bytes = encrypt_bytes_with_dek(raw_bytes, dek)
    assert enc_bytes.startswith(b"ENC:V1:")

    dec_bytes = decrypt_bytes_with_dek(enc_bytes, dek)
    assert dec_bytes == raw_bytes

    # Safe decrypt bytes fallback
    wrong_dek = kv.generate_dek()
    fallback = b"fallback-image-data"
    res = safe_decrypt_bytes_with_dek(enc_bytes, wrong_dek, context="test", fallback=fallback)
    assert res == fallback


# ── ASYNC TESTS: PROVISION, RETRIEVE, AND DESTROY (MOCK DB) ──────────────────

@pytest.mark.anyio
async def test_key_vault_db_lifecycle():
    kv = KeyVault()
    kv.clear_cache()
    user_id = "user-uuid-12345"

    fake_db_storage: dict[str, dict] = {}

    def make_mock_client():
        client = MagicMock()

        def table_fn(table_name):
            t = MagicMock()

            # select
            def select_fn(*args, **kwargs):
                s = MagicMock()

                def eq_fn(col, val):
                    e = MagicMock()

                    def maybe_single_fn():
                        ms = MagicMock()

                        async def execute_fn():
                            data = fake_db_storage.get(val)
                            res = MagicMock()
                            res.data = data
                            return res

                        ms.execute = execute_fn
                        return ms

                    e.maybe_single = maybe_single_fn
                    return e

                s.eq = eq_fn
                return s

            # insert
            def insert_fn(row):
                ins = MagicMock()

                async def execute_fn():
                    fake_db_storage[row["user_id"]] = row
                    res = MagicMock()
                    res.data = [row]
                    return res

                ins.execute = execute_fn
                return ins

            # delete
            def delete_fn():
                d = MagicMock()

                def eq_fn(col, val):
                    del_eq = MagicMock()

                    async def execute_fn():
                        deleted = fake_db_storage.pop(val, None)
                        res = MagicMock()
                        res.data = [deleted] if deleted else []
                        return res

                    del_eq.execute = execute_fn
                    return del_eq

                d.eq = eq_fn
                return d

            t.select = select_fn
            t.insert = insert_fn
            t.delete = delete_fn
            return t

        client.table = table_fn
        return client

    mock_db = make_mock_client()

    # 1. Initially user has no key
    with pytest.raises(KeyNotFoundError):
        await kv.get_user_dek(user_id, mock_db)

    # 2. Provision user DEK
    await kv.provision_user_dek(user_id, mock_db)
    assert user_id in fake_db_storage
    wrapped_dek = fake_db_storage[user_id]["encrypted_dek"]
    assert wrapped_dek is not None

    # 3. Retrieve user DEK
    dek = await kv.get_user_dek(user_id, mock_db)
    assert isinstance(dek, bytes)
    assert len(dek) == 32
    assert kv.unwrap_dek(wrapped_dek) == dek

    # 4. Idempotency: provision again does not overwrite
    await kv.provision_user_dek(user_id, mock_db)
    dek_after_second_provision = await kv.get_user_dek(user_id, mock_db)
    assert dek_after_second_provision == dek

    # 5. Destroy user DEK (Crypto-Shredding)
    destroyed = await kv.destroy_user_dek(user_id, mock_db)
    assert destroyed is True
    assert user_id not in fake_db_storage

    # 6. Key lookup after crypto-shredding must fail
    with pytest.raises(KeyNotFoundError):
        await kv.get_user_dek(user_id, mock_db)


# ── INTEGRATION TESTS: REPOSITORY DEK ENCRYPTION ─────────────────────────────

@pytest.mark.anyio
async def test_receipt_repository_dek_flow():
    user_id = "test-user-dek-receipts"
    identity_user = Identity(user_id=user_id, username="dekuser", is_authenticated=True)
    identity_guest = Identity(device_id="guest-device-999", is_authenticated=False)

    receipt = Receipt(merchant_name="Encrypted DEK Mart", total_amount=49.99)

    # In-memory store for receipts table and user_keys table
    tables = {
        "receipts": [],
        "user_keys": [],
    }

    class MockQueryBuilder:
        def __init__(self, data_list, is_single=False):
            self._data = list(data_list)
            self._is_single = is_single

        def select(self, *a, **kw):
            return self

        def is_(self, *a, **kw):
            return self

        def order(self, *a, **kw):
            return self

        def range(self, *a, **kw):
            return self

        def eq(self, col, val):
            self._data = [r for r in self._data if str(r.get(col, "")) == str(val)]
            return self

        def maybe_single(self):
            self._is_single = True
            return self

        async def execute(self):
            res = MagicMock()
            if self._is_single:
                res.data = self._data[0] if self._data else None
            else:
                res.data = list(self._data)
                res.count = len(self._data)
            return res

    def make_db():
        client = MagicMock()

        def table_fn(table_name):
            t = MagicMock()
            if table_name == "user_keys":
                def sel(*a, **kw):
                    return MockQueryBuilder(tables["user_keys"])

                def ins(row):
                    i = MagicMock()
                    async def ex():
                        tables["user_keys"].append(dict(row))
                        res = MagicMock()
                        res.data = [row]
                        return res
                    i.execute = ex
                    return i

                t.select = sel
                t.insert = ins
                return t

            elif table_name == "receipts":
                def ins(rows):
                    i = MagicMock()
                    async def ex():
                        rows_list = [rows] if isinstance(rows, dict) else rows
                        inserted = []
                        for r in rows_list:
                            row_copy = dict(r)
                            row_copy.setdefault("id", "rec-uuid-" + os.urandom(4).hex())
                            tables["receipts"].append(row_copy)
                            inserted.append(row_copy)
                        res = MagicMock()
                        res.data = inserted
                        return res
                    i.execute = ex
                    return i

                def sel(*a, **kw):
                    return MockQueryBuilder(tables["receipts"])

                t.insert = ins
                t.select = sel
                return t

            return t

        client.table = table_fn
        return client

    db = make_db()
    kv = KeyVault()
    # Provision DEK for user
    await kv.provision_user_dek(user_id, db)
    user_dek = await kv.get_user_dek(user_id, db)

    repo = ReceiptRepository(db)

    # 1. User insert -> must set enc_version = 1 and be encrypted with DEK
    user_rec = await repo.create(identity_user, receipt)
    assert user_rec["enc_version"] == 1
    # Check raw table storage
    stored_row = [r for r in tables["receipts"] if r["id"] == user_rec["id"]][0]
    assert stored_row["enc_version"] == 1
    # Raw DB receipt JSON must be decryptable with user_dek
    raw_decrypted = decrypt_json_with_dek(stored_row["receipt"], user_dek)
    assert raw_decrypted["merchant_name"] == "Encrypted DEK Mart"

    # 2. User read -> transparently decrypted
    fetched = await repo.get_by_id(user_rec["id"], identity_user)
    assert fetched is not None
    assert fetched["receipt"]["merchant_name"] == "Encrypted DEK Mart"

    # 3. Guest insert -> must set enc_version = 0 (global key)
    guest_rec = await repo.create(identity_guest, receipt)
    assert guest_rec["enc_version"] == 0
    stored_guest = [r for r in tables["receipts"] if r["id"] == guest_rec["id"]][0]
    assert stored_guest["enc_version"] == 0


@pytest.mark.anyio
async def test_conversation_repository_dek_flow():
    user_id = "test-user-conv-dek"
    identity = Identity(user_id=user_id, username="convuser", is_authenticated=True)

    tables = {
        "conversations": {},
        "chat_messages": [],
        "user_keys": {},
    }

    def make_db():
        client = MagicMock()

        def table_fn(table_name):
            t = MagicMock()
            if table_name == "user_keys":
                def sel(*a, **kw):
                    s = MagicMock()
                    def eq_fn(col, val):
                        e = MagicMock()
                        def ms():
                            m = MagicMock()
                            async def ex():
                                res = MagicMock()
                                res.data = tables["user_keys"].get(val)
                                return res
                            m.execute = ex
                            return m
                        e.maybe_single = ms
                        return e
                    s.eq = eq_fn
                    return s

                def ins(row):
                    i = MagicMock()
                    async def ex():
                        tables["user_keys"][row["user_id"]] = row
                        res = MagicMock()
                        res.data = [row]
                        return res
                    i.execute = ex
                    return i

                t.select = sel
                t.insert = ins
                return t

            elif table_name == "conversations":
                def ins(row):
                    i = MagicMock()
                    async def ex():
                        r = dict(row)
                        r.setdefault("id", "conv-uuid-" + os.urandom(4).hex())
                        tables["conversations"][r["id"]] = r
                        res = MagicMock()
                        res.data = [r]
                        return res
                    i.execute = ex
                    return i

                def sel(*a, **kw):
                    s = MagicMock()
                    s.is_ = lambda c, v: s
                    s.order = lambda c, desc=False: s
                    s.range = lambda start, end: s
                    def eq_fn(col, val):
                        e = MagicMock()
                        e.is_ = lambda c, v: e
                        def ms():
                            m = MagicMock()
                            async def ex():
                                res = MagicMock()
                                res.data = tables["conversations"].get(val)
                                return res
                            m.execute = ex
                            return m
                        e.maybe_single = ms
                        return e
                    s.eq = eq_fn
                    async def ex():
                        res = MagicMock()
                        res.data = list(tables["conversations"].values())
                        return res
                    s.execute = ex
                    return s

                t.insert = ins
                t.select = sel
                return t

            elif table_name == "chat_messages":
                def ins(row):
                    i = MagicMock()
                    async def ex():
                        r = dict(row)
                        r.setdefault("id", "msg-uuid-" + os.urandom(4).hex())
                        tables["chat_messages"].append(r)
                        res = MagicMock()
                        res.data = [r]
                        return res
                    i.execute = ex
                    return i

                def sel(*a, **kw):
                    s = MagicMock()
                    s.order = lambda c, desc=False: s
                    s.range = lambda start, end: s
                    def eq_fn(col, val):
                        e = MagicMock()
                        e.order = lambda c, desc=False: e
                        e.range = lambda start, end: e
                        async def ex():
                            matched = [m for m in tables["chat_messages"] if m.get(col) == val]
                            res = MagicMock()
                            res.data = matched
                            res.count = len(matched)
                            return res
                        e.execute = ex
                        return e
                    s.eq = eq_fn
                    return s

                t.insert = ins
                t.select = sel
                return t

            return t

        client.table = table_fn
        return client

    db = make_db()
    kv = KeyVault()
    await kv.provision_user_dek(user_id, db)
    user_dek = await kv.get_user_dek(user_id, db)

    repo = ConversationRepository(db)

    # 1. Create conversation -> title encrypted with DEK, enc_version = 1
    conv = await repo.create_conversation(identity, title="My Financial Assistant")
    assert conv["enc_version"] == 1
    assert conv["title"] == "My Financial Assistant"

    raw_conv = tables["conversations"][conv["id"]]
    assert raw_conv["enc_version"] == 1
    assert decrypt_text_with_dek(raw_conv["title"], user_dek) == "My Financial Assistant"

    # 2. Add message -> encrypted with DEK, enc_version = 1
    msg = await repo.add_message(conv["id"], "user", "What is my total spend?")
    assert msg["enc_version"] == 1
    assert msg["content"] == "What is my total spend?"

    raw_msg = tables["chat_messages"][0]
    assert raw_msg["enc_version"] == 1
    assert decrypt_text_with_dek(raw_msg["content"], user_dek) == "What is my total spend?"

    # 3. Read messages -> decrypted
    messages, count = await repo.get_messages(conv["id"], limit=10, offset=0, identity=identity)
    assert count == 1
    assert messages[0]["content"] == "What is my total spend?"


@pytest.mark.anyio
async def test_image_service_dek_encryption():
    user_id = "test-user-img-dek"
    kv = KeyVault()
    user_dek = kv.generate_dek()

    # Create dummy 100x100 RGB JPEG bytes
    from PIL import Image
    import io
    img = Image.new("RGB", (100, 100), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    dummy_jpeg = buf.getvalue()

    uploaded_files: dict[str, bytes] = {}

    mock_db = MagicMock()
    mock_storage = MagicMock()
    mock_bucket = MagicMock()

    async def mock_upload(path, file, file_options=None):
        uploaded_files[path] = file

    async def mock_download(path):
        return uploaded_files.get(path)

    mock_bucket.upload = mock_upload
    mock_bucket.download = mock_download
    mock_storage.from_ = lambda bucket_name: mock_bucket
    mock_db.storage = mock_storage

    image_service = ImageStorageService(mock_db, bucket="user-data")

    # 1. Upload receipt image with user DEK
    receipt_id = "receipt-img-uuid-001"
    storage_path = await image_service.upload_receipt_image(
        user_id=user_id,
        receipt_id=receipt_id,
        image_bytes=dummy_jpeg,
        dek=user_dek,
    )
    assert storage_path == f"{user_id}/receipt_images/{receipt_id}.jpg"

    # Verify uploaded bytes are encrypted with user DEK
    uploaded_encrypted_bytes = uploaded_files[storage_path]
    assert uploaded_encrypted_bytes.startswith(b"ENC:V1:")
    # Direct decryption with DEK recovers valid JPEG
    decrypted_raw = decrypt_bytes_with_dek(uploaded_encrypted_bytes, user_dek)
    assert decrypted_raw.startswith(b"\xff\xd8")  # JPEG magic bytes

    # 2. Download receipt image with DEK
    downloaded = await image_service.download_receipt_image(storage_path, dek=user_dek)
    assert downloaded is not None
    assert downloaded.startswith(b"\xff\xd8")
