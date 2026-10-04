#!/usr/bin/env python3
"""
Supabase Object Storage Mirroring Utility
=========================================
Mirrors objects from a Supabase Storage bucket (default: user-data)
into Cloudflare R2 under `objects/{date}/<path>`.

Key Features:
  - Recursively traverses all nested directories in Supabase Storage with pagination.
  - Streams/downloads objects from Supabase Storage and uploads to Cloudflare R2.
  - Automatically preserves folder structures and content types.
  - Supports --dry-run simulation to calculate object counts and byte volume without transfer.
  - Summarizes execution metrics including objects processed, bytes transferred, and duration.

Usage:
    python scripts/backup_object_storage.py
    python scripts/backup_object_storage.py --date 20261005
    python scripts/backup_object_storage.py --dry-run
    python scripts/backup_object_storage.py --supabase-bucket user-data --bucket sancfund-backups
"""

import argparse
from datetime import datetime, timezone
import mimetypes
import os
import sys
import time
from typing import Any, Optional
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from supabase import create_client, Client

# Ensure UTF-8 output on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure backend root is in sys.path
BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)


def load_credentials(
    supabase_url_arg: Optional[str] = None,
    supabase_key_arg: Optional[str] = None,
    account_id_arg: Optional[str] = None,
    access_key_arg: Optional[str] = None,
    secret_key_arg: Optional[str] = None,
    r2_bucket_arg: Optional[str] = None,
    supabase_bucket_arg: Optional[str] = None,
) -> tuple[str, str, str, str, str, str, str]:
    """Resolve Supabase and Cloudflare R2 credentials from CLI, environment, or Settings."""
    # 1. Environment variables
    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or ""
    ).strip()
    r2_account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    r2_access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    r2_secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    r2_bucket = os.environ.get("R2_BUCKET_NAME", "").strip()
    sb_bucket = os.environ.get("SUPABASE_USER_DATA_BUCKET", "user-data").strip()

    # 2. Settings fallback
    try:
        from src.config import get_settings
        settings = get_settings()
        sb_url = sb_url or getattr(settings, "supabase_url", "")
        sb_key = sb_key or getattr(settings, "supabase_key", "")
        r2_account_id = r2_account_id or getattr(settings, "r2_account_id", "")
        r2_access_key = r2_access_key or getattr(settings, "r2_access_key_id", "")
        r2_secret_key = r2_secret_key or getattr(settings, "r2_secret_access_key", "")
        r2_bucket = r2_bucket or getattr(settings, "r2_bucket_name", "")
        sb_bucket = sb_bucket or getattr(settings, "supabase_user_data_bucket", "user-data")
    except Exception:
        pass

    # 3. CLI arguments override
    sb_url = (supabase_url_arg or sb_url).strip()
    sb_key = (supabase_key_arg or sb_key).strip()
    r2_account_id = (account_id_arg or r2_account_id).strip()
    r2_access_key = (access_key_arg or r2_access_key).strip()
    r2_secret_key = (secret_key_arg or r2_secret_key).strip()
    r2_bucket = (r2_bucket_arg or r2_bucket or "sancfund-backups").strip()
    sb_bucket = (supabase_bucket_arg or sb_bucket or "user-data").strip()

    return sb_url, sb_key, r2_account_id, r2_access_key, r2_secret_key, r2_bucket, sb_bucket


def get_r2_client(account_id: str, access_key: str, secret_key: str):
    """Create a boto3 S3 client for Cloudflare R2."""
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        region_name="auto",
    )


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable representation."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def list_all_supabase_objects(supabase_client: Client, bucket_name: str, folder_path: str = "") -> list[dict[str, Any]]:
    """Recursively list all file objects under folder_path in a Supabase Storage bucket.
    
    Handles pagination using limit and offset.
    Returns list of dicts with keys: 'path', 'size', 'content_type'.
    """
    clean_folder = folder_path.strip("/")
    files: list[dict[str, Any]] = []
    bucket_proxy = supabase_client.storage.from_(bucket_name)

    limit = 100
    offset = 0

    while True:
        try:
            items = bucket_proxy.list(
                path=clean_folder if clean_folder else None,
                options={"limit": limit, "offset": offset},
            )
        except Exception as exc:
            print(f"⚠️  Error listing Supabase storage path '{clean_folder}' (offset {offset}): {exc}", file=sys.stderr)
            break

        if not items or not isinstance(items, list):
            break

        for item in items:
            name = item.get("name")
            if not name or name in (".emptyFolderPlaceholder", ".keep"):
                continue

            item_path = f"{clean_folder}/{name}" if clean_folder else name

            # Supabase folder items have no id or no metadata
            metadata = item.get("metadata")
            item_id = item.get("id")
            if item_id is None or metadata is None:
                # Recurse into subdirectory
                sub_files = list_all_supabase_objects(supabase_client, bucket_name, item_path)
                files.extend(sub_files)
            else:
                size = 0
                content_type = None
                if isinstance(metadata, dict):
                    size = metadata.get("size", 0) or 0
                    content_type = metadata.get("mimetype")

                files.append({
                    "path": item_path,
                    "size": int(size),
                    "content_type": content_type,
                })

        offset += len(items)
        if len(items) < limit:
            break

    return files


