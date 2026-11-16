from database import get_db_connection
from services.audit import log_action, diff
from services import units
from datetime import datetime
from config import business_today
import logging
import pandas as pd
from services import order_by, search_where

_UNSET = object()
_EPS = 1e-9  # float tolerance when comparing allocated REAL quantities against a batch total


def _generate_shipment_number(cursor, db, date_shipped) -> str:
    """
    Builds shipment_number as YYMMDD + a per-date counter (e.g. "2607151" for the 1st
    shipment dated 2026-07-15). The counter is the only auto part; it is not zero-padded,
    so it just keeps growing past the 9th shipment on a given date (...10, ...11, ...).
    """
    prefix = datetime.strptime(date_shipped, '%Y-%m-%d').strftime('%y%m%d')
    db.execute(cursor, "SELECT COUNT(*) FROM shipments WHERE date_shipped = %s", (date_shipped,))
    count = cursor.fetchone()[0]
    return f"{prefix}{count+1}"


def _batch_remaining(cursor, db, batch_id, exclude_shipment_id=None):
    """
    Remaining unshipped quantity of a batch = produced quantity - SUM of its allocations.
    Pass exclude_shipment_id to ignore this shipment's own current allocation (used when
    re-validating an edit, so a line can keep/raise its quantity up to the true remaining).
    Returns None if the batch does not exist.
    """
    db.execute(cursor, "SELECT quantity FROM batches WHERE batch_id = %s", (batch_id,))
    row = cursor.fetchone()
    if not row:
        return None
    total = row[0] or 0

    if exclude_shipment_id is None:
        db.execute(cursor, "SELECT COALESCE(SUM(quantity), 0) FROM shipment_batches WHERE batch_id = %s", (batch_id,))
    else:
        db.execute(cursor,
                   "SELECT COALESCE(SUM(quantity), 0) FROM shipment_batches WHERE batch_id = %s AND shipment_id != %s",
                   (batch_id, exclude_shipment_id))
    allocated = cursor.fetchone()[0] or 0
    return total - allocated


def _recompute_batch_status(cursor, db, batch_id):
    """
    Recompute a batch's shipment state from its allocations. Status is derived, not manually flipped:
      nothing allocated      -> 'Ready'
      0 < allocated < total  -> 'Partially Shipped'
      allocated >= total     -> 'Shipped'
    date_shipped becomes the latest date of the shipments it's on (NULL when none).
    Guarded to only touch produced batches — a 'Planned' batch (deferred production) is never
    changed by shipment logic.
    """
    db.execute(cursor, """
        UPDATE batches SET
            status = CASE
                WHEN (SELECT COALESCE(SUM(quantity), 0) FROM shipment_batches WHERE batch_id = %s) <= 0
                THEN 'Ready'
               
                WHEN (SELECT COALESCE(SUM(quantity), 0) FROM shipment_batches WHERE batch_id = %s) < quantity
                THEN 'Partially Shipped'
               
                ELSE 'Shipped'
            END,
            date_shipped = (SELECT MAX(s.date_shipped)
                            FROM shipment_batches sb
                            JOIN shipments s ON sb.shipment_id = s.shipment_id
                            WHERE sb.batch_id = %s)
        WHERE batch_id = %s AND status IN ('Ready', 'Partially Shipped', 'Shipped')
    """, (batch_id, batch_id, batch_id, batch_id))


