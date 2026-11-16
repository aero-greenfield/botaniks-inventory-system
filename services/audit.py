from database import get_db_connection
import contextvars
import json
import logging
import os
import pandas as pd

# Which code path wrote a row. app.py sets this per request ("POST /update-lot/42"); anything run
# outside a request (scripts, a shell) keeps the default. A ContextVar keeps Flask out of services.
current_source = contextvars.ContextVar("audit_source", default="system")

# The deployed commit (Render sets RENDER_GIT_COMMIT), so a bad edit can be tied to the code that made it.
APP_VERSION = os.environ.get("RENDER_GIT_COMMIT", "local")[:7]


def diff(before, after):
    """{field: [old, new]} for every field in `after` whose value differs from `before`."""
    # Equal by value OR by text: Postgres hands back date/Decimal where the form sent '2026-11-01'/12.0.
    return {k: [before.get(k), v] for k, v in after.items()
            if before.get(k) != v and str(before.get(k)) != str(v)}


def log_action(action, details=None, entity=None, changes=None, db=None, cursor=None):
    """
    Writes an entry to the audit_log table.

    action:  short slug, e.g. 'lot_updated'
    details: free text for humans, e.g. 'name=Matcha, reason=Count correction'
    entity:  (entity_type, entity_id) of the record touched, e.g. ('lot', 42) — powers the
             per-record history filter on /audit-log
    changes: {field: [old, new]} for edits (build it with diff())
    db, cursor: pass the caller's open transaction to write the row INSIDE it (no commit here),
             so the change and its audit row commit or roll back together. Without them the row
             is written on its own connection — only safe after the caller has committed and closed.
    """
    entity_type, entity_id = entity if entity else (None, None)
    changes_json = json.dumps(changes, default=str) if changes else None
    sql = """
        INSERT INTO audit_log (action, details, entity_type, entity_id, changes, source, app_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    params = (action, details, entity_type, entity_id, changes_json, current_source.get(), APP_VERSION)

    if db is not None:
        # Errors propagate on purpose: the caller's transaction rolls back with the change.
        db.execute(cursor, sql, params)
        return

    own = get_db_connection()
    try:
        own.execute(own.cursor(), sql, params)
        own.commit()
    except Exception as e:
        # The change itself is already committed. Keep the full row in the server log so the
        # history can still be recovered from Render's logs.
        logging.error(f"Failed to write audit log: {e} | action={action} entity={entity} "
                      f"changes={changes_json} details={details}")
    finally:
        own.close()


def view_logs(entity_type=None, entity_id=None, page=1, per_page=100):
    """
    Audit rows, newest first, optionally only one record's history.
    Returns (df, total_matching_rows).
    """
    where, params = "", ()
    if entity_type and entity_id is not None:
        where, params = "WHERE entity_type = %s AND entity_id = %s", (entity_type, entity_id)

    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor, f"SELECT COUNT(*) FROM audit_log {where}", params)
        total = cursor.fetchone()[0]
        db.execute(cursor, f"""
            SELECT log_id, timestamp, action, entity_type, entity_id, changes, details, source, app_version
            FROM audit_log {where}
            ORDER BY log_id DESC
            LIMIT %s OFFSET %s
        """, (*params, per_page, (page - 1) * per_page))
        columns = [d[0] for d in cursor.description]
        df = pd.DataFrame(cursor.fetchall(), columns=columns)
        df['changes'] = df['changes'].map(lambda c: json.loads(c) if c else {})
        return df, total
    finally:
        db.close()
