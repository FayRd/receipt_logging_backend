import csv
import glob
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from src.Infrastructure.logger import get_logger

logger = get_logger("Services.backup_scrubber")

DEFAULT_BACKUP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "database")
)


def _recompute_manifest_checksum(backup_dir: str, filename: str) -> None:
    """Recomputes and updates the SHA-256 checksum and file_size_bytes for filename in manifest.json."""
    manifest_path = os.path.join(backup_dir, "manifest.json")
    csv_path = os.path.join(backup_dir, filename)
    if not os.path.exists(manifest_path) or not os.path.exists(csv_path):
        return

    with open(csv_path, "rb") as f:
        content = f.read()
        new_hash = hashlib.sha256(content).hexdigest()

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        table_key = filename.replace(".csv", "")
        if "tables" in manifest and table_key in manifest["tables"]:
            manifest["tables"][table_key]["sha256"] = new_hash
            manifest["tables"][table_key]["file_size_bytes"] = len(content)
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            logger.info("Updated %s checksum in %s: %s", filename, manifest_path, new_hash[:12])
    except Exception as exc:
        logger.warning("Failed to update manifest at %s: %s", manifest_path, exc)


def scrub_users_csv(user_id: str, backup_root: Optional[str] = None) -> int:
    """Overwrites users.csv rows for user_id with tombstone values.

    Returns the total count of rows scrubbed across all backup directories.
    """
    root = backup_root or DEFAULT_BACKUP_ROOT
    clean_uid = str(user_id).strip()
    rows_scrubbed = 0
    pattern = os.path.join(root, "**", "users.csv")

    for csv_path in glob.glob(pattern, recursive=True):
        tmp_path = csv_path + ".tmp"
        modified = False
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as infile, \
                 open(tmp_path, "w", encoding="utf-8-sig", newline="") as outfile:
                reader = csv.DictReader(infile)
                if not reader.fieldnames:
                    continue
                writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
                writer.writeheader()
                for row in reader:
                    if row.get("id") == clean_uid:
                        row["email"] = f"deleted_{clean_uid}@deleted.local"
                        row["username"] = f"deleted_{clean_uid}"
                        if "country_code" in row:
                            row["country_code"] = ""
                        if "mobile_number" in row:
                            row["mobile_number"] = ""
                        if "avatar_image_path" in row:
                            row["avatar_image_path"] = ""
                        if "deleted_at" in row and not row["deleted_at"]:
                            row["deleted_at"] = datetime.now(timezone.utc).isoformat()
                        modified = True
                        rows_scrubbed += 1
                    writer.writerow(row)

            if modified:
                shutil.move(tmp_path, csv_path)
                _recompute_manifest_checksum(os.path.dirname(csv_path), "users.csv")
                logger.info("Scrubbed users.csv in %s for user_id=%s", os.path.dirname(csv_path), clean_uid)
            else:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            logger.error("Error processing %s: %s", csv_path, e)

    return rows_scrubbed


def scrub_devices_csv(user_id: str, hashed_device_id: str, backup_root: Optional[str] = None) -> int:
    """Overwrites devices.csv rows linked to user_id:

      device_id / name -> hashed_device_id  (same SHA-256 tombstone as live DB)
      fcm_token        -> ""                (clears push credential)
      user_id          -> ""                (unlinks from deleted user)

    Returns the total count of rows scrubbed across all backup directories.
    """
    root = backup_root or DEFAULT_BACKUP_ROOT
    clean_uid = str(user_id).strip()
    rows_scrubbed = 0
    pattern = os.path.join(root, "**", "devices.csv")

    for csv_path in glob.glob(pattern, recursive=True):
        tmp_path = csv_path + ".tmp"
        modified = False
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as infile, \
                 open(tmp_path, "w", encoding="utf-8-sig", newline="") as outfile:
                reader = csv.DictReader(infile)
                if not reader.fieldnames:
                    continue
                writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
                writer.writeheader()
                for row in reader:
                    if row.get("user_id") == clean_uid:
                        if "device_id" in row:
                            row["device_id"] = hashed_device_id
                        if "name" in row:
                            row["name"] = hashed_device_id
                        if "fcm_token" in row:
                            row["fcm_token"] = ""
                        row["user_id"] = ""
                        modified = True
                        rows_scrubbed += 1
                    writer.writerow(row)

            if modified:
                shutil.move(tmp_path, csv_path)
                _recompute_manifest_checksum(os.path.dirname(csv_path), "devices.csv")
                logger.info("Scrubbed devices.csv in %s for user_id=%s", os.path.dirname(csv_path), clean_uid)
            else:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            logger.error("Error processing %s: %s", csv_path, e)

    return rows_scrubbed


def scrub_user_from_all_backups(
    user_id: str,
    hashed_device_ids: Optional[list[str]] = None,
    backup_root: Optional[str] = None,
) -> dict[str, int]:
    """Scrub both users.csv and devices.csv across all backup folders for a deleted user."""
    users_count = scrub_users_csv(user_id, backup_root=backup_root)
    devices_count = 0
    if hashed_device_ids:
        for h_id in hashed_device_ids:
            if h_id:
                devices_count += scrub_devices_csv(user_id, h_id, backup_root=backup_root)
    return {
        "users_scrubbed": users_count,
        "devices_scrubbed": devices_count,
    }