def create_shipment(lines, destination=None, notes=None, category=None, date_shipped=None):
    """
    Creates one shipment drawing the given quantities from one or more batches.
    `lines` is a dict {batch_id: quantity}. Each batch must be produced (status 'Ready' or
    'Partially Shipped') and the requested quantity must not exceed its remaining quantity.
    `date_shipped` (YYYY-MM-DD) drives both the stored date_shipped and the shipment_number
    prefix; defaults to business_today() when not given (e.g. for callers/tests that don't care).
    Returns the new shipment_id on success, None on unexpected error.
    Raises ValueError on bad input (batch missing/not shippable/over-allocated).


    note-- this loops over each batch in lines, and does 2 queries per batch: (might be optimized in the future)
    """
    if not lines:
        raise ValueError("A shipment must include at least one batch.")

    db = get_db_connection()
    cursor = db.cursor()
    try:
        # Validation pass — no writes until every line checks out.
        normalized = {}
        for batch_id, qty in lines.items():
            batch_id = int(batch_id)
            qty = float(qty)
            if qty <= 0:
                continue  # skip blank/zero lines
            
           
            #check each batch status
            db.execute(cursor, "SELECT status FROM batches WHERE batch_id = %s", (batch_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Batch {batch_id} not found.")
            if row[0] not in ('Ready', 'Partially Shipped'):
                raise ValueError(f"Batch {batch_id} has status '{row[0]}' — only produced batches with "
                                 f"remaining quantity can be shipped.")

            #check remaining quantity of each batch
            # float() the remaining: on PostgreSQL it comes back as Decimal (NUMERIC), and
            # `Decimal + float` (remaining + _EPS) raises TypeError — which is what made
            # shipping an exact remainder (e.g. 0.5) error out.
            remaining = float(_batch_remaining(cursor, db, batch_id) or 0)
            if qty > remaining + _EPS:
                raise ValueError(f"Batch {batch_id}: requested {qty} exceeds remaining {remaining}.")
            normalized[batch_id] = qty #add validated batch_id and quantity to the normalized dictionary

        if not normalized:
            raise ValueError("A shipment must include at least one batch with a positive quantity.")

         #validation done--

        if not date_shipped:
            date_shipped = business_today().strftime('%Y-%m-%d')
        shipment_number = _generate_shipment_number(cursor, db, date_shipped)

        db.execute(cursor, """
            INSERT INTO shipments (shipment_number, date_shipped, destination, notes, category)
            VALUES (%s, %s, %s, %s, %s)
        """, (shipment_number, date_shipped, destination, notes, category))
        shipment_id = db.get_last_insert_id(cursor)

        for batch_id, qty in normalized.items():
            db.execute(cursor, """
                INSERT INTO shipment_batches (shipment_id, batch_id, quantity)
                VALUES (%s, %s, %s)
            """, (shipment_id, batch_id, qty))
            _recompute_batch_status(cursor, db, batch_id) # update the status of the batch

        log_action('shipment_created', f"number={shipment_number}, lines={normalized}",
                   entity=('shipment', shipment_id), db=db, cursor=cursor)
        db.commit()
        return shipment_id

    except ValueError:
        db.rollback()
        raise

    except Exception as e:
        logging.error(f"Error creating shipment: {e}")
        db.rollback()
        return None

    finally:
        db.close()


# sort/search feature — whitelist (URL key → SQL) for the shipments list pages.
_SHIPMENT_SORT_COLS = {
    "date": "s.date_shipped IS NULL, s.date_shipped",
    "shipment_number": "s.shipment_number",
    "destination": "s.destination IS NULL, s.destination",
    "category": "s.category IS NULL, s.category",
    "batches": "batch_count",
}


def get_all_shipments(page=None, per_page=50, sort_by=None, sort_dir='desc', q=None):
    """
    Returns (DataFrame, total) of all shipments with the number of batch lines on each.
    page=None returns all records without LIMIT (used by /manage-shipments).
    sort_by = a _SHIPMENT_SORT_COLS key; q = shipment # / destination / category search.
    """
    db = get_db_connection()
    cursor = db.cursor()
    order_clause = order_by(_SHIPMENT_SORT_COLS, sort_by, sort_dir, "s.shipment_id DESC", "s.shipment_id DESC")
    where, where_params = search_where(["s.shipment_number", "s.destination", "s.category"], q)
    where_sql = f"WHERE {where}" if where else ""

    columns = ['shipment_id', 'shipment_number', 'date_shipped', 'destination', 'notes', 'category', 'batch_count']
    base_query = f"""
    SELECT s.shipment_id, s.shipment_number, s.date_shipped, s.destination, s.notes, s.category,
           COUNT(sb.shipment_batch_id) AS batch_count
    FROM shipments s
    LEFT JOIN shipment_batches sb ON sb.shipment_id = s.shipment_id
    {where_sql}
    GROUP BY s.shipment_id, s.shipment_number, s.date_shipped, s.destination, s.notes, s.category
    {order_clause}
    """

    try:
        if page is None:
            db.execute(cursor, base_query, where_params or None)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        db.execute(cursor, f"SELECT COUNT(*) FROM shipments s {where_sql}", where_params or None)
        total = cursor.fetchone()[0]

        offset = (page - 1) * per_page
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", where_params + (per_page, offset))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error getting shipments: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()


def get_shipments_summary():
    """
    One row per shipment — the "Shipments" sheet of the shipments Excel export.

    WHY shipment_batches is the source of truth: a batch can be split across several shipments,
    each split recorded as a shipment_batches row carrying its own `quantity`. So a shipment's
    "size" isn't a column on `shipments` — it's derived from its shipment_batches rows:
        batch_count = how many batch-lines are on the shipment (COUNT of the junction rows)
        total_units = SUM of those split quantities (the actual amount that shipped)

    WHY LEFT JOIN (not JOIN): a shipment with zero batch lines should still appear in the
    export (completeness). With a plain JOIN it would silently vanish. The LEFT JOIN keeps it;
    COUNT then returns 0 and COALESCE turns the NULL SUM into 0 instead of a blank cell.

    WHY a plain DataFrame (no pagination tuple): exports always want every row.

    Columns: shipment_number, destination, notes, category, batch_count, total_units.
    """
    db = get_db_connection()
    cursor = db.cursor()
    columns = ['shipment_number', 'destination', 'notes', 'category', 'batch_count', 'total_units']
    try:
        db.execute(cursor, """
        SELECT s.shipment_number, s.destination, s.notes, s.category,
               COUNT(sb.shipment_batch_id) AS batch_count,
               COALESCE(SUM(sb.quantity), 0) AS total_units
        FROM shipments s
        LEFT JOIN shipment_batches sb ON sb.shipment_id = s.shipment_id
        GROUP BY s.shipment_id, s.shipment_number, s.destination, s.notes, s.category
        ORDER BY s.shipment_id DESC
        """)
        result = cursor.fetchall()
        return pd.DataFrame(result, columns=columns)

    except Exception as e:
        logging.error(f"Error getting shipments summary for export: {e}")
        return pd.DataFrame()

    finally:
        db.close()


def get_shipment_details():
    """
    One row per shipment_batches record — the "Shipment Detail" sheet of the shipments export.

    This is the line-level companion to get_shipments_summary: where the summary has one row per
    shipment, this has one row per (shipment, batch) split. A batch split across two shipments
    produces two rows here.

    WHY a three-way join shipment_batches -> shipments -> batches: shipment_batches holds only
    ids + the split quantity, so we join out to `shipments` for the human-readable shipment_number
    and destination, and to `batches` for the batch_number / product_name / expiration_date.

    CRITICAL — which quantity: `quantity` is `sb.quantity`, the amount of the batch that shipped
    ON THIS shipment (the SPLIT amount), NOT `b.quantity` (the batch's full produced total).
    Using the batch total here would overstate what actually shipped on each line.

    WHY parent columns are repeated on every row (shipment_number, destination): an export row
    must stand alone for Excel sorting/filtering. We deliberately do NOT blank-out repeated
    parent values the way an HTML table might — each line carries its own shipment context.

    Columns: shipment_number, destination, batch_number, product_name, quantity, expiration_date.
    """
    db = get_db_connection()
    cursor = db.cursor()
    columns = ['shipment_number', 'destination', 'batch_number', 'product_name',
               'quantity', 'expiration_date']
    try:
        db.execute(cursor, """
        SELECT s.shipment_number, s.destination, b.batch_number, b.product_name,
               sb.quantity, b.expiration_date
        FROM shipment_batches sb
        JOIN shipments s ON sb.shipment_id = s.shipment_id
        JOIN batches b ON sb.batch_id = b.batch_id
        ORDER BY s.shipment_id DESC, b.batch_id ASC
        """)
        result = cursor.fetchall()
        return pd.DataFrame(result, columns=columns)

    except Exception as e:
        logging.error(f"Error getting shipment details for export: {e}")
        return pd.DataFrame()

    finally:
        db.close()


def get_shipment_materials(shipment_id):
    """
    One row per (batch, material, lot) consumed by the batches on this shipment — the
    "Materials & Lots" sheet of the per-shipment manifest export.

    WHY this exists: the manifest's first sheet is batch-level (what shipped). This sheet drills
    into the lot-level traceability for those batches — which lot of which material went into
    each shipped batch, and the cost recorded at deduction time. Joins out from shipment_batches
    -> batches -> batch_materials -> raw_materials / raw_material_lots.

    NOTE on quantity: bm.quantity_used is stored in the BASE unit (grams for mass). We convert it
    back to the material's display unit with units.from_base — same conversion the `disp` filter
    uses in the UI — so the export reads e.g. "1" kg, not "1000" g. Planned batches have no
    batch_materials rows (deduction deferred), so they simply contribute no rows here.

    Columns: batch_number, product_name, material_name, lot_number, quantity_used, unit, cost_per_unit.
    """
    db = get_db_connection()
    cursor = db.cursor()
    columns = ['batch_number', 'product_name', 'material_name', 'lot_number',
               'quantity_used', 'unit', 'cost_per_unit']
    try:
        db.execute(cursor, """
            SELECT b.batch_number, b.product_name, rm.name AS material_name,
                   rml.lot_number, bm.quantity_used, rm.unit, bm.cost_per_unit
            FROM shipment_batches sb
            JOIN batches b ON sb.batch_id = b.batch_id
            JOIN batch_materials bm ON bm.batch_id = b.batch_id
            JOIN raw_materials rm ON bm.material_id = rm.material_id
            JOIN raw_material_lots rml ON bm.lot_id = rml.lot_id
            WHERE sb.shipment_id = %s
            ORDER BY b.batch_id ASC, rm.name ASC
        """, (shipment_id,))
        rows = []
        for r in cursor.fetchall():
            try:
                qty_disp = units.from_base(r[4], r[5])
            except (TypeError, ValueError):
                qty_disp = r[4]
            rows.append((r[0], r[1], r[2], r[3], qty_disp, r[5], r[6]))
        return pd.DataFrame(rows, columns=columns)

    except Exception as e:
        logging.error(f"Error getting shipment materials for export: {e}")
        return pd.DataFrame()

    finally:
        db.close()


def get_shipment_by_id(shipment_id):
    """
    Returns {'shipment': dict, 'batches': list of dicts} or None if not found.
    Each batch line carries `quantity` (amount shipped on THIS shipment) plus `batch_quantity`
    (the batch's produced total, for reference).
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, """
            SELECT shipment_id, shipment_number, date_shipped, destination, notes, category
            FROM shipments WHERE shipment_id = %s
        """, (shipment_id,))
        row = cursor.fetchone()
        if not row:
            return None

        shipment = {
            'shipment_id': row[0],
            'shipment_number': row[1],
            'date_shipped': row[2],
            'destination': row[3],
            'notes': row[4],
            'category': row[5],
        }

        db.execute(cursor, """
            SELECT sb.shipment_batch_id, b.batch_id, b.batch_number, b.product_name,
                   sb.quantity, b.quantity, b.date_completed, b.expiration_date,
                   (SELECT product_unit FROM recipes
                    WHERE LOWER(recipes.product_name) = LOWER(b.product_name) LIMIT 1) AS product_unit
            FROM shipment_batches sb
            JOIN batches b ON sb.batch_id = b.batch_id
            WHERE sb.shipment_id = %s
            ORDER BY b.batch_id ASC
        """, (shipment_id,))
        batch_rows = cursor.fetchall()
        batches = [
            {
                'shipment_batch_id': r[0],
                'batch_id': r[1],
                'batch_number': r[2],
                'product_name': r[3],
                'quantity': r[4],         # amount shipped on this shipment
                'batch_quantity': r[5],   # batch produced total
                'date_completed': r[6],
                'expiration_date': r[7],
                'product_unit': r[8],
            }
            for r in batch_rows
        ]

        return {'shipment': shipment, 'batches': batches}

    except Exception as e:
        logging.error(f"Error getting shipment {shipment_id}: {e}")
        return None

    finally:
        db.close()


def update_shipment(shipment_id, destination=_UNSET, notes=_UNSET, category=_UNSET,
                     date_shipped=_UNSET, shipment_number=_UNSET, lines=_UNSET):
    """
    Updates a shipment's editable fields and/or its batch lines.
    - destination/notes/category/date_shipped/shipment_number use the _UNSET sentinel so None
      can explicitly clear a field.
    - shipment_number, when provided, must stay unique — raises ValueError on collision with
      another shipment (the route rebuilds it from an edited date + counter, so a collision
      means two shipments would end up sharing the same YYMMDD# number).
    - lines (dict {batch_id: quantity}), when provided, replaces the shipment's batch lines:
      batches absent from the dict (or with qty<=0) are removed, others are added/adjusted.
      Each batch's status/remaining is recomputed afterwards.
    Returns True on success, None on exception. Raises ValueError on bad input.
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, """SELECT destination, notes, category, date_shipped, shipment_number
                              FROM shipments WHERE shipment_id = %s""", (shipment_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Shipment {shipment_id} not found — nothing updated.")
        before = dict(zip(['destination', 'notes', 'category', 'date_shipped', 'shipment_number'], row))

        if shipment_number is not _UNSET:
            db.execute(cursor, "SELECT shipment_id FROM shipments WHERE shipment_number = %s AND shipment_id != %s",
                       (shipment_number, shipment_id))
            if cursor.fetchone():
                raise ValueError(f"Shipment number '{shipment_number}' is already in use by another shipment.")

        # --- field updates ---
        fields = {}
        if destination is not _UNSET:
            fields['destination'] = destination
        if notes is not _UNSET:
            fields['notes'] = notes
        if category is not _UNSET:
            fields['category'] = category
        if date_shipped is not _UNSET:
            fields['date_shipped'] = date_shipped
        if shipment_number is not _UNSET:
            fields['shipment_number'] = shipment_number
        if fields:
            set_clause = ', '.join(f"{k} = %s" for k in fields)
            params = (*fields.values(), shipment_id)
            db.execute(cursor, f"UPDATE shipments SET {set_clause} WHERE shipment_id = %s", params)

        # --- line edits ---
        if lines is not _UNSET:
            db.execute(cursor,
                       "SELECT batch_id, shipment_batch_id, quantity FROM shipment_batches WHERE shipment_id = %s",
                       (shipment_id,))
            old_rows = cursor.fetchall()
            existing = {r[0]: r[1] for r in old_rows}
            before['lines'] = {r[0]: float(r[2]) for r in old_rows}

            new_lines = {}
            for batch_id, qty in lines.items():
                qty = float(qty)
                if qty <= 0:
                    continue
                new_lines[int(batch_id)] = qty

            if not new_lines:
                raise ValueError("A shipment must keep at least one batch line — delete the shipment instead.")

            # Validate every new/changed line against remaining (excluding this shipment's own allocation).
            for batch_id, qty in new_lines.items():
                remaining = _batch_remaining(cursor, db, batch_id, exclude_shipment_id=shipment_id)
                if remaining is None:
                    raise ValueError(f"Batch {batch_id} not found.")
                # float() the remaining — see the same Decimal+float TypeError note in create_shipment.
                remaining = float(remaining)
                if qty > remaining + _EPS:
                    raise ValueError(f"Batch {batch_id}: requested {qty} exceeds remaining {remaining}.")

            affected = set(existing) | set(new_lines)

            # Remove lines no longer present.
            for batch_id, sbid in existing.items():
                if batch_id not in new_lines:
                    db.execute(cursor, "DELETE FROM shipment_batches WHERE shipment_batch_id = %s", (sbid,))

            # Add or adjust the rest.
            for batch_id, qty in new_lines.items():
                if batch_id in existing:
                    db.execute(cursor, "UPDATE shipment_batches SET quantity = %s WHERE shipment_batch_id = %s",
                               (qty, existing[batch_id]))
                else:
                    db.execute(cursor, "INSERT INTO shipment_batches (shipment_id, batch_id, quantity) VALUES (%s, %s, %s)",
                               (shipment_id, batch_id, qty))

            for batch_id in affected:
                _recompute_batch_status(cursor, db, batch_id)

        if not fields and lines is _UNSET:
            return True

        after = dict(fields, **({'lines': new_lines} if lines is not _UNSET else {}))
        log_action('shipment_updated', None, entity=('shipment', shipment_id),
                   changes=diff(before, after), db=db, cursor=cursor)
        db.commit()
        return True

    except ValueError:
        db.rollback()
        raise

    except Exception as e:
        logging.error(f"Error updating shipment {shipment_id}: {e}")
        db.rollback()
        return None

    finally:
        db.close()


def delete_shipment(shipment_id):
    """
    Deletes a shipment and its batch lines, then recomputes each affected batch's status/remaining
    (a batch returns toward 'Ready' if it has no other allocations).
    Returns True on success, None on exception. Raises ValueError if shipment not found.
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, "SELECT shipment_number FROM shipments WHERE shipment_id = %s", (shipment_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Shipment {shipment_id} not found — nothing deleted.")

        db.execute(cursor, "SELECT batch_id, quantity FROM shipment_batches WHERE shipment_id = %s", (shipment_id,))
        deleted_lines = {r[0]: float(r[1]) for r in cursor.fetchall()}
        affected = list(deleted_lines)

        db.execute(cursor, "DELETE FROM shipment_batches WHERE shipment_id = %s", (shipment_id,))
        db.execute(cursor, "DELETE FROM shipments WHERE shipment_id = %s", (shipment_id,))

        for batch_id in affected:
            _recompute_batch_status(cursor, db, batch_id)

        log_action('shipment_deleted', f"number={row[0]}, lines={deleted_lines}",
                   entity=('shipment', shipment_id), db=db, cursor=cursor)
        db.commit()
        return True

    except ValueError:
        db.rollback()
        raise

    except Exception as e:
        logging.error(f"Error deleting shipment {shipment_id}: {e}")
        db.rollback()
        return None

    finally:
        db.close()
