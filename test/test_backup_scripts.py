import os
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from scripts.upload_to_r2 import (
    load_r2_credentials,
    upload_directory_to_r2,
)
from scripts.backup_object_storage import (
    list_all_supabase_objects,
    mirror_bucket_to_r2,
)
from scripts.restore_object_storage import (
    list_r2_snapshot_objects,
    restore_r2_to_supabase,
)
from scripts.prune_r2_snapshots import (
    parse_snapshot_date,
    discover_snapshot_prefixes,
    prune_snapshots,
)


# ==============================================================================
# 1. upload_to_r2 tests
# ==============================================================================

def test_upload_to_r2_load_credentials():
    with patch.dict(os.environ, {
        "R2_ACCOUNT_ID": "env-acc",
        "R2_ACCESS_KEY_ID": "env-key",
        "R2_SECRET_ACCESS_KEY": "env-secret",
        "R2_BUCKET_NAME": "env-bucket",
    }):
        # Env vars used when CLI args are None
        acc, key, secret, bucket = load_r2_credentials()
        assert acc == "env-acc"
        assert key == "env-key"
        assert secret == "env-secret"
        assert bucket == "env-bucket"

        # CLI overrides env vars
        acc2, key2, secret2, bucket2 = load_r2_credentials(
            account_id_arg="cli-acc",
            access_key_arg="cli-key",
            secret_key_arg="cli-secret",
            bucket_arg="cli-bucket",
        )
        assert acc2 == "cli-acc"
        assert key2 == "cli-key"
        assert secret2 == "cli-secret"
        assert bucket2 == "cli-bucket"


def test_upload_directory_to_r2_dry_run(tmp_path):
    sub = tmp_path / "subfolder"
    sub.mkdir()
    file1 = tmp_path / "file1.txt"
    file1.write_text("Hello world")
    file2 = sub / "file2.json"
    file2.write_text('{"key": "value"}')

    count, total_bytes = upload_directory_to_r2(
        source_dir=str(tmp_path),
        r2_prefix="db/backup_20261005",
        bucket="test-bucket",
        account_id="acc",
        access_key="key",
        secret_key="secret",
        dry_run=True,
    )

    assert count == 2
    assert total_bytes == len("Hello world") + len('{"key": "value"}')


def test_upload_directory_to_r2_live(tmp_path):
    file1 = tmp_path / "data.csv"
    data = b"col1,col2\n1,2"
    file1.write_bytes(data)

    mock_s3 = MagicMock()
    with patch("scripts.upload_to_r2.get_r2_client", return_value=mock_s3):
        count, total_bytes = upload_directory_to_r2(
            source_dir=str(tmp_path),
            r2_prefix="db/backup_20261005",
            bucket="test-bucket",
            account_id="acc",
            access_key="key",
            secret_key="secret",
            dry_run=False,
        )

        assert count == 1
        assert total_bytes == len(data)
        mock_s3.upload_file.assert_called_once()
        args, kwargs = mock_s3.upload_file.call_args
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "db/backup_20261005/data.csv"


# ==============================================================================
# 2. backup_object_storage tests
# ==============================================================================

def test_list_all_supabase_objects_recursive():
    mock_sb = MagicMock()
    mock_storage = MagicMock()
    mock_sb.storage.from_.return_value = mock_storage

    # Root contains a file and a folder
    def list_side_effect(path=None, options=None):
        if not path:
            return [
                {"name": "root_file.txt", "id": "f1", "metadata": {"size": 100, "mimetype": "text/plain"}},
                {"name": "user123", "id": None, "metadata": None},  # folder
            ]
        elif path == "user123":
            return [
                {"name": "receipt1.jpg", "id": "f2", "metadata": {"size": 2048, "mimetype": "image/jpeg"}},
            ]
        return []

    mock_storage.list.side_effect = list_side_effect

    objects = list_all_supabase_objects(mock_sb, "user-data")
    assert len(objects) == 2
    paths = {o["path"]: o["size"] for o in objects}
    assert paths["root_file.txt"] == 100
    assert paths["user123/receipt1.jpg"] == 2048


def test_mirror_bucket_to_r2():
    mock_sb = MagicMock()
    mock_storage = MagicMock()
    mock_sb.storage.from_.return_value = mock_storage
    mock_storage.list.return_value = [
        {"name": "img.jpg", "id": "id1", "metadata": {"size": 500, "mimetype": "image/jpeg"}},
    ]
    mock_storage.download.return_value = b"fake-jpeg-bytes"

    mock_s3 = MagicMock()
    with patch("scripts.backup_object_storage.get_r2_client", return_value=mock_s3):
        count, total_bytes = mirror_bucket_to_r2(
            supabase_client=mock_sb,
            supabase_bucket="user-data",
            r2_bucket="sancfund-backups",
            date_str="20261005",
            r2_account_id="acc",
            r2_access_key="key",
            r2_secret_key="secret",
            dry_run=False,
        )

        assert count == 1
        assert total_bytes == len(b"fake-jpeg-bytes")
        mock_storage.download.assert_called_once_with("img.jpg")
        mock_s3.put_object.assert_called_once_with(
            Bucket="sancfund-backups",
            Key="objects/20261005/img.jpg",
            Body=b"fake-jpeg-bytes",
            ContentType="image/jpeg",
        )


