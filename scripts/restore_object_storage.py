#!/usr/bin/env python3
"""
Supabase Object Storage Restore Utility
=======================================
Restores an object storage backup from Cloudflare R2 `objects/{date}/`
back into a Supabase Storage bucket (default: user-data).

Key Features:
  - Reads backup snapshot from Cloudflare R2 under `objects/{date}/`.
  - Automatically reconstructs Supabase bucket file hierarchy.
  - Prompts for confirmation before modifying Supabase storage (bypass with --force).
  - Preserves original file types via MIME detection or R2 ContentType headers.
  - Uses upsert mode so missing files are created and existing files are updated safely.
  - Supports --dry-run simulation to review files to be restored without altering Supabase.

Usage:
    python scripts/restore_object_storage.py --date 20261005 --dry-run
    python scripts/restore_object_storage.py --date 20261005
    python scripts/restore_object_storage.py --date 20261005 --force
    python scripts/restore_object_storage.py --date 20261005 --supabase-bucket user-data --r2-bucket sancfund-backups
"""

import argparse
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


def list_r2_snapshot_objects(s3_client, r2_bucket: str, prefix: str) -> list[dict[str, Any]]:
    """List all objects in R2 matching the prefix."""
    paginator = s3_client.get_paginator("list_objects_v2")
    objects: list[dict[str, Any]] = []

    for page in paginator.paginate(Bucket=r2_bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            # Skip the prefix placeholder folder itself if present
            if key == prefix or key.endswith("/"):
                continue
            objects.append({
                "Key": key,
                "Size": item.get("Size", 0),
                "LastModified": item.get("LastModified"),
            })

    return objects


def restore_r2_to_supabase(
    supabase_client: Client,
    supabase_bucket: str,
    r2_bucket: str,
    date_str: str,
    r2_account_id: str,
    r2_access_key: str,
    r2_secret_key: str,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[int, int]:
    """Restore objects from R2 objects/{date_str}/ back to Supabase bucket.
    
    Returns (restored_count, total_bytes).
    """
    prefix = f"objects/{date_str.strip('/')}/"
    s3_client = get_r2_client(r2_account_id, r2_access_key, r2_secret_key)

    print(f"🔍 Searching for backup objects in R2 bucket '{r2_bucket}' under '{prefix}'...")
    objects = list_r2_snapshot_objects(s3_client, r2_bucket, prefix)

    if not objects:
        print(f"ℹ️  No backup objects found under '{prefix}' in R2 bucket '{r2_bucket}'.")
        return 0, 0

    total_size = sum(o["Size"] for o in objects)
    print(f"📦 Found {len(objects)} object(s) ({total_size:,} bytes / {format_bytes(total_size)}) to restore.")

    if not dry_run and not force:
        print("\n" + "⚠️ " * 15)
        print(f"WARNING: Restoring {len(objects)} object(s) into Supabase bucket '{supabase_bucket}'.")
        print("This will overwrite existing files with identical paths.")
        print("⚠️ " * 15 + "\n")

        if sys.stdin.isatty():
            confirm = input(f"Type 'RESTORE' to confirm overwriting '{supabase_bucket}': ").strip()
            if confirm != "RESTORE":
                print("Restore cancelled by user. Exiting.")
                sys.exit(0)
        else:
            print("ERROR: Non-interactive session detected and --force was not specified.", file=sys.stderr)
            print("Aborting to prevent unintended data overwrite.", file=sys.stderr)
            sys.exit(1)

    bucket_proxy = supabase_client.storage.from_(supabase_bucket)
    total_bytes = 0
    restored_count = 0

    for idx, obj in enumerate(objects, start=1):
        r2_key = obj["Key"]
        size = obj["Size"]

        # Calculate relative Supabase storage path by stripping objects/{date}/
        storage_path = r2_key[len(prefix):].lstrip("/")
        if not storage_path:
            continue

        if dry_run:
            print(f"  [{idx}/{len(objects)}] [DRY RUN] Would restore: {r2_key} -> {supabase_bucket}/{storage_path} ({size:,} bytes)")
            total_bytes += size
            restored_count += 1
        else:
            print(f"  [{idx}/{len(objects)}] Restoring: {r2_key} -> {storage_path}...", end=" ", flush=True)
            try:
                # Fetch object from R2
                resp = s3_client.get_object(Bucket=r2_bucket, Key=r2_key)
                file_bytes = resp["Body"].read()
                actual_size = len(file_bytes)

                content_type = resp.get("ContentType") or mimetypes.guess_type(storage_path)[0] or "application/octet-stream"

                # Upload into Supabase Storage with upsert enabled
                bucket_proxy.upload(
                    path=storage_path,
                    file=file_bytes,
                    file_options={"upsert": "true", "content-type": content_type},
                )
                print(f"[OK] ({actual_size:,} bytes)")
                total_bytes += actual_size
                restored_count += 1
            except Exception as exc:
                print(f"[FAILED: {exc}]")
                raise exc

    return restored_count, total_bytes


def main():
    parser = argparse.ArgumentParser(
        description="Restore Supabase Storage bucket objects from Cloudflare R2 objects/{date}/."
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Backup date partition in YYYYMMDD format to restore (required)",
    )
    parser.add_argument(
        "--r2-bucket",
        default=None,
        help="Source Cloudflare R2 bucket name (default: env R2_BUCKET_NAME or sancfund-backups)",
    )
    parser.add_argument(
        "--supabase-bucket",
        default=None,
        help="Destination Supabase Storage bucket name (default: user-data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate restore and list files without downloading from R2 or uploading to Supabase",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass interactive confirmation prompt (required for headless/CI runs)",
    )
    parser.add_argument("--supabase-url", default=None, help="Supabase project URL")
    parser.add_argument("--supabase-key", default=None, help="Supabase Service-Role / API Key")
    parser.add_argument("--service-role-key", default=None, help="Alias for --supabase-key")
    parser.add_argument("--account-id", default=None, help="Cloudflare Account ID")
    parser.add_argument("--access-key-id", default=None, help="Cloudflare R2 Access Key ID")
    parser.add_argument("--secret-access-key", default=None, help="Cloudflare R2 Secret Access Key")

    args = parser.parse_args()
    sb_key_arg = args.supabase_key or args.service_role_key

    sb_url, sb_key, r2_account_id, r2_access_key, r2_secret_key, r2_bucket, sb_bucket = load_credentials(
        supabase_url_arg=args.supabase_url,
        supabase_key_arg=sb_key_arg,
        account_id_arg=args.account_id,
        access_key_arg=args.access_key_id,
        secret_key_arg=args.secret_access_key,
        r2_bucket_arg=args.r2_bucket,
        supabase_bucket_arg=args.supabase_bucket,
    )

    print("=" * 70)
    print("  CLOUDFLARE R2 TO SUPABASE STORAGE RESTORE UTILITY")
    print(f"  Mode:            {'DRY RUN (Simulation)' if args.dry_run else 'LIVE RESTORE'}")
    print(f"  Backup Date:     {args.date}")
    print(f"  R2 Bucket:       {r2_bucket}")
    print(f"  R2 Prefix:       objects/{args.date.strip('/')}/")
    print(f"  Supabase Bucket: {sb_bucket}")
    print("=" * 70)

    if not sb_url or not sb_key:
        print("ERROR: Missing Supabase credentials.", file=sys.stderr)
        print("Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or pass via CLI.", file=sys.stderr)
        sys.exit(1)

    if not r2_account_id or not r2_access_key or not r2_secret_key:
        print("ERROR: Missing Cloudflare R2 credentials.", file=sys.stderr)
        print("Please set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY or pass via CLI.", file=sys.stderr)
        sys.exit(1)

    start_time = time.time()
    try:
        supabase_client = create_client(sb_url, sb_key)
        count, total_bytes = restore_r2_to_supabase(
            supabase_client=supabase_client,
            supabase_bucket=sb_bucket,
            r2_bucket=r2_bucket,
            date_str=args.date,
            r2_account_id=r2_account_id,
            r2_access_key=r2_access_key,
            r2_secret_key=r2_secret_key,
            dry_run=args.dry_run,
            force=args.force,
        )
    except Exception as exc:
        print(f"\n❌ Object storage restore failed: {exc}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - start_time
    print("-" * 70)
    print(f"✅ {'Simulation' if args.dry_run else 'Restore'} Completed Successfully!")
    print(f"  Objects Restored:  {count}")
    print(f"  Bytes Transferred: {total_bytes:,} bytes ({format_bytes(total_bytes)})")
    print(f"  Duration:          {elapsed:.2f}s")
    print("=" * 70)


if __name__ == "__main__":
    main()
