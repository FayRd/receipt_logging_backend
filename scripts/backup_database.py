#!/usr/bin/env python3
"""
Full Database CSV Backup Tool
=============================
Exports Supabase PostgreSQL database tables into structured CSV files within
receipt_logging_backend/database/backup_YYYYMMDD_HHMMSS/.

Tables backed up:
  - users
  - devices
  - receipts
  - conversations
  - chat_messages
  - forget_password

Key Features:
  - Full Snapshot: Backs up all tables including soft-deleted rows by default.
  - Encryption Preservation: Retains raw AES-256-GCM ciphertexts by default for safe restoration.
  - Optional Decryption: Pass --decrypt to export human-readable plaintexts using DATA_ENCRYPTION_KEY.
  - Manifest Generation: Generates manifest.json with row counts, execution time, and SHA-256 checksums.
  - Chunked Pagination: Iterates in configurable batches (default 500) to avoid Supabase API row caps.
  - Dry-Run Support: Pass --dry-run to test connectivity and row counts without file writes.

Usage:
    python scripts/backup_database.py
    python scripts/backup_database.py --decrypt
    python scripts/backup_database.py --dry-run
    python scripts/backup_database.py --tables receipts users --exclude-deleted
    python scripts/backup_database.py --output-dir path/to/custom_backup

NOTE: This script is in .gitignore. Backups contain sensitive data and must be stored securely.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

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
sys.path.insert(0, BACKEND_ROOT)

from src.Infrastructure.crypto import CryptoEngine, get_crypto_engine
from src.Infrastructure.key_vault import KeyVault
from src.config import get_settings

ALL_TABLES = [
    "users",
    "devices",
    "receipts",
    "conversations",
    "chat_messages",
    "forget_password",
]

SOFT_DELETE_TABLES = {"users", "devices", "receipts", "conversations"}


def load_env_defaults() -> tuple[str, str, str, str]:
    """Load configuration defaults from .env, Settings, or environment variables."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY", "")
    enc_key = os.environ.get("DATA_ENCRYPTION_KEY", "")
    env_name = os.environ.get("ENVIRONMENT", "development")
    try:
        settings = get_settings()
        url = url or settings.supabase_url or ""
        key = key or settings.supabase_key or ""
        enc_key = enc_key or settings.data_encryption_key or ""
        env_name = settings.environment or env_name
    except Exception:
        pass
    return url, key, enc_key, env_name


