import pytest
from unittest.mock import AsyncMock, MagicMock
from src.API.v1.receipts import (
    _apply_receipt_record_tier_gate,
    list_receipts,
    get_receipt,
    create_receipt,
    update_receipt,
)
from src.Auth.identity import Identity
from src.Models.schemas import Receipt, LineItem


SAMPLE_ROW = {
    "id": "rec-1234-uuid",
    "user_id": "user-abc-123",
    "device_id": "device-xyz-456",
    "receipt": {
        "merchant_name": "Battercatch",
        "total_amount": 98.20,
        "currency": "SGD",
        "date": "2026-10-01T00:00:00Z",
        "category": "Dining",
        "raw_text": "Sample raw text transcript",
        "confidence_score": 0.95,
        "line_items": [
            {"description": "Fish & Chips", "quantity": 2, "unit_price": 40.0, "total_price": 80.0},
            {"description": "Soda Can", "quantity": 2, "unit_price": 5.0, "total_price": 10.0},
        ],
        "subtotal": 90.0,
        "tax_amount": 8.20,
    },
    "receipt_image_path": "user-abc-123/receipt_images/rec-1234-uuid.jpg",
    "created_at": "2026-10-01T00:00:00Z",
    "updated_at": "2026-10-01T00:00:00Z",
    "deleted_at": None,
}


class TestReceiptsTierGateHelper:
    def test_gate_free_tier_nulls_premium_fields(self):
        """Free tier should have line_items, subtotal, and tax_amount set to None."""
        gated = _apply_receipt_record_tier_gate(SAMPLE_ROW, "free")
        assert gated is not None
        rec = gated["receipt"]
        assert rec["line_items"] is None
        assert rec["subtotal"] is None
        assert rec["tax_amount"] is None
        # Core fields must remain intact
        assert rec["merchant_name"] == "Battercatch"
        assert rec["total_amount"] == 98.20
        assert rec["currency"] == "SGD"
        assert rec["category"] == "Dining"

    def test_gate_premium_tier_preserves_all_fields(self):
        """Premium tier should preserve full line_items, subtotal, and tax_amount."""
        gated = _apply_receipt_record_tier_gate(SAMPLE_ROW, "premium")
        assert gated is not None
        rec = gated["receipt"]
        assert len(rec["line_items"]) == 2
        assert rec["subtotal"] == 90.0
        assert rec["tax_amount"] == 8.20

    def test_gate_dev_tier_preserves_all_fields(self):
        """Dev tier should preserve full line_items, subtotal, and tax_amount."""
        gated = _apply_receipt_record_tier_gate(SAMPLE_ROW, "dev")
        assert gated is not None
        rec = gated["receipt"]
        assert len(rec["line_items"]) == 2

    def test_gate_handles_none_and_non_dict_gracefully(self):
        assert _apply_receipt_record_tier_gate(None, "free") is None
        assert _apply_receipt_record_tier_gate("string-data", "free") == "string-data"


import asyncio


