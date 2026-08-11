# Database Migrations — Receipt Logger Backend

This directory contains the canonical SQL scripts required to initialize, configure, and maintain the PostgreSQL database on Supabase.

---

## 📜 Execution Order

Execute scripts sequentially in the **Supabase SQL Editor** (`Dashboard -> SQL Editor -> New Query`):

| File | Purpose | Key Objects Created |
| :--- | :--- | :--- |
| `00_teardown_all.sql` | **Rollback / Teardown** | Drops all RLS policies, functions, triggers, indexes, and tables. *(Use during development teardowns).* |
| `01_schema_tables.sql` | **Core Schema** | Creates `users`, `devices`, `receipts`, `conversations`, `chat_messages`, and `forget_password`. All mutable tables include `updated_at TIMESTAMPTZ`. |
| `02_indexes_triggers.sql` | **Indexes, Triggers & RPCs** | Adds performance indexes (including `updated_at` delta-sync and guest migration partial indexes), `set_updated_at_column()` auto-update trigger, conversation cap trigger, `soft_delete_user()`, and `link_device_and_migrate_guest_data()`. |
| `03_rls_policies.sql` | **Row Level Security** | Enables RLS across all 6 tables and configures `SELECT`, `INSERT`, `UPDATE` policies for `service_role`, `authenticated`, and `anon`. |
| `04_grants_permissions.sql` | **DML Privileges** | Grants schema usage, sequence permissions, and table-level `SELECT, INSERT, UPDATE, DELETE` to target roles. |

---

## ⚡ RPC Functions Reference

### `link_device_and_migrate_guest_data(p_device_id, p_device_token, p_user_id)`
Atomically links a device to a user account and adopts all un-owned guest receipts and conversations for that device in a single database transaction. `updated_at` is updated on all migrated rows via trigger:
```sql
SELECT link_device_and_migrate_guest_data(
    'MS701-0000',
    'device_token_secret_123',
    'c57d952a-f7be-4c24-a97b-86490274bb25'::UUID
);
```

### `soft_delete_user(target_user_id)`
Soft-deletes a user profile (`deleted_at = NOW()`) and unlinks active sessions from the `devices` table:
```sql
SELECT soft_delete_user('c57d952a-f7be-4c24-a97b-86490274bb25'::UUID);
```

---

## 🔄 Delta Sync & `updated_at`

All mutable tables (`users`, `devices`, `receipts`, `conversations`) include an `updated_at` column automatically maintained by the `set_updated_at_column()` trigger.

The `GET /api/v1/receipts/?updated_after=<ISO_TIMESTAMP>` endpoint uses this column to support incremental delta syncing on mobile clients:
- On initial login: fetch last 30–50 receipts via `limit`.
- On subsequent app opens: fetch only changed/new receipts via `updated_after=<last_sync_ts>`.