def mirror_bucket_to_r2(
    supabase_client: Client,
    supabase_bucket: str,
    r2_bucket: str,
    date_str: str,
    r2_account_id: str,
    r2_access_key: str,
    r2_secret_key: str,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Mirror all objects in Supabase bucket to Cloudflare R2 under objects/{date_str}/.
    
    Returns (objects_mirrored, total_bytes).
    """
    print(f"🔍 Scanning Supabase Storage bucket '{supabase_bucket}'...")
    objects = list_all_supabase_objects(supabase_client, supabase_bucket)

    if not objects:
        print(f"ℹ️  No objects found in Supabase Storage bucket '{supabase_bucket}'.")
        return 0, 0

    total_discovered_bytes = sum(o["size"] for o in objects)
    print(f"📦 Discovered {len(objects)} object(s) ({total_discovered_bytes:,} bytes / {format_bytes(total_discovered_bytes)}).")

    s3_client = None
    if not dry_run:
        s3_client = get_r2_client(r2_account_id, r2_access_key, r2_secret_key)

    bucket_proxy = supabase_client.storage.from_(supabase_bucket)
    total_bytes = 0
    mirrored_count = 0

    for idx, obj in enumerate(objects, start=1):
        obj_path = obj["path"]
        size = obj["size"]
        content_type = obj.get("content_type") or mimetypes.guess_type(obj_path)[0] or "application/octet-stream"
        r2_key = f"objects/{date_str}/{obj_path}"

        if dry_run:
            print(f"  [{idx}/{len(objects)}] [DRY RUN] Would mirror: {obj_path} ({size:,} bytes) -> {r2_key}")
            total_bytes += size
            mirrored_count += 1
        else:
            print(f"  [{idx}/{len(objects)}] Mirroring: {obj_path} -> {r2_key}...", end=" ", flush=True)
            try:
                # Download bytes from Supabase
                file_bytes = bucket_proxy.download(obj_path)
                actual_size = len(file_bytes)

                # Upload to Cloudflare R2
                s3_client.put_object(
                    Bucket=r2_bucket,
                    Key=r2_key,
                    Body=file_bytes,
                    ContentType=content_type,
                )
                print(f"[OK] ({actual_size:,} bytes)")
                total_bytes += actual_size
                mirrored_count += 1
            except Exception as exc:
                print(f"[FAILED: {exc}]")
                raise exc

    return mirrored_count, total_bytes


def main():
    parser = argparse.ArgumentParser(
        description="Mirror Supabase Storage bucket objects to Cloudflare R2 under objects/{date}/."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Backup date partition in YYYYMMDD format (default: UTC today)",
    )
    parser.add_argument(
        "--bucket",
        default=None,
        help="Target Cloudflare R2 bucket name (default: env R2_BUCKET_NAME or sancfund-backups)",
    )
    parser.add_argument(
        "--supabase-bucket",
        default=None,
        help="Source Supabase Storage bucket name (default: user-data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate object listing and calculate byte sizes without downloading or uploading",
    )
    parser.add_argument("--supabase-url", default=None, help="Supabase project URL")
    parser.add_argument("--supabase-key", default=None, help="Supabase Service-Role / API Key")
    parser.add_argument("--service-role-key", default=None, help="Alias for --supabase-key")
    parser.add_argument("--account-id", default=None, help="Cloudflare Account ID")
    parser.add_argument("--access-key-id", default=None, help="Cloudflare R2 Access Key ID")
    parser.add_argument("--secret-access-key", default=None, help="Cloudflare R2 Secret Access Key")

    args = parser.parse_args()

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y%m%d")
    sb_key_arg = args.supabase_key or args.service_role_key

    sb_url, sb_key, r2_account_id, r2_access_key, r2_secret_key, r2_bucket, sb_bucket = load_credentials(
        supabase_url_arg=args.supabase_url,
        supabase_key_arg=sb_key_arg,
        account_id_arg=args.account_id,
        access_key_arg=args.access_key_id,
        secret_key_arg=args.secret_access_key,
        r2_bucket_arg=args.bucket,
        supabase_bucket_arg=args.supabase_bucket,
    )

    print("=" * 70)
    print("  SUPABASE OBJECT STORAGE TO R2 MIRROR UTILITY")
    print(f"  Mode:            {'DRY RUN (Simulation)' if args.dry_run else 'LIVE MIRROR'}")
    print(f"  Backup Date:     {date_str}")
    print(f"  Supabase Bucket: {sb_bucket}")
    print(f"  R2 Bucket:       {r2_bucket}")
    print(f"  R2 Prefix:       objects/{date_str}/")
    print("=" * 70)

    if not sb_url or not sb_key:
        print("ERROR: Missing Supabase credentials.", file=sys.stderr)
        print("Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or pass via CLI.", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        if not r2_account_id or not r2_access_key or not r2_secret_key:
            print("ERROR: Missing Cloudflare R2 credentials.", file=sys.stderr)
            print("Please set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY or pass via CLI.", file=sys.stderr)
            sys.exit(1)

    start_time = time.time()
    try:
        supabase_client = create_client(sb_url, sb_key)
        count, total_bytes = mirror_bucket_to_r2(
            supabase_client=supabase_client,
            supabase_bucket=sb_bucket,
            r2_bucket=r2_bucket,
            date_str=date_str,
            r2_account_id=r2_account_id,
            r2_access_key=r2_access_key,
            r2_secret_key=r2_secret_key,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"\n❌ Object storage mirror failed: {exc}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - start_time
    print("-" * 70)
    print(f"✅ {'Simulation' if args.dry_run else 'Mirror'} Completed Successfully!")
    print(f"  Objects Processed: {count}")
    print(f"  Bytes Transferred: {total_bytes:,} bytes ({format_bytes(total_bytes)})")
    print(f"  Duration:          {elapsed:.2f}s")
    print("=" * 70)


if __name__ == "__main__":
    main()
