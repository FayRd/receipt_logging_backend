#!/usr/bin/env python3
"""
Cloudflare R2 Snapshot Retention Pruner
=======================================
Prunes expired database backups and object storage snapshots from Cloudflare R2
that are older than --keep-days (default 7 days).

Snapshot Patterns Inspected:
  - Database backups: `db/backup_*` (e.g. `db/backup_YYYYMMDD/` or `db/backup_YYYYMMDD_HHMMSS/`)
  - Object snapshots: `objects/*` (e.g. `objects/YYYYMMDD/` or `objects/YYYY-MM-DD/`)

Key Features:
  - Discovers snapshot partitions using S3 list_objects_v2 delimiter prefixes.
  - Automatically parses snapshot dates and calculates age relative to UTC today.
  - Deletes all objects under expired snapshot prefixes in batches of up to 1,000 keys.
  - Supports --dry-run simulation to preview expired snapshots without deleting anything.
  - Displays summary metrics of inspected, kept, and deleted snapshots and objects.

Usage:
    python scripts/prune_r2_snapshots.py --keep-days 7 --dry-run
    python scripts/prune_r2_snapshots.py --keep-days 7
    python scripts/prune_r2_snapshots.py --keep-days 14 --r2-bucket sancfund-backups
"""

import argparse
from datetime import datetime, timezone
import os
import re
import sys
import time
from typing import Any, Optional
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
    """Resolve R2 credentials from CLI, environment, or Settings."""
    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    access_key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    bucket = os.environ.get("R2_BUCKET_NAME", "").strip()

    try:
        from src.config import get_settings
        settings = get_settings()
        account_id = account_id or getattr(settings, "r2_account_id", "")
        access_key = access_key or getattr(settings, "r2_access_key_id", "")
        secret_key = secret_key or getattr(settings, "r2_secret_access_key", "")
        bucket = bucket or getattr(settings, "r2_bucket_name", "")
    except Exception:
        pass

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


