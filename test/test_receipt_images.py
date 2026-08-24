import io
import uuid
import pytest
from PIL import Image
from src.Infrastructure.crypto import get_crypto_engine


def _create_sample_jpeg_bytes() -> bytes:
    img = Image.new('RGB', (100, 100), color='blue')
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    return buf.getvalue()


def test_crypto_engine_bytes_encryption():
    crypto = get_crypto_engine()
    raw_data = b'Hello, secure receipt image binary payload!'

    encrypted = crypto.encrypt_bytes(raw_data)
    assert encrypted.startswith(b'ENC:V1:')
    assert encrypted != raw_data

    decrypted = crypto.decrypt_bytes(encrypted)
    assert decrypted == raw_data


def test_crypto_engine_bytes_legacy_passthrough():
    crypto = get_crypto_engine()
    jpeg_bytes = _create_sample_jpeg_bytes()

    assert jpeg_bytes.startswith(b'\xff\xd8\xff')
    decrypted = crypto.decrypt_bytes(jpeg_bytes)
    assert decrypted == jpeg_bytes


def test_crypto_engine_bytes_tamper_detection():
    crypto = get_crypto_engine()
    raw_data = b'Confidential receipt snapshot'
    encrypted = bytearray(crypto.encrypt_bytes(raw_data))

    encrypted[-1] ^= 0xFF

    with pytest.raises(Exception):
        crypto.decrypt_bytes(bytes(encrypted))


def test_receipt_image_upload_and_download_flow(client, mock_user_session):
    headers = mock_user_session['headers']
    jpeg_bytes = _create_sample_jpeg_bytes()

    files = {'receipt_image': ('receipt.jpg', jpeg_bytes, 'image/jpeg')}
    data = {
        'merchant_name': 'Encrypted Cafe',
        'date': '2026-08-24',
        'total_amount': 12.50,
        'currency': 'USD',
        'category': 'Dining',
    }
    response = client.post('/api/v1/receipts/create', data=data, files=files, headers=headers)
    assert response.status_code == 201
    record = response.json()
    receipt_id = record['id']
    assert record['receipt_image_path'] is not None

    image_response = client.get(f'/api/v1/receipts/{receipt_id}/image', headers=headers)
    assert image_response.status_code == 200
    assert image_response.headers.get('content-type') == 'image/jpeg'
    assert len(image_response.content) > 0


def test_receipt_image_cross_tenant_isolation(client, mock_user_session):
    user_a_headers = mock_user_session['headers']
    jpeg_bytes = _create_sample_jpeg_bytes()

    # 1. User A creates receipt with image
    files = {'receipt_image': ('secret_receipt.jpg', jpeg_bytes, 'image/jpeg')}
    data = {
        'merchant_name': 'User A Private Store',
        'date': '2026-08-24',
        'total_amount': 99.99,
        'currency': 'USD',
        'category': 'Shopping',
    }
    response = client.post('/api/v1/receipts/create', data=data, files=files, headers=user_a_headers)
    assert response.status_code == 201
    receipt_id = response.json()['id']

    # 2. Register User B with valid 3-10 char username
    user_b_name = f'ub_{uuid.uuid4().hex[:4]}'
    user_b_pass = 'SecurePass123!'
    create_resp = client.post(
        '/api/v1/user/create',
        json={
            'username': user_b_name,
            'email': f'{user_b_name}@test.com',
            'password': user_b_pass,
        },
    )
    assert create_resp.status_code == 201

    user_b_headers = {
        'X-User-Name': user_b_name,
        'X-User-Token': user_b_pass,
    }

    # 3. User B attempts to access User A\'s receipt image -> MUST FAIL with 404
    unauthorized_resp = client.get(f'/api/v1/receipts/{receipt_id}/image', headers=user_b_headers)
    assert unauthorized_resp.status_code == 404


def test_receipt_image_unauthenticated_access_rejected(client):
    fake_receipt_id = str(uuid.uuid4())
    resp = client.get(f'/api/v1/receipts/{fake_receipt_id}/image')
    assert resp.status_code in [400, 401, 422]
