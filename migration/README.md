# Supabase Database Migration & RLS Security Architecture

This directory contains modular, idempotent SQL migration scripts implementing a **Backend Service Gateway Architecture**. All public `anon` PostgREST access to Supabase is **100% revoked/blocked**, and database access is granted strictly to our FastAPI backend using the `service_role` key.

> [!IMPORTANT]
> **Backend Credentials Configuration**: In `.env`, ensure `SUPABASE_KEY` is set to your Supabase **`service_role` secret key** (found in Supabase Dashboard -> Project Settings -> API -> `service_role` secret key).

> [!NOTE]
> This `migration/` directory is excluded from git version control via `.gitignore`.

---

## 📁 Migration Files & Execution Order

| Step | Script File | Description | Execution Order |
|---|---|---|---|
| 🔄 | `00_teardown_all.sql` | **Development Reset / Rollback**: Drops all RLS policies, triggers, functions, indexes, and tables. | Run **only** when resetting local/dev DB |
| 1️⃣ | `01_schema_tables.sql` | **Schema DDL**: Creates `users`, `devices`, `receipts`, `conversations`, and `chat_messages` tables. | First |
| 2️⃣ | `02_indexes_triggers.sql` | **Indexes & Triggers**: Creates indexes, updated_at triggers, 10-conversation cap, and `soft_delete_user` RPC. | Second |
| 3️⃣ | `03_rls_policies.sql` | **Row-Level Security**: Enables RLS on all 5 tables and defines target policies for `service_role` and `authenticated` roles. | Third |
| 4️⃣ | `04_grants_permissions.sql` | **Access Lockdown & Grants**: Revokes ALL access from public `anon` role and grants DML privileges strictly to `service_role` and `authenticated`. | Fourth |

---

## 🚀 How to Execute Migrations

### Method 1: Supabase Web Dashboard (Recommended)

1. Log in to your [Supabase Dashboard](https://supabase.com/dashboard).
2. Select your project and navigate to **SQL Editor**.
3. Create a new query, copy and paste the contents of `01_schema_tables.sql`, and click **Run**.
4. Repeat for `02_indexes_triggers.sql`, `03_rls_policies.sql`, and `04_grants_permissions.sql`.

### Method 2: Command Line via `psql`

```bash
# Obtain your connection string from Supabase Settings -> Database
psql "postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres" -f migration/01_schema_tables.sql
psql "postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres" -f migration/02_indexes_triggers.sql
psql "postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres" -f migration/03_rls_policies.sql
psql "postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres" -f migration/04_grants_permissions.sql
```

---

## 🧹 How to Reset / Roll Back (Development Reset)

To wipe the database schemas and start completely fresh in development:

1. Open **Supabase Dashboard -> SQL Editor**.
2. Run `00_teardown_all.sql`.
3. Re-run `01_schema_tables.sql`, `02_indexes_triggers.sql`, `03_rls_policies.sql`, and `04_grants_permissions.sql`.
