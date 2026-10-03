import csv
import hashlib
import json
import os
import shutil
import tempfile
import pytest

from scripts.scrub_deleted_user_from_backups import (
    scrub_users_csv,
    scrub_devices_csv,
    scrub_user_from_all_backups,
    _recompute_manifest_checksum,
)


@pytest.fixture
def backup_fixture():
    """Create a temporary backup directory structure with users.csv, devices.csv, and manifest.json."""
    tmp_root = tempfile.mkdtemp(prefix="test_backup_root_")
    backup_dir = os.path.join(tmp_root, "backup_20261001_120000")
    os.makedirs(backup_dir, exist_ok=True)

    users_csv_path = os.path.join(backup_dir, "users.csv")
    devices_csv_path = os.path.join(backup_dir, "devices.csv")
    manifest_path = os.path.join(backup_dir, "manifest.json")

    user_a_id = "11111111-1111-1111-1111-111111111111"
    user_b_id = "22222222-2222-2222-2222-222222222222"

    users_rows = [
        {
            "id": user_a_id,
            "username": "user_alpha",
            "email": "alpha@example.com",
            "password": "hashed_pass_a",
            "country_code": "+1",
            "mobile_number": "5551234",
            "avatar_image_path": f"{user_a_id}/avatar_images",
            "deleted_at": "",
        },
        {
            "id": user_b_id,
            "username": "user_beta",
            "email": "beta@example.com",
            "password": "hashed_pass_b",
            "country_code": "+44",
            "mobile_number": "7778888",
            "avatar_image_path": f"{user_b_id}/avatar_images",
            "deleted_at": "",
        },
    ]

    with open(users_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(users_rows[0].keys()))
        writer.writeheader()
        writer.writerows(users_rows)

    with open(users_csv_path, "rb") as f:
        users_hash = hashlib.sha256(f.read()).hexdigest()

    devices_rows = [
        {
            "id": "dev-row-1",
            "name": "DEV-ALPHA-PHONE",
            "device_id": "DEV-ALPHA-PHONE",
            "fcm_token": "fcm_token_alpha_secret",
            "user_id": user_a_id,
        },
        {
            "id": "dev-row-2",
            "name": "DEV-BETA-PHONE",
            "device_id": "DEV-BETA-PHONE",
            "fcm_token": "fcm_token_beta_secret",
            "user_id": user_b_id,
        },
    ]

    with open(devices_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(devices_rows[0].keys()))
        writer.writeheader()
        writer.writerows(devices_rows)

    with open(devices_csv_path, "rb") as f:
        devices_hash = hashlib.sha256(f.read()).hexdigest()

    manifest_data = {
        "backup_timestamp_utc": "2026-10-01T12:00:00+00:00",
        "tables": {
            "users": {
                "file_name": "users.csv",
                "sha256": users_hash,
                "file_size_bytes": os.path.getsize(users_csv_path),
            },
            "devices": {
                "file_name": "devices.csv",
                "sha256": devices_hash,
                "file_size_bytes": os.path.getsize(devices_csv_path),
            },
        },
    }

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)

    yield {
        "root": tmp_root,
        "backup_dir": backup_dir,
        "users_csv": users_csv_path,
        "devices_csv": devices_csv_path,
        "manifest": manifest_path,
        "user_a_id": user_a_id,
        "user_b_id": user_b_id,
    }

    shutil.rmtree(tmp_root, ignore_errors=True)


def test_scrub_users_csv(backup_fixture):
    user_a = backup_fixture["user_a_id"]
    user_b = backup_fixture["user_b_id"]
    root = backup_fixture["root"]
    manifest_path = backup_fixture["manifest"]

    with open(manifest_path, "r", encoding="utf-8") as f:
        initial_manifest = json.load(f)
    initial_user_sha = initial_manifest["tables"]["users"]["sha256"]

    # Scrub user A
    scrubbed = scrub_users_csv(user_a, backup_root=root)
    assert scrubbed == 1

    # Read back users.csv
    with open(backup_fixture["users_csv"], "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    row_a = next(r for r in rows if r["id"] == user_a)
    row_b = next(r for r in rows if r["id"] == user_b)

    # Verify user A is tombstoned
    assert row_a["email"] == f"deleted_{user_a}@deleted.local"
    assert row_a["username"] == f"deleted_{user_a}"
    assert row_a["country_code"] == ""
    assert row_a["mobile_number"] == ""
    assert row_a["avatar_image_path"] == ""
    assert row_a["deleted_at"] != ""

    # Verify user B is untouched
    assert row_b["email"] == "beta@example.com"
    assert row_b["username"] == "user_beta"
    assert row_b["country_code"] == "+44"
    assert row_b["mobile_number"] == "7778888"

    # Verify manifest updated
    with open(manifest_path, "r", encoding="utf-8") as f:
        updated_manifest = json.load(f)
    new_user_sha = updated_manifest["tables"]["users"]["sha256"]
    assert new_user_sha != initial_user_sha

    # Verify sha matches actual file
    with open(backup_fixture["users_csv"], "rb") as f:
        expected_sha = hashlib.sha256(f.read()).hexdigest()
    assert new_user_sha == expected_sha


def test_scrub_devices_csv(backup_fixture):
    user_a = backup_fixture["user_a_id"]
    user_b = backup_fixture["user_b_id"]
    root = backup_fixture["root"]
    manifest_path = backup_fixture["manifest"]

    hashed_dev_a = hashlib.sha256(b"DEV-ALPHA-PHONE-SALT").hexdigest()

    # Scrub device linked to user A
    scrubbed = scrub_devices_csv(user_a, hashed_dev_a, backup_root=root)
    assert scrubbed == 1

    # Read back devices.csv
    with open(backup_fixture["devices_csv"], "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    row_a = next(r for r in rows if r["id"] == "dev-row-1")
    row_b = next(r for r in rows if r["id"] == "dev-row-2")

    # Verify device A is pseudonymized
    assert row_a["device_id"] == hashed_dev_a
    assert row_a["name"] == hashed_dev_a
    assert row_a["fcm_token"] == ""
    assert row_a["user_id"] == ""

    # Verify device B is untouched
    assert row_b["device_id"] == "DEV-BETA-PHONE"
    assert row_b["fcm_token"] == "fcm_token_beta_secret"
    assert row_b["user_id"] == user_b

    # Verify manifest updated
    with open(manifest_path, "r", encoding="utf-8") as f:
        updated_manifest = json.load(f)
    new_device_sha = updated_manifest["tables"]["devices"]["sha256"]
    with open(backup_fixture["devices_csv"], "rb") as f:
        expected_sha = hashlib.sha256(f.read()).hexdigest()
    assert new_device_sha == expected_sha


def test_scrub_user_from_all_backups_combined(backup_fixture):
    user_a = backup_fixture["user_a_id"]
    root = backup_fixture["root"]
    hashed_dev = hashlib.sha256(b"HASHED-DEV-123").hexdigest()

    results = scrub_user_from_all_backups(
        user_id=user_a,
        hashed_device_ids=[hashed_dev],
        backup_root=root,
    )
    assert results["users_scrubbed"] == 1
    assert results["devices_scrubbed"] == 1