def compute_sha256(filepath: str) -> str:
    """Compute the SHA-256 hexadecimal digest of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def serialize_cell(val: Any) -> str:
    """Convert arbitrary Python/JSON values into clean CSV string representations."""
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def decrypt_row(
    table: str,
    row: dict[str, Any],
    crypto: CryptoEngine,
    user_deks: Optional[dict[str, bytes]] = None,
    conv_user_map: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Conditionally decrypt encrypted columns for a row, supporting both global KEK and per-user DEKs."""
    decrypted_row = dict(row)
    enc_version = decrypted_row.get("enc_version", 0)

    # Determine user DEK if applicable
    dek: Optional[bytes] = None
    if user_deks:
        user_id = None
        if table in ("users",):
            user_id = str(decrypted_row.get("id") or "")
        elif table in ("receipts", "conversations"):
            user_id = str(decrypted_row.get("user_id") or "")
        elif table == "chat_messages":
            conv_id = str(decrypted_row.get("conversation_id") or "")
            if conv_user_map and conv_id in conv_user_map:
                user_id = conv_user_map[conv_id]
        if user_id:
            dek = user_deks.get(user_id)

    if table == "receipts":
        raw = decrypted_row.get("receipt")
        if raw is not None:
            try:
                if enc_version == 1 and dek is not None:
                    decrypted_row["receipt"] = crypto.safe_decrypt_json_with_dek(
                        raw, dek, context="receipts.receipt", fallback=raw
                    )
                else:
                    decrypted_row["receipt"] = crypto.safe_decrypt_json(
                        raw, context="receipts.receipt", fallback=raw
                    )
            except Exception as e:
                decrypted_row["receipt_decryption_error"] = str(e)

    elif table == "conversations":
        raw = decrypted_row.get("title")
        if raw is not None:
            try:
                if enc_version == 1 and dek is not None:
                    decrypted_row["title"] = crypto.safe_decrypt_text_with_dek(
                        raw, dek, context="conversations.title", fallback=raw
                    )
                else:
                    decrypted_row["title"] = crypto.safe_decrypt_text(
                        raw, context="conversations.title", fallback=raw
                    )
            except Exception as e:
                decrypted_row["title_decryption_error"] = str(e)

    elif table == "chat_messages":
        raw = decrypted_row.get("content")
        if raw is not None:
            try:
                if enc_version == 1 and dek is not None:
                    decrypted_row["content"] = crypto.safe_decrypt_text_with_dek(
                        raw, dek, context="chat_messages.content", fallback=raw
                    )
                else:
                    decrypted_row["content"] = crypto.safe_decrypt_text(
                        raw, context="chat_messages.content", fallback=raw
                    )
            except Exception as e:
                decrypted_row["content_decryption_error"] = str(e)

    elif table == "users":
        raw_cats = decrypted_row.get("custom_categories")
        if raw_cats is not None:
            try:
                if enc_version == 1 and dek is not None:
                    decrypted_row["custom_categories"] = crypto.safe_decrypt_json_with_dek(
                        raw_cats, dek, context="users.custom_categories", fallback=raw_cats
                    )
                else:
                    decrypted_row["custom_categories"] = crypto.safe_decrypt_json(
                        raw_cats, context="users.custom_categories", fallback=raw_cats
                    )
            except Exception as e:
                decrypted_row["custom_categories_decryption_error"] = str(e)

        raw_prefs = decrypted_row.get("preferences")
        if raw_prefs is not None:
            try:
                if enc_version == 1 and dek is not None:
                    decrypted_row["preferences"] = crypto.safe_decrypt_json_with_dek(
                        raw_prefs, dek, context="users.preferences", fallback=raw_prefs
                    )
                else:
                    decrypted_row["preferences"] = crypto.safe_decrypt_json(
                        raw_prefs, context="users.preferences", fallback=raw_prefs
                    )
            except Exception as e:
                decrypted_row["preferences_decryption_error"] = str(e)

        raw_cc = decrypted_row.get("country_code")
        if raw_cc is not None:
            try:
                if enc_version == 1 and dek is not None:
                    decrypted_row["country_code"] = crypto.safe_decrypt_text_with_dek(
                        raw_cc, dek, context="users.country_code", fallback=raw_cc
                    )
                else:
                    decrypted_row["country_code"] = crypto.safe_decrypt_text(
                        raw_cc, context="users.country_code", fallback=raw_cc
                    )
            except Exception as e:
                decrypted_row["country_code_decryption_error"] = str(e)

        raw_mob = decrypted_row.get("mobile_number")
        if raw_mob is not None:
            try:
                if enc_version == 1 and dek is not None:
                    decrypted_row["mobile_number"] = crypto.safe_decrypt_text_with_dek(
                        raw_mob, dek, context="users.mobile_number", fallback=raw_mob
                    )
                else:
                    decrypted_row["mobile_number"] = crypto.safe_decrypt_text(
                        raw_mob, context="users.mobile_number", fallback=raw_mob
                    )
            except Exception as e:
                decrypted_row["mobile_number_decryption_error"] = str(e)

    return decrypted_row


def fetch_table_rows(
    client,
    table: str,
    batch_size: int = 500,
    exclude_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Paginate and fetch all records from a Supabase PostgreSQL table."""
    all_rows: list[dict[str, Any]] = []
    offset = 0

    while True:
        query = client.table(table).select("*")
        if exclude_deleted and table in SOFT_DELETE_TABLES:
            query = query.is_("deleted_at", "null")

        res = query.range(offset, offset + batch_size - 1).execute()
        rows = res.data or []
        if not rows:
            break

        all_rows.extend(rows)
        offset += len(rows)

        if len(rows) < batch_size:
            break

    return all_rows


def export_table_to_csv(
    table: str,
    rows: list[dict[str, Any]],
    output_dir: str,
    decrypt: bool,
    crypto: Optional[CryptoEngine],
    user_deks: Optional[dict[str, bytes]] = None,
    conv_user_map: Optional[dict[str, str]] = None,
) -> tuple[str, int, str]:
    """Write table rows to a CSV file and return (filepath, row_count, sha256)."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{table}.csv")

    if not rows:
        # Write empty CSV with dummy header or 0 bytes
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            f.write("# empty table\n")
        sha256 = compute_sha256(filepath)
        return filepath, 0, sha256

    # If decrypting, transform rows
    processed_rows: list[dict[str, Any]] = []
    if decrypt and crypto:
        for r in rows:
            processed_rows.append(
                decrypt_row(
                    table,
                    r,
                    crypto,
                    user_deks=user_deks,
                    conv_user_map=conv_user_map,
                )
            )
    else:
        processed_rows = rows

    # Collect union of all column names across all rows
    fieldnames: list[str] = []
    seen: set[str] = set()
    for r in processed_rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)

    # Write CSV
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL, extrasaction="ignore"
        )
        writer.writeheader()
        for r in processed_rows:
            serialized = {k: serialize_cell(r.get(k)) for k in fieldnames}
            writer.writerow(serialized)

    sha256 = compute_sha256(filepath)
    return filepath, len(processed_rows), sha256


