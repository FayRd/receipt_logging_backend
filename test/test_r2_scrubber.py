from unittest.mock import MagicMock, patch
import pytest
from botocore.exceptions import ClientError

from src.Services.r2_scrubber import get_r2_client, purge_user_r2_backups


def test_get_r2_client_unconfigured():
    """Verify get_r2_client returns None when R2 credentials are not set."""
    with patch("src.Services.r2_scrubber.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.r2_account_id = ""
        mock_settings.r2_access_key_id = ""
        mock_get_settings.return_value = mock_settings

        client = get_r2_client()
        assert client is None


def test_get_r2_client_configured():
    """Verify get_r2_client initializes boto3 client when credentials are set."""
    with patch("src.Services.r2_scrubber.get_settings") as mock_get_settings, \
         patch("boto3.client") as mock_boto3_client:
        mock_settings = MagicMock()
        mock_settings.r2_account_id = "test-account-id"
        mock_settings.r2_access_key_id = "test-access-key"
        mock_settings.r2_secret_access_key = "test-secret-key"
        mock_get_settings.return_value = mock_settings

        mock_s3 = MagicMock()
        mock_boto3_client.return_value = mock_s3

        client = get_r2_client()
        assert client is mock_s3
        mock_boto3_client.assert_called_once()
        call_kwargs = mock_boto3_client.call_args.kwargs
        assert call_kwargs["endpoint_url"] == "https://test-account-id.r2.cloudflarestorage.com"
        assert call_kwargs["aws_access_key_id"] == "test-access-key"
        assert call_kwargs["aws_secret_access_key"] == "test-secret-key"
        assert call_kwargs["region_name"] == "auto"


def test_purge_user_r2_backups_when_unconfigured():
    """Verify purge_user_r2_backups returns 0 if R2 client is None."""
    with patch("src.Services.r2_scrubber.get_r2_client", return_value=None):
        count = purge_user_r2_backups("user-abc-123")
        assert count == 0


def test_purge_user_r2_backups_empty_user_id():
    """Verify purge_user_r2_backups returns 0 if user_id is empty."""
    count = purge_user_r2_backups("")
    assert count == 0
    count_whitespace = purge_user_r2_backups("   ")
    assert count_whitespace == 0


def test_purge_user_r2_backups_filtering_and_deletion():
    """Verify purge_user_r2_backups only deletes keys matching objects/<date>/<user_id>/..."""
    target_user = "usr_target_123"
    other_user = "usr_other_456"

    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    # Mock S3 objects with various key structures
    page_1 = {
        "Contents": [
            {"Key": f"objects/2026-10-01/{target_user}/receipts/rec1.jpg"},
            {"Key": f"objects/2026-10-01/{other_user}/receipts/rec2.jpg"},
            {"Key": f"objects/2026-10-01/manifest.json"},
            {"Key": f"other_prefix/2026-10-01/{target_user}/rec3.jpg"},
        ]
    }
    page_2 = {
        "Contents": [
            {"Key": f"objects/2026-10-02/{target_user}/exports/data.csv"},
            {"Key": f"objects/2026-10-02/{target_user}/backups/dump.sql"},
        ]
    }
    mock_paginator.paginate.return_value = [page_1, page_2]

    with patch("src.Services.r2_scrubber.get_r2_client", return_value=mock_client), \
         patch("src.Services.r2_scrubber.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.r2_bucket_name = "test-bucket"
        mock_get_settings.return_value = mock_settings

        purged_count = purge_user_r2_backups(target_user)

        assert purged_count == 3
        mock_paginator.paginate.assert_called_once_with(Bucket="test-bucket", Prefix="objects/")
        mock_client.delete_objects.assert_called_once_with(
            Bucket="test-bucket",
            Delete={
                "Objects": [
                    {"Key": f"objects/2026-10-01/{target_user}/receipts/rec1.jpg"},
                    {"Key": f"objects/2026-10-02/{target_user}/exports/data.csv"},
                    {"Key": f"objects/2026-10-02/{target_user}/backups/dump.sql"},
                ]
            },
        )


def test_purge_user_r2_backups_no_matching_objects():
    """Verify purge_user_r2_backups returns 0 and does not call delete_objects if no keys match."""
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": "objects/2026-10-01/other_user/data.csv"}]}
    ]

    with patch("src.Services.r2_scrubber.get_r2_client", return_value=mock_client), \
         patch("src.Services.r2_scrubber.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.r2_bucket_name = "test-bucket"
        mock_get_settings.return_value = mock_settings

        purged_count = purge_user_r2_backups("target_user")
        assert purged_count == 0
        mock_client.delete_objects.assert_not_called()


def test_purge_user_r2_backups_batching_over_1000():
    """Verify purge_user_r2_backups batches delete_objects calls in chunks of 1000."""
    target_user = "bulk_user"
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    # 1500 matching items
    all_items = [
        {"Key": f"objects/2026-10-01/{target_user}/file_{i}.jpg"}
        for i in range(1500)
    ]
    mock_paginator.paginate.return_value = [{"Contents": all_items}]

    with patch("src.Services.r2_scrubber.get_r2_client", return_value=mock_client), \
         patch("src.Services.r2_scrubber.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.r2_bucket_name = "test-bucket"
        mock_get_settings.return_value = mock_settings

        purged_count = purge_user_r2_backups(target_user)
        assert purged_count == 1500
        assert mock_client.delete_objects.call_count == 2

        first_call = mock_client.delete_objects.call_args_list[0]
        second_call = mock_client.delete_objects.call_args_list[1]
        assert len(first_call.kwargs["Delete"]["Objects"]) == 1000
        assert len(second_call.kwargs["Delete"]["Objects"]) == 500


def test_purge_user_r2_backups_exception_handling():
    """Verify exceptions during pagination or deletion are caught and return 0."""
    mock_client = MagicMock()
    mock_client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "ListObjectsV2",
    )

    with patch("src.Services.r2_scrubber.get_r2_client", return_value=mock_client), \
         patch("src.Services.r2_scrubber.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.r2_bucket_name = "test-bucket"
        mock_get_settings.return_value = mock_settings

        purged_count = purge_user_r2_backups("usr_err")
        assert purged_count == 0


def test_purge_user_r2_backups_delete_exception_handling():
    """Verify exceptions during delete_objects are caught and return 0."""
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": "objects/2026-10-01/usr_err/data.csv"}]}
    ]
    mock_client.delete_objects.side_effect = RuntimeError("S3 Network error")

    with patch("src.Services.r2_scrubber.get_r2_client", return_value=mock_client), \
         patch("src.Services.r2_scrubber.get_settings") as mock_get_settings:
        mock_settings = MagicMock()
        mock_settings.r2_bucket_name = "test-bucket"
        mock_get_settings.return_value = mock_settings

        purged_count = purge_user_r2_backups("usr_err")
        assert purged_count == 0
