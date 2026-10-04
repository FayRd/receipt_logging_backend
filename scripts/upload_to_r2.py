#!/usr/bin/env python3
"""
Cloudflare R2 Directory Uploader
================================
Uploads a local directory tree to Cloudflare R2 object storage under a target prefix.

Features:
  - Uses boto3 S3 client configured with Cloudflare R2 endpoint.
  - Automatically preserves relative folder hierarchy within the target prefix.
  - Normalizes path separators to forward slashes for S3 object keys.
  - Detects Content-Type via mimetypes for proper metadata in R2.
  - Supports --dry-run simulation without transferring bytes.
  - Displays progress, file counts, byte totals, and execution duration.

Usage:
    python scripts/upload_to_r2.py --source /path/to/backup --r2-prefix db/backup_20261005
    python scripts/upload_to_r2.py --source /path/to/backup --r2-prefix db/backup_20261005 --dry-run
    python scripts/upload_to_r2.py --source /path/to/backup --r2-prefix db/backup_20261005 --bucket custom-bucket
"""

import argparse
import mimetypes
import os
import sys
import time
from typing import Optional
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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


def load_r2_credentials(
    account_id_arg: Optional[str] = None,
    access_key_arg: Optional[str] = None,
    secret_key_arg: Optional[str] = None,
    bucket_arg: Optional[str] = None,
) -> tuple[str, str, str, str]:
    """Resolve R2 credentials from CLI args, environment variables, or Settings."""
    # 1. Start with environment variables
    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    bucket = os.environ.get("R2_BUCKET_NAME", "").strip()

    # 2. Try Settings as fallback
    try:
        from src.config import get_settings
        settings = get_settings()
        account_id = account_id or getattr(settings, "r2_account_id", "")
        access_key = access_key or getattr(settings, "r2_access_key_id", "")
        secret_key = secret_key or getattr(settings, "r2_secret_access_key", "")
        bucket = bucket or getattr(settings, "r2_bucket_name", "")
    except Exception:
        pass

    # 3. Explicit CLI arguments take highest precedence
    account_id = (account_id_arg or account_id).strip()
    access_key = (access_key_arg or access_key).strip()
    secret_key = (secret_key_arg or secret_key).strip()
    bucket = (bucket_arg or bucket or "sancfund-backups").strip()

    return account_id, access_key, secret_key, bucket


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


def upload_directory_to_r2(
    source_dir: str,
    r2_prefix: str,
    bucket: str,
    account_id: str,
    access_key: str,
    secret_key: str,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Walk local directory and upload files to R2 under r2_prefix.
    
    Returns (files_count, total_bytes).
    """
    if not os.path.exists(source_dir):
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")
    if not os.path.isdir(source_dir):
        raise NotADirectoryError(f"Source path is not a directory: {source_dir}")

    s3_client = None
    if not dry_run:
        s3_client = get_r2_client(account_id, access_key, secret_key)

    prefix_clean = r2_prefix.strip("/")
    files_to_upload: list[tuple[str, str, int]] = []

    for root, _, files in os.walk(source_dir):
        for file_name in sorted(files):
            abs_path = os.path.join(root, file_name)
            rel_path = os.path.relpath(abs_path, source_dir).replace(os.sep, "/")
            r2_key = f"{prefix_clean}/{rel_path}" if prefix_clean else rel_path
            file_size = os.path.getsize(abs_path)
            files_to_upload.append((abs_path, r2_key, file_size))

    if not files_to_upload:
        print(f"ℹ️  No files found in source directory: {source_dir}")
        return 0, 0

    print(f"📦 Found {len(files_to_upload)} file(s) to transfer:")
    total_bytes = 0
    uploaded_count = 0

    for idx, (local_path, r2_key, size) in enumerate(files_to_upload, start=1):
        content_type, _ = mimetypes.guess_type(local_path)
        content_type = content_type or "application/octet-stream"

        if dry_run:
            print(f"  [{idx}/{len(files_to_upload)}] [DRY RUN] Would upload: {r2_key} ({size:,} bytes, {content_type})")
        else:
            print(f"  [{idx}/{len(files_to_upload)}] Uploading: {r2_key} ({size:,} bytes)...", end=" ", flush=True)
            try:
                extra_args = {"ContentType": content_type}
                s3_client.upload_file(
                    Filename=local_path,
                    Bucket=bucket,
                    Key=r2_key,
                    ExtraArgs=extra_args,
                )
                print("[OK]")
            except ClientError as exc:
                print(f"[FAILED: {exc}]")
                raise exc

        total_bytes += size
        uploaded_count += 1

    return uploaded_count, total_bytes


def main():
    parser = argparse.ArgumentParser(
        description="Upload a directory tree to Cloudflare R2 under a specified prefix."
    )
    parser.add_argument("--source", required=True, help="Local directory path to upload")
    parser.add_argument("--r2-prefix", required=True, help="Target key prefix in Cloudflare R2 (e.g., db/backup_20261005)")
    parser.add_argument("--bucket", default=None, help="Target R2 bucket name (default: env R2_BUCKET_NAME or sancfund-backups)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate upload without writing to R2")
    parser.add_argument("--account-id", default=None, help="Cloudflare Account ID (overrides R2_ACCOUNT_ID)")
    parser.add_argument("--access-key-id", default=None, help="Cloudflare R2 Access Key ID (overrides R2_ACCESS_KEY_ID)")
    parser.add_argument("--secret-access-key", default=None, help="Cloudflare R2 Secret Access Key (overrides R2_SECRET_ACCESS_KEY)")

    args = parser.parse_args()

    account_id, access_key, secret_key, bucket = load_r2_credentials(
        account_id_arg=args.account_id,
        access_key_arg=args.access_key_id,
        secret_key_arg=args.secret_access_key,
        bucket_arg=args.bucket,
    )

    print("=" * 70)
    print("  CLOUDFLARE R2 DIRECTORY UPLOAD UTILITY")
    print(f"  Mode:        {'DRY RUN (Simulation)' if args.dry_run else 'LIVE UPLOAD'}")
    print(f"  Source:      {os.path.abspath(args.source)}")
    print(f"  R2 Bucket:   {bucket}")
    print(f"  R2 Prefix:   {args.r2_prefix.strip('/')}/")
    print("=" * 70)

    if not args.dry_run:
        if not account_id or not access_key or not secret_key:
            print("ERROR: Missing Cloudflare R2 credentials.", file=sys.stderr)
            print("Please set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY in environment or pass via CLI.", file=sys.stderr)
            sys.exit(1)

    start_time = time.time()
    try:
        count, total_bytes = upload_directory_to_r2(
            source_dir=args.source,
            r2_prefix=args.r2_prefix,
            bucket=bucket,
            account_id=account_id,
            access_key=access_key,
            secret_key=secret_key,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"\n❌ Upload failed: {exc}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - start_time
    print("-" * 70)
    print(f"✅ {'Simulation' if args.dry_run else 'Upload'} Completed Successfully!")
    print(f"  Total Files:       {count}")
    print(f"  Total Transferred: {total_bytes:,} bytes ({format_bytes(total_bytes)})")
    print(f"  Duration:          {elapsed:.2f}s")
    print("=" * 70)


if __name__ == "__main__":
    main()