def main() -> None:
    default_url, default_key, default_enc_key, default_env = load_env_defaults()

    parser = argparse.ArgumentParser(
        description="SancFund Supabase Database CSV Backup Tool"
    )
    parser.add_argument("--supabase-url", default=default_url, help="Supabase project URL")
    parser.add_argument("--service-role-key", default=default_key, help="Supabase API key (service-role or anon)")
    parser.add_argument("--data-encryption-key", default=default_enc_key, help="Base64 AES-256 data encryption key")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Custom output directory (defaults to receipt_logging_backend/database/backup_YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=ALL_TABLES,
        help=f"List of tables to backup (default: {' '.join(ALL_TABLES)})",
    )
    parser.add_argument(
        "--exclude-deleted",
        action="store_true",
        help="Exclude soft-deleted records (deleted_at IS NOT NULL)",
    )
    parser.add_argument(
        "--decrypt",
        action="store_true",
        help="Decrypt encrypted columns (receipts, titles, chats) into plaintext CSVs",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Pagination chunk size per database query (default 500)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate backup, count rows, and test connectivity without writing files",
    )
    args = parser.parse_args()

    start_time = time.time()
    now_utc = datetime.now(timezone.utc)
    timestamp_folder = f"backup_{now_utc.strftime('%Y%m%d_%H%M%S')}"

    print("=" * 70)
    print("  SANCFUND DATABASE CSV BACKUP UTILITY")
    print(f"  Mode: {'DRY RUN (Simulation)' if args.dry_run else 'LIVE EXPORT'}")
    print(f"  Data Protection: {'DECRYPTED PLAINTEXT (Warning: PII unencrypted)' if args.decrypt else 'ENCRYPTED CIPHERTEXT (Secure at Rest)'}")
    print("=" * 70)

    url = (args.supabase_url or "").strip()
    key = (args.service_role_key or "").strip()

    if not url:
        if sys.stdin.isatty():
            url = input("Supabase Project URL: ").strip()
        else:
            print("ERROR: Supabase URL is required (--supabase-url or SUPABASE_URL). Exiting.")
            sys.exit(1)
    if not key:
        if sys.stdin.isatty():
            key = input("Supabase Service-Role / API Key: ").strip()
        else:
            print("ERROR: Supabase Service-Role Key is required (--service-role-key or SUPABASE_SERVICE_ROLE_KEY). Exiting.")
            sys.exit(1)

    if not url or not key:
        print("ERROR: Supabase URL and Key are required. Exiting.")
        sys.exit(1)

    # Initialize Supabase client
    from supabase import create_client

    client = create_client(url, key)

    # Initialize CryptoEngine and load user DEKs if decrypt requested
    crypto: Optional[CryptoEngine] = None
    user_deks: dict[str, bytes] = {}
    conv_user_map: dict[str, str] = {}
    if args.decrypt:
        enc_key = (args.data_encryption_key or default_enc_key or "").strip()
        if not enc_key:
            enc_key = input("DATA_ENCRYPTION_KEY (base64): ").strip()
        if not enc_key:
            print("ERROR: --decrypt requested but no DATA_ENCRYPTION_KEY provided. Aborting.")
            sys.exit(1)
        crypto = CryptoEngine(key=enc_key)
        print("\n⚠️  WARNING: --decrypt mode active. Exported CSVs will contain UNENCRYPTED")
        print("   personal receipts, merchant names, and private user messages.")
        print("   Treat the destination folder as strictly confidential.\n")

        # Load per-user DEKs from user_keys table
        try:
            print("🔑 Loading and unwrapping user DEKs from user_keys...", end=" ", flush=True)
            kv = KeyVault(kek=crypto._key_bytes)
            keys_res = client.table("user_keys").select("user_id, encrypted_dek").execute()
            key_rows = keys_res.data or []
            for kr in key_rows:
                u_id = kr.get("user_id")
                enc_dek = kr.get("encrypted_dek")
                if u_id and enc_dek:
                    try:
                        user_deks[str(u_id)] = kv.unwrap_dek(enc_dek)
                    except Exception as dek_err:
                        print(f"\n   [Warning] Failed to unwrap DEK for user {u_id}: {dek_err}")
            print(f"[OK] Loaded {len(user_deks)} DEKs.")
        except Exception as exc:
            print(f"[Warning] Could not load user_keys: {exc}")

        # If chat_messages is among target tables, build conversation_id -> user_id map
        if "chat_messages" in args.tables:
            try:
                convs_res = client.table("conversations").select("id, user_id").execute()
                for c in (convs_res.data or []):
                    c_id = c.get("id")
                    u_id = c.get("user_id")
                    if c_id and u_id:
                        conv_user_map[str(c_id)] = str(u_id)
            except Exception as exc:
                print(f"[Warning] Could not load conversation mappings: {exc}")

    # Resolve output directory
    if args.output_dir:
        target_dir = os.path.abspath(args.output_dir)
    else:
        target_dir = os.path.abspath(
            os.path.join(BACKEND_ROOT, "database", timestamp_folder)
        )

    tables_to_dump = [t for t in args.tables if t in ALL_TABLES]
    invalid_tables = set(args.tables) - set(ALL_TABLES)
    if invalid_tables:
        print(f"Warning: Unknown tables ignored: {', '.join(invalid_tables)}")

    if not tables_to_dump:
        print("ERROR: No valid tables selected for backup. Exiting.")
        sys.exit(1)

    print(f"Target Tables: {', '.join(tables_to_dump)}")
    print(f"Destination:   {target_dir}\n")

    manifest_tables: dict[str, Any] = {}
    total_rows = 0

    for table in tables_to_dump:
        print(f"📦 Processing table '{table}'...", end=" ", flush=True)
        try:
            rows = fetch_table_rows(
                client=client,
                table=table,
                batch_size=args.batch_size,
                exclude_deleted=args.exclude_deleted,
            )
            count = len(rows)
            total_rows += count

            if args.dry_run:
                print(f"[OK] Found {count} rows (dry-run, not written)")
                manifest_tables[table] = {
                    "row_count": count,
                    "file_name": f"{table}.csv",
                    "file_size_bytes": 0,
                    "sha256": "dry-run",
                }
            else:
                filepath, written_count, sha256 = export_table_to_csv(
                    table=table,
                    rows=rows,
                    output_dir=target_dir,
                    decrypt=args.decrypt,
                    crypto=crypto,
                    user_deks=user_deks,
                    conv_user_map=conv_user_map,
                )
                file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                print(f"[OK] Wrote {written_count} rows ({file_size:,} bytes)")
                manifest_tables[table] = {
                    "row_count": written_count,
                    "file_name": f"{table}.csv",
                    "file_size_bytes": file_size,
                    "sha256": sha256,
                }
        except Exception as exc:
            print(f"[FAILED]: {exc}")
            manifest_tables[table] = {
                "error": str(exc),
                "row_count": 0,
                "file_name": f"{table}.csv",
            }

    duration = time.time() - start_time

    # Generate manifest.json if not dry-run
    if not args.dry_run and os.path.exists(target_dir):
        manifest_data = {
            "backup_timestamp_utc": now_utc.isoformat(),
            "execution_duration_seconds": round(duration, 3),
            "environment": default_env,
            "decrypted": args.decrypt,
            "exclude_deleted": args.exclude_deleted,
            "batch_size": args.batch_size,
            "total_tables": len(tables_to_dump),
            "total_rows": total_rows,
            "tables": manifest_tables,
        }
        manifest_path = os.path.join(target_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
        print(f"\n📋 Manifest generated: {manifest_path}")

    print("\n" + "=" * 70)
    print("BACKUP SUMMARY:")
    print(f"  Status:          {'SUCCESS (Dry-Run)' if args.dry_run else 'SUCCESS'}")
    print(f"  Total Tables:    {len(tables_to_dump)}")
    print(f"  Total Rows:      {total_rows}")
    print(f"  Execution Time:  {duration:.2f} seconds")
    print(f"  Output Folder:   {target_dir}")
    print("=" * 70)


if __name__ == "__main__":
    main()