def parse_snapshot_date(prefix_str: str) -> Optional[datetime]:
    """Extract and parse date from a snapshot prefix string.
    
    Supports:
      - 20261005 (YYYYMMDD)
      - 2026-10-05 (YYYY-MM-DD)
      - 2026_10_05 (YYYY_MM_DD)
      - 20261005_120000 (YYYYMMDD_HHMMSS)
    """
    # Look for YYYY-MM-DD or YYYY_MM_DD
    m_sep = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", prefix_str)
    if m_sep:
        try:
            return datetime.strptime(f"{m_sep.group(1)}{m_sep.group(2)}{m_sep.group(3)}", "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Look for 8 consecutive digits (YYYYMMDD)
    m_digits = re.search(r"(20\d{6})", prefix_str)
    if m_digits:
        try:
            return datetime.strptime(m_digits.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def discover_snapshot_prefixes(s3_client, bucket: str) -> list[str]:
    """Find all top-level snapshot partition prefixes under db/ and objects/."""
    prefixes: set[str] = set()

    for base_prefix in ["db/", "objects/"]:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix, Delimiter="/"):
            for common_prefix in page.get("CommonPrefixes", []):
                p = common_prefix.get("Prefix", "")
                if p:
                    # If base is db/ and prefix is db/backup_..., keep it
                    if base_prefix == "db/" and "backup" not in p:
                        continue
                    prefixes.add(p)

    return sorted(prefixes)


def get_objects_under_prefix(s3_client, bucket: str, prefix: str) -> list[dict[str, Any]]:
    """List all object keys and sizes under prefix."""
    paginator = s3_client.get_paginator("list_objects_v2")
    objects: list[dict[str, Any]] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            objects.append({
                "Key": item["Key"],
                "Size": item.get("Size", 0),
            })
    return objects


def prune_snapshots(
    s3_client,
    bucket: str,
    keep_days: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Inspect and prune snapshots older than keep_days.
    
    Returns summary statistics dict.
    """
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    print(f"🔍 Discovering snapshot partitions in R2 bucket '{bucket}'...")
    all_prefixes = discover_snapshot_prefixes(s3_client, bucket)

    if not all_prefixes:
        print(f"ℹ️  No snapshot partitions found under 'db/' or 'objects/' in R2 bucket '{bucket}'.")
        return {
            "inspected": 0,
            "kept": 0,
            "pruned": 0,
            "objects_deleted": 0,
            "bytes_reclaimed": 0,
        }

    print(f"📋 Found {len(all_prefixes)} snapshot partition(s) to evaluate (retention: {keep_days} day(s)):")

    prune_targets: list[tuple[str, int, datetime]] = []
    kept_count = 0

    for prefix in all_prefixes:
        snap_dt = parse_snapshot_date(prefix)
        if snap_dt is None:
            print(f"  [SKIP] {prefix} — Could not parse date partition")
            continue

        snap_date = snap_dt.date()
        age_days = (today - snap_date).days

        if age_days >= keep_days:
            print(f"  [PRUNE] {prefix} — Snapshot date: {snap_date} (age: {age_days}d >= {keep_days}d)")
            prune_targets.append((prefix, age_days, snap_dt))
        else:
            print(f"  [KEEP]  {prefix} — Snapshot date: {snap_date} (age: {age_days}d < {keep_days}d)")
            kept_count += 1

    if not prune_targets:
        print(f"\n✨ All {len(all_prefixes)} snapshot(s) are within the {keep_days}-day retention window. Nothing to prune.")
        return {
            "inspected": len(all_prefixes),
            "kept": kept_count,
            "pruned": 0,
            "objects_deleted": 0,
            "bytes_reclaimed": 0,
        }

    print(f"\n🗑️  Pruning {len(prune_targets)} expired snapshot(s)...")
    total_objects_deleted = 0
    total_bytes_reclaimed = 0

    for idx, (prefix, age_days, _) in enumerate(prune_targets, start=1):
        objects = get_objects_under_prefix(s3_client, bucket, prefix)
        obj_count = len(objects)
        bytes_sum = sum(o["Size"] for o in objects)

        if dry_run:
            print(f"  [{idx}/{len(prune_targets)}] [DRY RUN] Would delete: {prefix} ({obj_count} object(s), {bytes_sum:,} bytes)")
        else:
            print(f"  [{idx}/{len(prune_targets)}] Deleting: {prefix} ({obj_count} object(s), {bytes_sum:,} bytes)...", end=" ", flush=True)
            if obj_count > 0:
                keys = [o["Key"] for o in objects]
                # Delete objects in batches of up to 1000
                for i in range(0, len(keys), 1000):
                    batch = keys[i : i + 1000]
                    s3_client.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                    )
            print("[OK]")

        total_objects_deleted += obj_count
        total_bytes_reclaimed += bytes_sum

    return {
        "inspected": len(all_prefixes),
        "kept": kept_count,
        "pruned": len(prune_targets),
        "objects_deleted": total_objects_deleted,
        "bytes_reclaimed": total_bytes_reclaimed,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Prune Cloudflare R2 backup snapshots older than --keep-days (default 7)."
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=7,
        help="Retention period in days. Snapshots older than this will be deleted (default: 7)",
    )
    parser.add_argument(
        "--r2-bucket",
        default=None,
        help="Cloudflare R2 bucket name (default: env R2_BUCKET_NAME or sancfund-backups)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate pruning and list expired snapshots without deleting files",
    )
    parser.add_argument("--account-id", default=None, help="Cloudflare Account ID")
    parser.add_argument("--access-key-id", default=None, help="Cloudflare R2 Access Key ID")
    parser.add_argument("--secret-access-key", default=None, help="Cloudflare R2 Secret Access Key")

    args = parser.parse_args()

    account_id, access_key, secret_key, bucket = load_r2_credentials(
        account_id_arg=args.account_id,
        access_key_arg=args.access_key_id,
        secret_key_arg=args.secret_access_key,
        bucket_arg=args.r2_bucket,
    )

    print("=" * 70)
    print("  CLOUDFLARE R2 SNAPSHOT RETENTION PRUNING UTILITY")
    print(f"  Mode:            {'DRY RUN (Simulation)' if args.dry_run else 'LIVE PRUNE'}")
    print(f"  R2 Bucket:       {bucket}")
    print(f"  Retention Window: {args.keep_days} day(s)")
    print("=" * 70)

    if not account_id or not access_key or not secret_key:
        print("ERROR: Missing Cloudflare R2 credentials.", file=sys.stderr)
        print("Please set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY or pass via CLI.", file=sys.stderr)
        sys.exit(1)

    start_time = time.time()
    try:
        s3_client = get_r2_client(account_id, access_key, secret_key)
        stats = prune_snapshots(
            s3_client=s3_client,
            bucket=bucket,
            keep_days=args.keep_days,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"\n❌ Snapshot pruning failed: {exc}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - start_time
    print("-" * 70)
    print(f"✅ {'Simulation' if args.dry_run else 'Pruning'} Completed Successfully!")
    print(f"  Snapshots Inspected: {stats['inspected']}")
    print(f"  Snapshots Retained:  {stats['kept']}")
    print(f"  Snapshots Pruned:    {stats['pruned']}")
    print(f"  Objects Deleted:     {stats['objects_deleted']}")
    print(f"  Bytes Reclaimed:     {stats['bytes_reclaimed']:,} bytes ({format_bytes(stats['bytes_reclaimed'])})")
    print(f"  Duration:            {elapsed:.2f}s")
    print("=" * 70)


if __name__ == "__main__":
    main()