# ==============================================================================
# 3. restore_object_storage tests
# ==============================================================================

def test_list_r2_snapshot_objects():
    mock_s3 = MagicMock()
    mock_paginator = MagicMock()
    mock_s3.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": "objects/20261005/", "Size": 0},  # Directory prefix itself
                {"Key": "objects/20261005/user1/avatar.png", "Size": 1234},
                {"Key": "objects/20261005/user2/receipt.enc", "Size": 5678},
            ]
        }
    ]

    objs = list_r2_snapshot_objects(mock_s3, "sancfund-backups", "objects/20261005/")
    assert len(objs) == 2
    assert objs[0]["Key"] == "objects/20261005/user1/avatar.png"
    assert objs[1]["Key"] == "objects/20261005/user2/receipt.enc"


def test_restore_r2_to_supabase_live():
    mock_sb = MagicMock()
    mock_storage = MagicMock()
    mock_sb.storage.from_.return_value = mock_storage

    mock_s3 = MagicMock()
    mock_paginator = MagicMock()
    mock_s3.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": "objects/20261005/u1/doc.pdf", "Size": 42},
            ]
        }
    ]
    body_mock = MagicMock()
    body_mock.read.return_value = b"pdf-content"
    mock_s3.get_object.return_value = {
        "Body": body_mock,
        "ContentType": "application/pdf",
    }

    with patch("scripts.restore_object_storage.get_r2_client", return_value=mock_s3):
        count, total_bytes = restore_r2_to_supabase(
            supabase_client=mock_sb,
            supabase_bucket="user-data",
            r2_bucket="sancfund-backups",
            date_str="20261005",
            r2_account_id="acc",
            r2_access_key="key",
            r2_secret_key="secret",
            dry_run=False,
            force=True,
        )

        assert count == 1
        assert total_bytes == len(b"pdf-content")
        mock_storage.upload.assert_called_once_with(
            path="u1/doc.pdf",
            file=b"pdf-content",
            file_options={"upsert": "true", "content-type": "application/pdf"},
        )


# ==============================================================================
# 4. prune_r2_snapshots tests
# ==============================================================================

def test_parse_snapshot_date():
    assert parse_snapshot_date("db/backup_20261005/") == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    assert parse_snapshot_date("db/backup_20261005_123456/") == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    assert parse_snapshot_date("objects/2026-10-05/") == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    assert parse_snapshot_date("objects/2026_10_05/") == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    assert parse_snapshot_date("invalid_prefix_name") is None


def test_prune_snapshots_mixed_age():
    mock_s3 = MagicMock()

    # Today is 2026-10-10
    # Snapshot 1: 2026-10-01 (9 days old -> prune if keep_days=7)
    # Snapshot 2: 2026-10-08 (2 days old -> keep)
    old_prefix = "db/backup_20261001/"
    recent_prefix = "db/backup_20261008/"

    with patch("scripts.prune_r2_snapshots.discover_snapshot_prefixes", return_value=[old_prefix, recent_prefix]), \
         patch("scripts.prune_r2_snapshots.get_objects_under_prefix") as mock_get_objs:

        mock_get_objs.return_value = [
            {"Key": f"{old_prefix}file1.csv", "Size": 100},
            {"Key": f"{old_prefix}manifest.json", "Size": 50},
        ]

        # Freeze today to 2026-10-10
        mock_now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        with patch("scripts.prune_r2_snapshots.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.strptime.side_effect = datetime.strptime

            stats = prune_snapshots(
                s3_client=mock_s3,
                bucket="sancfund-backups",
                keep_days=7,
                dry_run=False,
            )

            assert stats["inspected"] == 2
            assert stats["kept"] == 1
            assert stats["pruned"] == 1
            assert stats["objects_deleted"] == 2
            assert stats["bytes_reclaimed"] == 150

            mock_s3.delete_objects.assert_called_once_with(
                Bucket="sancfund-backups",
                Delete={
                    "Objects": [{"Key": f"{old_prefix}file1.csv"}, {"Key": f"{old_prefix}manifest.json"}],
                    "Quiet": True,
                },
            )
