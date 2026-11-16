# Botaniks Inventory System — Runbook

A plain-language guide for using the inventory app and handling the few things that can go wrong.
You do **not** need to be technical to use this. When in doubt, the data is backed up every night —
nothing you click in the app can permanently destroy your records.

> **Where to read this:** this file lives in the code repo as the master copy, but staff shouldn't
> need GitHub. Keep a copy where everyone already looks — a printed sheet by the workstation and/or
> a pinned Google Doc / shared drive page — with the contacts below and the "Needs attention" fix
> (section 1) at the top.

---

## Who to contact

- **System owner:** _______________________  (fill in name)
- **How to reach them:** _______________________  (phone / email / Slack)
- **Expected response time over the summer:** _______________________
- **If it's truly urgent and you can't reach them:** _______________________

> Tip: for almost any problem, the most useful thing you can send is **a screenshot of what you
> see on screen** plus **what you were trying to do**.

---

## The handful of things that can go wrong (and how to fix them yourself)

### 1. A Planned batch says "⚠ Needs attention"
On the **Manage Batches** page, a Planned batch may show a red **"⚠ Needs attention"** tag next to
its status. This means the system tried to complete (promote) the batch automatically but couldn't.

**Why it happens:** usually not enough raw material in stock when the batch's date arrived, or the
recipe for that product was changed/removed.

**What to do:**
1. Click the batch row to open it — the drawer shows a line **"Couldn't complete yet"** with the
   exact reason.
2. Fix the cause:
   - *Not enough stock* → receive more of the missing material (see "Receiving stock" below).
   - *No recipe found* → recreate or fix the recipe for that product.
3. You don't need to press anything special. The next time someone opens the **Batches** page, the
   system retries automatically and the batch completes once the cause is fixed.

### 2. "Not enough stock" when creating a batch
The app blocks a batch it can't fully supply and shows a table of exactly what's short.

**What to do:** receive a new lot of the short material(s) first, then create the batch again.
(The system will not let stock go negative — this is intentional.)

### 3. "Couldn't create the batch — reload and try again"
This almost always means a lot you picked got used up or expired between opening the form and
submitting it (e.g. a coworker used it at the same time).

**What to do:** reload the **Create Batch** page so it shows current stock, then re-select and submit.

### 4. A material, lot, recipe, or batch won't delete
The app **blocks** deletes that would break your records, and tells you why — for example
"can't delete this lot, it's used by 3 batch(es)." This is a safety feature, not a bug.

**What to do:** if you genuinely need to remove it, first deal with whatever is using it. If you're
not sure, **leave it alone** and ask the system owner.

### 5. The whole app is down or showing errors everywhere
1. Wait a minute and refresh — it may be a brief hiccup.
2. Check the hosting status page (Render): _______________________ (fill in URL).
3. If it's still down, contact the system owner with a screenshot.
4. **Don't panic about data loss** — the database is backed up automatically every night (see below).

---

## What NOT to do

- **Don't delete materials, recipes, or lots that are still in use.** Deleting a material that
  past batches used removes it from those batches' ingredient lists. If the app warns you something
  is in use, take the warning seriously.
- **Don't try to force past a block** ("not enough stock", "in use", etc.). The blocks exist to
  protect your inventory numbers.
- **Don't share the login** outside the team, and don't change the login credentials without
  telling the system owner.

---

## Backups & recovery (for the system owner / technical helper)

- The production database is dumped automatically **every night (~2am UTC)** by a GitHub Action
  (`.github/workflows/backup.yml` → `backup_prod.py`) and uploaded to the Supabase Storage bucket
  **`db-backups`** as `prod_backup_YYYYMMDD_HHMMSS.sql`.
- You can run a backup on demand: GitHub → **Actions** → **Daily Database Backup** → **Run workflow**.
- **Restore steps are documented in [`backup_instructions.txt`](backup_instructions.txt).**
- Health check: the app exposes **`/health`** — visiting it returns `{"status":"ok"}` when the app
  and database are reachable, or an error/503 when the database is down. Use this for uptime
  monitoring (Render health check or a free monitor like UptimeRobot).

### Tracing a bad edit (audit log)
- **Audit Log → click a record** (e.g. `lot #42`) shows every change to that record, with
  `old → new` values, the route that wrote it (Source), and the deployed commit (hover Source).
- If an audit write ever fails, the full row is still in Render's logs: search for
  `Failed to write audit log`.
- **One-time migration before deploying the audit-log refactor** — run in the Supabase SQL editor
  first, or every audit write will fail:
  ```sql
  ALTER TABLE audit_log
      ADD COLUMN IF NOT EXISTS entity_type TEXT,
      ADD COLUMN IF NOT EXISTS entity_id   INTEGER,
      ADD COLUMN IF NOT EXISTS changes     TEXT,
      ADD COLUMN IF NOT EXISTS source      TEXT,
      ADD COLUMN IF NOT EXISTS app_version TEXT;
  CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
  ```
  Rows from before this change have no record id, so they show in the full list but not in a
  record's history.

### Known minor quirks (not emergencies)
- Clicking **Edit** on a *Planned* batch shows a "Batch Materials Not Found" page — Planned batches
  have no materials recorded yet, so they can't be edited until they complete. View their status
  and reason on the **Manage Batches** list instead.
- Rate limiting uses in-memory storage, so limits are per-worker rather than global. Harmless at
  this scale; revisit only if abuse becomes an issue.
