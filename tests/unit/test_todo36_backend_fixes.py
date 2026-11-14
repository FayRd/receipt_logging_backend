"""Unit tests for TODO-36 backend fixes.

Validates:
1. ReceiptRepository.get_all_by_identity executes cleanly without NameError
   (previously caused by undefined `receipt` reference in currency rate check).
2. DeviceRepository.register_or_update allows re-registration and guest state reset
   for development devices (dev_fixed_debug_device*) or non-production environments
   (development, staging) when tokens mismatch, while preserving strict token
   mismatch validation in production for non-dev devices.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.Auth.identity import Identity
from src.Auth.device_security import hash_device_token
from src.Models.Devices.device_repository import DeviceRepository
from src.Models.Receipts.receipt_repository import ReceiptRepository
from src.Models.schemas import DeviceRegisterRequest


class TestReceiptRepositoryGetAllByIdentity:
    """Test suite verifying ReceiptRepository.get_all_by_identity fix."""

    def test_get_all_by_identity_no_name_error_authenticated(self):
        """Verify get_all_by_identity returns decrypted rows without NameError for authenticated user."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.select.return_value = mock_query
        mock_query.is_.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.gt.return_value = mock_query
        mock_query.order.return_value = mock_query
        mock_query.range.return_value = mock_query

        sample_rows = [
            {
                "id": "rec-1",
                "user_id": "user-uuid-123",
                "receipt": {"merchant_name": "Store A", "total_amount": 15.5, "currency": "USD"},
                "updated_at": "2026-11-14T00:00:00Z",
                "deleted_at": None,
            },
            {
                "id": "rec-2",
                "user_id": "user-uuid-123",
                "receipt": {"merchant_name": "Store B", "total_amount": 42.0, "currency": "SGD"},
                "updated_at": "2026-11-13T00:00:00Z",
                "deleted_at": None,
            },
        ]
        mock_response = MagicMock()
        mock_response.data = sample_rows
        mock_query.execute = AsyncMock(return_value=mock_response)

        with patch("src.Models.Receipts.receipt_repository.get_crypto_engine") as mock_crypto, \
             patch("src.Models.Receipts.receipt_repository.get_key_vault") as mock_vault:
            crypto_inst = MagicMock()
            crypto_inst.safe_decrypt_json.side_effect = lambda data, **kwargs: data
            crypto_inst.safe_decrypt_json_with_dek.side_effect = lambda data, dek, **kwargs: data
            mock_crypto.return_value = crypto_inst

            vault_inst = MagicMock()
            vault_inst.get_user_dek_or_none = AsyncMock(return_value=None)
            mock_vault.return_value = vault_inst

            repo = ReceiptRepository(mock_db)
            identity = Identity(user_id="user-uuid-123", device_id="device-uuid-456")

            # This call previously raised NameError: name 'receipt' is not defined
            result = asyncio.run(repo.get_all_by_identity(
                identity=identity,
                updated_after="2026-11-01T00:00:00Z",
                limit=10,
                offset=0,
            ))

            assert len(result) == 2
            assert result[0]["id"] == "rec-1"
            assert result[1]["id"] == "rec-2"
            mock_query.execute.assert_awaited_once()

    def test_get_all_by_identity_no_name_error_guest_device(self):
        """Verify get_all_by_identity succeeds for guest device identity."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.select.return_value = mock_query
        mock_query.is_.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.order.return_value = mock_query

        sample_rows = [
            {
                "id": "rec-guest-1",
                "user_id": None,
                "device_id": "dev-guest-001",
                "receipt": {"merchant_name": "Cafe", "total_amount": 5.0, "currency": "USD"},
                "updated_at": "2026-11-14T01:00:00Z",
                "deleted_at": None,
            }
        ]
        mock_response = MagicMock()
        mock_response.data = sample_rows
        mock_query.execute = AsyncMock(return_value=mock_response)

        with patch("src.Models.Receipts.receipt_repository.get_crypto_engine") as mock_crypto, \
             patch("src.Models.Receipts.receipt_repository.get_key_vault") as mock_vault:
            crypto_inst = MagicMock()
            crypto_inst.safe_decrypt_json.side_effect = lambda data, **kwargs: data
            mock_crypto.return_value = crypto_inst

            vault_inst = MagicMock()
            vault_inst.get_user_dek_or_none = AsyncMock(return_value=None)
            mock_vault.return_value = vault_inst

            repo = ReceiptRepository(mock_db)
            identity = Identity(user_id=None, device_id="dev-guest-001")

            result = asyncio.run(repo.get_all_by_identity(identity=identity))

            assert len(result) == 1
            assert result[0]["id"] == "rec-guest-1"


class TestDeviceRepositoryRegisterOrUpdate:
    """Test suite verifying DeviceRepository.register_or_update dev re-registration and token mismatch rules."""

    def test_dev_fixed_debug_device_token_mismatch_updates_token_and_resets_user_to_guest(self):
        """Verify dev_fixed_debug_device allows re-registration on token mismatch and resets user_id to None."""
        mock_db = MagicMock()
        repo = DeviceRepository(mock_db)

        # Existing device in DB has a stale token hash and was tied to a user
        existing_record = {
            "id": "dev-row-uuid-001",
            "name": "dev_fixed_debug_device_001",
            "device_token_hash": "old_mismatched_hash_12345",
            "user_id": "old-user-uuid-999",
        }
        repo.get_by_device_id = AsyncMock(return_value=existing_record)

        updated_row = {
            "id": "dev-row-uuid-001",
            "name": "dev_fixed_debug_device_001",
            "device_token_hash": hash_device_token("token_fixed_debug_secret_001"),
            "user_id": None,
        }
        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.update.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_response = MagicMock()
        mock_response.data = [updated_row]
        mock_query.execute = AsyncMock(return_value=mock_response)

        req = DeviceRegisterRequest(
            device_name="dev_fixed_debug_device_001",
            device_token="token_fixed_debug_secret_001",
            username=None,
        )

        with patch("src.Models.Devices.device_repository.get_settings") as mock_settings:
            # Even in production environment, name starting with dev_fixed_debug_device triggers dev override
            mock_settings.return_value.environment = "production"

            result = asyncio.run(repo.register_or_update(req))

            assert result is not None
            assert result["id"] == "dev-row-uuid-001"
            assert result["user_id"] is None
            # Verify update payload sent to DB:
            mock_query.update.assert_called_once()
            called_updates = mock_query.update.call_args[0][0]
            assert called_updates["device_token_hash"] == hash_device_token("token_fixed_debug_secret_001")
            assert called_updates["user_id"] is None

    def test_dev_fixed_debug_device_re_register_with_username_updates_user_id(self):
        """Verify dev_fixed_debug_device re-registration with username associates resolved user."""
        mock_db = MagicMock()
        repo = DeviceRepository(mock_db)

        existing_record = {
            "id": "dev-row-uuid-001",
            "name": "dev_fixed_debug_device_001",
            "device_token_hash": "old_hash",
            "user_id": None,
        }
        repo.get_by_device_id = AsyncMock(return_value=existing_record)

        updated_row = {
            "id": "dev-row-uuid-001",
            "name": "dev_fixed_debug_device_001",
            "device_token_hash": hash_device_token("new_token"),
            "user_id": "user-uuid-alex",
        }
        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.update.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_response = MagicMock()
        mock_response.data = [updated_row]
        mock_query.execute = AsyncMock(return_value=mock_response)

        req = DeviceRegisterRequest(
            device_name="dev_fixed_debug_device_001",
            device_token="new_token",
            username="alex",
        )

        with patch("src.Models.Devices.device_repository.get_settings") as mock_settings, \
             patch("src.Models.Devices.device_repository.UserRepository") as mock_user_repo_cls:
            mock_settings.return_value.environment = "development"
            user_repo_inst = MagicMock()
            user_repo_inst.get_by_identifier = AsyncMock(return_value={"id": "user-uuid-alex", "username": "alex"})
            mock_user_repo_cls.return_value = user_repo_inst

            result = asyncio.run(repo.register_or_update(req))

            assert result is not None
            assert result["user_id"] == "user-uuid-alex"
            called_updates = mock_query.update.call_args[0][0]
            assert called_updates["user_id"] == "user-uuid-alex"

    def test_development_environment_allows_re_registration_for_any_device(self):
        """Verify environment == 'development' allows re-registration on token mismatch for arbitrary device."""
        mock_db = MagicMock()
        repo = DeviceRepository(mock_db)

        existing_record = {
            "id": "phone-uuid-123",
            "name": "regular_phone_tester",
            "device_token_hash": "stale_hash",
            "user_id": "some_user_id",
        }
        repo.get_by_device_id = AsyncMock(return_value=existing_record)

        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.update.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_response = MagicMock()
        mock_response.data = [{"id": "phone-uuid-123", "user_id": None}]
        mock_query.execute = AsyncMock(return_value=mock_response)

        req = DeviceRegisterRequest(
            device_name="regular_phone_tester",
            device_token="new_secret_key_12345",
            username=None,
        )

        with patch("src.Models.Devices.device_repository.get_settings") as mock_settings:
            mock_settings.return_value.environment = "development"

            result = asyncio.run(repo.register_or_update(req))

            assert result is not None
            called_updates = mock_query.update.call_args[0][0]
            assert called_updates["device_token_hash"] == hash_device_token("new_secret_key_12345")
            assert called_updates["user_id"] is None

    def test_staging_environment_allows_re_registration_for_any_device(self):
        """Verify environment == 'staging' allows re-registration on token mismatch for arbitrary device."""
        mock_db = MagicMock()
        repo = DeviceRepository(mock_db)

        existing_record = {
            "id": "phone-uuid-456",
            "name": "staging_test_device",
            "device_token_hash": "stale_hash",
            "user_id": "staging_user",
        }
        repo.get_by_device_id = AsyncMock(return_value=existing_record)

        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.update.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_response = MagicMock()
        mock_response.data = [{"id": "phone-uuid-456", "user_id": None}]
        mock_query.execute = AsyncMock(return_value=mock_response)

        req = DeviceRegisterRequest(
            device_name="staging_test_device",
            device_token="staging_secret_token",
            username=None,
        )

        with patch("src.Models.Devices.device_repository.get_settings") as mock_settings:
            mock_settings.return_value.environment = "staging"

            result = asyncio.run(repo.register_or_update(req))

            assert result is not None
            called_updates = mock_query.update.call_args[0][0]
            assert called_updates["device_token_hash"] == hash_device_token("staging_secret_token")
            assert called_updates["user_id"] is None

    def test_production_environment_rejects_non_dev_token_mismatch(self):
        """Verify production strictly rejects token mismatch for non-dev devices."""
        mock_db = MagicMock()
        repo = DeviceRepository(mock_db)

        existing_record = {
            "id": "prod-device-uuid",
            "name": "prod_hardware_device_1",
            "device_token_hash": hash_device_token("correct_original_token"),
            "user_id": "prod-user-1",
        }
        repo.get_by_device_id = AsyncMock(return_value=existing_record)

        req = DeviceRegisterRequest(
            device_name="prod_hardware_device_1",
            device_token="attacker_or_mismatched_token",
            username=None,
        )

        with patch("src.Models.Devices.device_repository.get_settings") as mock_settings:
            mock_settings.return_value.environment = "production"

            result = asyncio.run(repo.register_or_update(req))

            # Strict production check must return None
            assert result is None
            # Update should NOT be called
            mock_db.table.assert_not_called()

    def test_production_environment_accepts_matching_token(self):
        """Verify production updates record when token matches."""
        mock_db = MagicMock()
        repo = DeviceRepository(mock_db)

        correct_token = "valid_production_device_secret"
        valid_hash = hash_device_token(correct_token)
        existing_record = {
            "id": "prod-device-uuid-2",
            "name": "prod_hardware_device_2",
            "device_token_hash": valid_hash,
            "user_id": "existing-user",
        }
        repo.get_by_device_id = AsyncMock(return_value=existing_record)

        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.update.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_response = MagicMock()
        mock_response.data = [{"id": "prod-device-uuid-2", "user_id": "existing-user"}]
        mock_query.execute = AsyncMock(return_value=mock_response)

        req = DeviceRegisterRequest(
            device_name="prod_hardware_device_2",
            device_token=correct_token,
            username=None,
        )

        with patch("src.Models.Devices.device_repository.get_settings") as mock_settings:
            mock_settings.return_value.environment = "production"

            result = asyncio.run(repo.register_or_update(req))

            assert result is not None
            assert result["id"] == "prod-device-uuid-2"
            mock_query.update.assert_called_once()