class TestReceiptsEndpointsTierGating:
    def test_list_receipts_gates_for_free_tier(self):
        mock_repo = MagicMock()
        mock_repo.get_all_by_identity = AsyncMock(return_value=[SAMPLE_ROW])

        mock_user_repo = MagicMock()
        mock_quota_service = MagicMock()
        mock_quota_service.get_identity_tier = AsyncMock(return_value="free")

        identity = Identity(user_id="user-abc-123", username="testuser")

        results = asyncio.run(list_receipts(
            updated_after=None,
            limit=None,
            offset=None,
            identity=identity,
            repo=mock_repo,
            user_repo=mock_user_repo,
            quota_service=mock_quota_service,
        ))

        assert len(results) == 1
        record = results[0]
        rec = record.get("receipt")
        assert rec["line_items"] is None
        assert rec["subtotal"] is None
        assert rec["tax_amount"] is None
        assert rec["merchant_name"] == "Battercatch"

    def test_list_receipts_returns_full_data_for_premium_tier(self):
        mock_repo = MagicMock()
        mock_repo.get_all_by_identity = AsyncMock(return_value=[SAMPLE_ROW])

        mock_user_repo = MagicMock()
        mock_quota_service = MagicMock()
        mock_quota_service.get_identity_tier = AsyncMock(return_value="premium")

        identity = Identity(user_id="user-abc-123", username="testuser")

        results = asyncio.run(list_receipts(
            updated_after=None,
            limit=None,
            offset=None,
            identity=identity,
            repo=mock_repo,
            user_repo=mock_user_repo,
            quota_service=mock_quota_service,
        ))

        assert len(results) == 1
        record = results[0]
        rec = record.get("receipt")
        assert len(rec["line_items"]) == 2
        assert rec["subtotal"] == 90.0

    def test_get_receipt_gates_for_free_tier(self):
        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=SAMPLE_ROW)

        mock_user_repo = MagicMock()
        mock_quota_service = MagicMock()
        mock_quota_service.get_identity_tier = AsyncMock(return_value="free")

        identity = Identity(user_id="user-abc-123", username="testuser")

        result = asyncio.run(get_receipt(
            receipt_id="rec-1234-uuid",
            identity=identity,
            repo=mock_repo,
            user_repo=mock_user_repo,
            quota_service=mock_quota_service,
        ))

        rec = result.get("receipt")
        assert rec["line_items"] is None
        assert rec["subtotal"] is None

    def test_get_receipt_returns_full_data_for_premium_tier(self):
        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=SAMPLE_ROW)

        mock_user_repo = MagicMock()
        mock_quota_service = MagicMock()
        mock_quota_service.get_identity_tier = AsyncMock(return_value="premium")

        identity = Identity(user_id="user-abc-123", username="testuser")

        result = asyncio.run(get_receipt(
            receipt_id="rec-1234-uuid",
            identity=identity,
            repo=mock_repo,
            user_repo=mock_user_repo,
            quota_service=mock_quota_service,
        ))

        rec = result.get("receipt")
        assert len(rec["line_items"]) == 2
        assert rec["tax_amount"] == 8.20

    def test_create_receipt_stores_full_data_and_returns_gated_to_free(self):
        """Verifies full receipt is passed to repo.create, while response is gated for free tier."""
        mock_repo = MagicMock()
        mock_repo.create = AsyncMock(return_value=dict(SAMPLE_ROW))

        mock_user_repo = MagicMock()
        mock_quota_service = MagicMock()
        mock_quota_service.get_identity_tier = AsyncMock(return_value="free")

        mock_image_storage = MagicMock()

        mock_request = MagicMock()
        mock_request.headers = {"content-type": "application/json"}
        mock_request.json = AsyncMock(return_value={
            "receipt": {
                "merchant_name": "Battercatch",
                "total_amount": 98.20,
                "currency": "SGD",
                "line_items": [{"description": "Item", "quantity": 1, "unit_price": 98.20, "total_price": 98.20}],
            }
        })

        identity = Identity(user_id="user-abc-123", username="testuser")

        result = asyncio.run(create_receipt(
            request=mock_request,
            identity=identity,
            repo=mock_repo,
            image_storage=mock_image_storage,
            user_repo=mock_user_repo,
            quota_service=mock_quota_service,
        ))

        # Confirm repo.create was invoked with a Receipt containing the line items
        created_receipt_arg = mock_repo.create.call_args[0][1]
        assert isinstance(created_receipt_arg, Receipt)
        assert created_receipt_arg.line_items is not None
        assert len(created_receipt_arg.line_items) == 1

        # Confirm the response returned to the free-tier caller had line_items gated
        assert result["receipt"]["line_items"] is None

    def test_update_receipt_gates_for_free_tier(self):
        mock_repo = MagicMock()
        mock_repo.update = AsyncMock(return_value=dict(SAMPLE_ROW))

        mock_user_repo = MagicMock()
        mock_quota_service = MagicMock()
        mock_quota_service.get_identity_tier = AsyncMock(return_value="free")

        mock_image_storage = MagicMock()

        mock_request = MagicMock()
        mock_request.headers = {"content-type": "application/json"}
        mock_request.json = AsyncMock(return_value={
            "merchant_name": "Updated Merchant",
            "total_amount": 100.0,
            "currency": "SGD",
        })

        identity = Identity(user_id="user-abc-123", username="testuser")

        result = asyncio.run(update_receipt(
            receipt_id="rec-1234-uuid",
            request=mock_request,
            identity=identity,
            repo=mock_repo,
            image_storage=mock_image_storage,
            user_repo=mock_user_repo,
            quota_service=mock_quota_service,
        ))

        # Confirm update was called with the receipt
        updated_receipt_arg = mock_repo.update.call_args[1]["receipt"]
        assert isinstance(updated_receipt_arg, Receipt)
        assert updated_receipt_arg.merchant_name == "Updated Merchant"

        # Confirm the response returned to the free-tier caller had line_items gated
        assert result["receipt"]["line_items"] is None

    def test_repository_update_preserves_existing_line_items_when_incoming_is_none(self):
        from src.Models.Receipts.receipt_repository import ReceiptRepository

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.eq.return_value = mock_query
        mock_query.is_.return_value = mock_query
        mock_query.execute = AsyncMock(return_value=MagicMock(data=[dict(SAMPLE_ROW)]))
        mock_db.table.return_value.update.return_value = mock_query

        repo = ReceiptRepository(db=mock_db)
        repo.get_by_id = AsyncMock(return_value=dict(SAMPLE_ROW))
        repo.crypto = MagicMock()
        repo.crypto.encrypt_json = MagicMock(return_value="encrypted-json-payload")
        repo.crypto.safe_decrypt_json = MagicMock(return_value=dict(SAMPLE_ROW["receipt"]))

        identity = Identity(user_id="user-abc-123", username="testuser")
        incoming_receipt = Receipt(
            merchant_name="Updated Merchant",
            total_amount=100.0,
            line_items=None,
            subtotal=None,
            tax_amount=None,
        )

        asyncio.run(repo.update(
            receipt_id="rec-1234-uuid",
            identity=identity,
            receipt=incoming_receipt,
        ))

        # repo.crypto.encrypt_json should have been called with the merged dict containing the existing line_items
        assert repo.crypto.encrypt_json.called
        encrypted_dict = repo.crypto.encrypt_json.call_args[0][0]
        assert encrypted_dict["merchant_name"] == "Updated Merchant"
        assert len(encrypted_dict["line_items"]) == 2
        assert encrypted_dict["line_items"][0]["description"] == "Fish & Chips"
        assert encrypted_dict["subtotal"] == 90.0
        assert encrypted_dict["tax_amount"] == 8.20

