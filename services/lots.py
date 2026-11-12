# ADDED: these imports were all at the top of inventory_app.py — only the ones
#        actually used by the functions in this file are kept here.
from database import get_db_connection
import pandas as pd
import logging
from config import business_now  # lot-expiration checks — see config.py

_LOT_UPDATABLE_COLS = frozenset({"lot_number", "quantity", "received_date", "expiration_date",
                                  "location", "supplier", "cost_per_unit", "status"})

# Below this many units, a lot is treated as fully depleted. Deducting (nearly) all of a lot
# leaves a tiny floating-point residual (e.g. 0.0000055) that is too small to be useful but
# big enough to pass the `quantity > 0` filters, so the lot keeps showing as available stock.
# CHANGED: units-conversion-layer feature — quantities are now stored in grams (mass) instead
#          of pounds, so this threshold is re-based to grams. 0.01 g (10 mg) is well below any
#          real usage but safely above float drift; counts (whole units) are far above it too.
LOT_EXHAUST_THRESHOLD = 0.01


class LotChangedError(Exception):
    """The lot's quantity changed (e.g. a batch drew from it) after the edit page was opened.
    Deliberately NOT a ValueError, so update_lot doesn't swallow it into a plain False — the
    route needs to tell the user to reload instead of showing a generic failure."""


def exhaust_lot_if_depleted(db, cursor, lot_id, threshold=LOT_EXHAUST_THRESHOLD):
    """After a deduction, zero out and deactivate a lot whose remaining quantity is a negligible
    residual, so it no longer shows as available stock. Uses `< threshold` so it also catches
    small negatives from float drift."""
    db.execute(cursor, """
        UPDATE raw_material_lots
        SET quantity = 0, status = 'inactive'
        WHERE lot_id = %s AND quantity < %s
    """, (lot_id, threshold))


#=======================
#LOT NUMBER FUNCTIONS
#=======================

# read functions:

def get_lots_for_material(material_id):
    """
    returns all 'active' lots (lots where quantitiy > 0) for a given material_id.
    ordered by received_date ASC.

    will be used for batch creation drop down menu when choosing which lots to deduct from.

    """

    db = get_db_connection()
    cursor = db.cursor()
    try:
        date_now = business_now().strftime('%Y-%m-%d %H:%M:%S')  # CHANGED: server-timezone-vs-business-timezone fix

        db.execute(cursor,"""
        SELECT rm_lot.lot_id, rm.name AS material_name, rm_lot.lot_number, rm_lot.quantity, rm_lot.received_date, rm_lot.status, rm_lot.expiration_date,
               rm_lot.location, rm.unit
        FROM raw_material_lots rm_lot
        JOIN raw_materials rm ON rm_lot.material_id = rm.material_id
        WHERE rm_lot.material_id = %s AND rm_lot.quantity > 0 AND rm_lot.status = 'active' AND (rm_lot.expiration_date IS NULL OR rm_lot.expiration_date > %s)
        ORDER BY rm_lot.received_date ASC

                    """, (material_id, date_now,))
        result = cursor.fetchall()
        # CHANGED: lot-picker-location — location (which warehouse the lot is in) and the
        #          material's unit, so the batch lot pickers can show "26260HU1 · Fern · 936 lb".
        columns = ['lot_id', 'material_name', 'lot_number', 'quantity', 'received_date', 'status', 'expiration_date',
                   'location', 'unit']
        df = pd.DataFrame(result, columns=columns)
        return df

    except Exception as e:
        logging.error(f"Error fetching lots for material (batch drop down function) {material_id}: {e}")
        return None

    finally:
        db.close()


def get_all_lots_for_material(material_id):
    """
    same as above, but includes exired and exhausted lots
    will be used for inventory page

    returns:
    lot_id, lot_number, lot_quantity, lot_recieved_date, lot_status, lot_expiration_date, lot_location.

    """

    db = get_db_connection()
    cursor = db.cursor()
    try:


        db.execute(cursor,"""
        SELECT rm_lot.lot_id, rm_lot.lot_number, rm_lot.quantity, rm_lot.received_date, rm_lot.status, rm_lot.expiration_date, rm_lot.location, rm_lot.cost_per_unit
        FROM raw_material_lots rm_lot
        JOIN raw_materials rm ON rm_lot.material_id = rm.material_id
        WHERE rm_lot.material_id = %s
        ORDER BY rm_lot.received_date ASC

                    """, (material_id,))
        result = cursor.fetchall()
        columns = ['lot_id', 'lot_number', 'quantity', 'received_date', 'status', 'expiration_date', 'location', 'cost_per_unit']
        df = pd.DataFrame(result, columns=columns)
        return df

    except Exception as e:
        logging.error(f"Error fetching lots for material (inventory page function) {material_id}: {e}")
        return None

    finally:
        db.close()


# ADDED: table-redesign feature — bulk fetch of active lots for many materials at once.
# The inventory page now server-renders each material's lots inside an expandable drawer.
# Doing that per-row would be an N+1 query storm (one /api/lots call per material);
# this runs a single SELECT for the whole page and groups the result by material_id.
def get_active_lots_for_materials(material_ids):
    """
    Returns active lots for a list of material_ids, grouped for the inventory drawers.

    "Active" matches get_lots_for_material: quantity > 0, status = 'active', and not expired.
    Ordered by received_date ASC (FIFO) within each material.

    returns: dict mapping material_id -> list of lot dicts, each with keys
             lot_number, quantity, received_date, expiration_date, location, supplier.
             Materials with no active lots are simply absent from the dict.
    """
    # ADDED: guard empty input — an empty IN () list is a SQL syntax error.
    if not material_ids:
        return {}

    db = get_db_connection()
    cursor = db.cursor()
    try:
        date_now = business_now().strftime('%Y-%m-%d %H:%M:%S')  # CHANGED: server-timezone-vs-business-timezone fix
        # ADDED: build the IN-list placeholders dynamically. These are %s placeholders
        #        (rewritten to ? for SQLite by db.execute), never interpolated values.
        placeholders = ', '.join(['%s'] * len(material_ids))
        db.execute(cursor, f"""
        SELECT material_id, lot_number, quantity, received_date, expiration_date, location, supplier
        FROM raw_material_lots
        WHERE material_id IN ({placeholders})
          AND quantity > 0 AND status = 'active'
          AND (expiration_date IS NULL OR expiration_date > %s)
        ORDER BY material_id ASC, received_date ASC
        """, (*material_ids, date_now))
        result = cursor.fetchall()

        grouped = {}
        for material_id, lot_number, quantity, received_date, expiration_date, location, supplier in result:
            grouped.setdefault(material_id, []).append({
                'lot_number': lot_number,
                'quantity': quantity,
                'received_date': received_date,
                'expiration_date': expiration_date,
                'location': location,
                'supplier': supplier,
            })
        return grouped

    except Exception as e:
        logging.error(f"Error bulk-fetching active lots for materials {material_ids}: {e}")
        return {}

    finally:
        db.close()


def get_material_stock_from_lots(material_id):

    """
    Used to get stock level by diriving from SUM of active lots with given material_id.

    used for reading stock level directly.


    """
    db = get_db_connection()
    cursor = db.cursor()
    try:
        date_now = business_now().strftime('%Y-%m-%d %H:%M:%S')  # CHANGED: server-timezone-vs-business-timezone fix
        db.execute(cursor, """
        SELECT SUM(quantity) FROM raw_material_lots
        WHERE material_id = %s AND status = 'active' AND (expiration_date IS NULL OR expiration_date > %s)
        """, (material_id, date_now,))
        result = cursor.fetchone()
        return result[0] if result[0] is not None else 0.0

    except Exception as e:
        logging.error(f"Error getting stock from lots for material (for stock_level of material_id) {material_id}: {e}")
        return None

    finally:
        db.close()


#Write functions:

def receive_lot(material_id, lot_number, quantity, received_date, expiry_date=None, location=None, supplier=None, cost_per_unit=None):
    """
    adds new row into raw_material_lots

    validates that quantity > 0

    will be used when needed to add to a material via a new lot.

    """

    db = get_db_connection()
    cursor = db.cursor()

    try:

        #validate quantity:
        if (quantity is None) or (quantity <= 0):
            raise ValueError("Quantity must be a positive number.")


        db.execute(cursor, """
        INSERT INTO raw_material_lots (material_id, lot_number, quantity, received_date, expiration_date, location, supplier, cost_per_unit)
        VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
                   """,(material_id, lot_number, quantity, received_date, expiry_date, location, supplier, cost_per_unit))


        if cursor.rowcount == 0:
                raise ValueError(f"LOT ID not found — nothing updated when receiving lot {lot_number}")

        lot_id = db.get_last_insert_id(cursor)



        logging.info(f"Added lot number {lot_number} for material id:{material_id} with quantity {quantity}")
        db.commit()
        return lot_id


    except ValueError as e:
        logging.error(f"Value error adding new row to lot table. Error: {e}")
        db.rollback()
        return None

    except Exception as e:
        logging.error(f"Error adding a new row in lot numbers table. {material_id}/{lot_number}: {e}")
        db.rollback()
        return None

    finally:
        db.close()


def get_all_lots(material_id=None, page=None, per_page=50):
    """
    returns all lots, for given material_id if provided, otherwise all materials.

    returns:
    lot_id, lot_number, material_name, material_id, quantity, received_date, expiration,
    location, supplier, cost_per_unit, status

    # ADDED: page/per_page pagination parameters.
    # page=None → full table, no LIMIT; returns (df, None)
    # page=int  → paginated slice; returns (df, total_lot_count)
    """

    db = get_db_connection()
    cursor = db.cursor()
    # CHANGED: units-conversion-layer feature — include the material's display unit so the
    #          manage-lots page can convert the stored base-unit quantity/cost back for display.
    columns = ['lot_id', 'lot_number', 'material_name', 'material_id', 'quantity', 'received_date', 'expiration_date', 'location', 'supplier', 'cost_per_unit', 'status', 'unit']

    try:
        if material_id:
            base_query = """
            SELECT rml.lot_id, rml.lot_number, rm.name AS material_name, rm.material_id,
                   rml.quantity, rml.received_date, rml.expiration_date,
                   rml.location, rml.supplier, rml.cost_per_unit, rml.status,
                   rm.unit
            FROM raw_material_lots rml
            JOIN raw_materials rm ON rml.material_id = rm.material_id
            WHERE rml.material_id = %s
            ORDER BY rm.name ASC, rml.received_date ASC
            """
            base_params = (material_id,)
            # ADDED: count query for this material's lots
            count_query = "SELECT COUNT(*) FROM raw_material_lots WHERE material_id = %s"
            count_params = (material_id,)
        else:
            base_query = """
            SELECT rml.lot_id, rml.lot_number, rm.name AS material_name, rm.material_id,
                   rml.quantity, rml.received_date, rml.expiration_date,
                   rml.location, rml.supplier, rml.cost_per_unit, rml.status,
                   rm.unit
            FROM raw_material_lots rml
            JOIN raw_materials rm ON rml.material_id = rm.material_id
            ORDER BY rm.name ASC, rml.received_date ASC
            """
            base_params = ()
            # ADDED: count all lots
            count_query = "SELECT COUNT(*) FROM raw_material_lots"
            count_params = ()

        if page is None:
            # ADDED: page=None → full table without LIMIT (no callers currently need this
            #        but kept consistent with other get_all_* functions)
            # CHANGED: was passing None when base_params is empty — omit params arg instead
            #          so SQLite doesn't receive None (not iterable in Python 3.12+).
            if base_params:
                db.execute(cursor, base_query, base_params)
            else:
                db.execute(cursor, base_query)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: total count for pagination metadata
        # CHANGED: was passing None when count_params is empty — omit params arg instead.
        if count_params:
            db.execute(cursor, count_query, count_params)
        else:
            db.execute(cursor, count_query)
        total = cursor.fetchone()[0]

        # ADDED: LIMIT/OFFSET for the requested page
        paginated_params = (*base_params, per_page, (page - 1) * per_page)
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", paginated_params)
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error fetching all lots (with or without material_id {material_id}): {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()

def get_all_lots_joined():
    """
    Returns EVERY lot in the system (one row per lot) joined to its material name, for the
    inventory Excel export's "Lots" sheet.

    WHY a separate function from get_lots_for_material / get_active_lots_for_materials:
        Those UI functions deliberately HIDE lots the warehouse can't draw from right now —
        they filter to quantity > 0, status = 'active', and not-expired. An EXPORT has the
        opposite goal: completeness. The boss/analyst opening this in Excel wants the full
        history — expired lots, exhausted (quantity 0 / status 'inactive') lots, everything —
        and will filter/sort themselves. So this query applies NO WHERE filter at all; the
        `status` column is included precisely so the reader can filter in Excel instead.

    WHY no pagination tuple: exports always want the whole table, so this returns a plain
        DataFrame (not the (df, total) shape the paginated read functions use).

    Columns: lot_number, material_name, quantity (remaining), cost_per_unit,
             received_date, expiration_date, location, supplier, status.
    """
    db = get_db_connection()
    cursor = db.cursor()
    # Column order here is the column order in the spreadsheet, so it's chosen for reading:
    # identity first (lot + material), then the numbers, then dates, then location/supplier, status last.
    columns = ['lot_number', 'material_name', 'quantity', 'cost_per_unit',
               'received_date', 'expiration_date', 'location', 'supplier', 'status']
    try:
        # JOIN (not LEFT JOIN) to raw_materials: every lot has a material_id by construction,
        # and we need the human-readable material name rather than the numeric id in the export.
        # ORDER BY material name then received_date so a reader scanning the sheet sees each
        # material's lots grouped together in receive order (FIFO), matching get_all_lots.
        db.execute(cursor, """
        SELECT rml.lot_number, rm.name AS material_name, rml.quantity, rml.cost_per_unit,
               rml.received_date, rml.expiration_date, rml.location, rml.supplier, rml.status
        FROM raw_material_lots rml
        JOIN raw_materials rm ON rml.material_id = rm.material_id
        ORDER BY rm.name ASC, rml.received_date ASC
        """)
        result = cursor.fetchall()
        return pd.DataFrame(result, columns=columns)

    except Exception as e:
        logging.error(f"Error fetching all lots joined for export: {e}")
        return pd.DataFrame()

    finally:
        db.close()


def get_lot_by_id(lot_id):

    """
    
    returns:
    every lot column, material name, material is housemad boolean
    
    
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        # CHANGED: units-conversion-layer feature — also select the material's unit and
        #          dimension so the edit-lot page can display/convert the stored base-unit
        #          quantity and cost back into the user's display unit.
        # CHANGED: list the lot columns explicitly instead of `rml.*`. With `rml.*` the
        #          result column order followed the table's physical order
        #          (... status, supplier, location, cost_per_unit) while the `columns` list
        #          below assumed (... location, supplier, cost_per_unit, status), so the
        #          location field received the status value ("active") and cost_per_unit
        #          received the location text — breaking the edit-lot page. Explicit columns
        #          keep query order and `columns` order in lock-step.
        db.execute(cursor, """
        SELECT rml.lot_id, rml.material_id, rml.lot_number, rml.quantity, rml.received_date,
               rml.expiration_date, rml.location, rml.supplier, rml.cost_per_unit, rml.status,
               rm.name AS material_name, rm.is_housemade, rm.unit, rm.dimension
        FROM raw_material_lots rml
        JOIN raw_materials rm ON rml.material_id = rm.material_id
        WHERE rml.lot_id = %s
        """, (lot_id,))
        result = cursor.fetchone()
        if result is None:
            raise ValueError(f"Lot with ID {lot_id} not found.")

        columns = ['lot_id', 'material_id', 'lot_number', 'quantity', 'received_date', 'expiration_date', 'location', 'supplier', 'cost_per_unit', 'status', 'material_name', 'is_housemade', 'unit', 'dimension']
        df = pd.DataFrame([result], columns=columns) # create a dataframe with a single row. 

        return df

    except ValueError as e:
        logging.error(f"Value error fetching lot by id {lot_id}. Error: {e}")
        return None

    except Exception as e:
        logging.error(f"Error fetching lot by id {lot_id}. Error: {e}")
        return None

    finally:
        db.close()


def update_lot(lot_id, lot_number=None, quantity=None, received_date=None, expiration_date=None, location=None, supplier=None, cost_per_unit=None, status=None, is_housemade=None, expected_quantity=None):
    """

    lets user update any lot information.


    If is_housemade is True
    (or lot_number starts with MIX-BATCH-), skip updating quantity (guard in service layer).

    When quantity is set, status follows it: 0 -> 'inactive', above 0 -> 'active' (so a refilled
    lot shows up for batches again). expected_quantity is the quantity the user saw when they
    opened the edit page; if the lot no longer holds that amount, raises LotChangedError instead
    of overwriting a deduction made in the meantime.

    returns True if successful, False otherwise.

    """

    db = get_db_connection()
    cursor = db.cursor()

    

    field = {}
    # for each value provided, add to field dict. 
    for key, value in { 
        'lot_number': lot_number, 
        'quantity': quantity, 
        'received_date': received_date, 
        'expiration_date': expiration_date, 
        'location': location, 
        'supplier': supplier, 
        'cost_per_unit': cost_per_unit, 
        'status': status,
        
    }.items():
        if value is not None:
            field[key] = value

    if not field:
        raise ValueError("No fields provided for update.")
    
    # Can't edit a house-made lot's quantity here — it is derived from the mix batch that produced
    # it, so the batch is the single entry point. CHANGED: that was already the intent, but the
    # place this deferred to was empty: update_batch never touched the output lot, so a quantity
    # edit desynced the two (8 production batches). update_batch now reconciles it for real, via
    # _reconcile_mix_output_lot, so this block correctly keeps the lot side read-only.
    if (is_housemade or (lot_number and lot_number.startswith("MIX-BATCH-"))) and 'quantity' in field:
        logging.warning(f"Attempted to update quantity for housemade lot {lot_id}. This is not allowed. Quantity will not be updated.")
        del field['quantity']

    # ADDED: lot-edit-safety — status follows quantity. Before, an exhausted lot stayed 'inactive'
    # after being refilled here, so it held stock that batches could never draw from.
    if 'quantity' in field:
        field['status'] = 'active' if field['quantity'] >= LOT_EXHAUST_THRESHOLD else 'inactive'

    invalid = set(field) - _LOT_UPDATABLE_COLS
    if invalid:
        raise ValueError(f"Invalid column name(s): {invalid}")

    try:
        # ISSUE: the loop issued one UPDATE per field — a separate round-trip for each column
        #        changed. 5 changed fields = 5 sequential SQL statements.
        # FIX: build the SET clause dynamically and run one multi-column UPDATE.
        #      Keys come from the hardcoded dict above (not user input) so f-string is safe.

        # REMOVED: per-field UPDATE loop
        # for key in field:
        #     db.execute(cursor, f"UPDATE raw_material_lots SET {key} = %s WHERE lot_id = %s", ...)

        # ADDED: single multi-column UPDATE replacing the loop
        set_clause = ', '.join(f"{key} = %s" for key in field)
        params = (*field.values(), lot_id)  # *field.values() unpacks column values; lot_id goes last for the WHERE
        where = "lot_id = %s"
        # ADDED: lot-edit-safety — only overwrite the quantity if it still holds what the user saw.
        # Without this, a page opened before a batch ran would put the batch's deduction back.
        if 'quantity' in field and expected_quantity is not None:
            where += " AND ABS(quantity - %s) < %s"
            params = (*params, expected_quantity, LOT_EXHAUST_THRESHOLD)
        db.execute(cursor, f"UPDATE raw_material_lots SET {set_clause} WHERE {where}", params)

        if cursor.rowcount == 0:
            db.execute(cursor, "SELECT 1 FROM raw_material_lots WHERE lot_id = %s", (lot_id,))
            if cursor.fetchone():
                db.rollback()
                raise LotChangedError(f"lot {lot_id} quantity changed since the edit page was opened")
            raise ValueError(f"lot ID {lot_id} not found — nothing updated")

        if 'cost_per_unit' in field:
            db.execute(cursor, "UPDATE batch_materials SET cost_per_unit = %s WHERE lot_id = %s", (field['cost_per_unit'], lot_id))
            logging.info(f"Cascaded cost_per_unit update to batch_materials for lot_id={lot_id}")

        db.commit()
        logging.info(f"Updated lot with id:{lot_id}")

        return True
    
    except ValueError as e:
        logging.error(f"Value error updating lot with id {lot_id}. Error: {e}")
        db.rollback()
        return False

    finally:
        db.close()


def get_batches_for_lot(lot_id):
    """
    Returns all batches that reference this lot in batch_materials.
    Used to warn the user before deleting a lot that is tracked in a batch.
    Returns a list of dicts with batch_id and batch_name, or an empty list.
    """
    db = get_db_connection()
    cursor = db.cursor()
    try:
        # FIXED: batches has no batch_name column — use product_name, aliased to batch_name
        #        so callers/templates that read `batch_name` keep working.
        db.execute(cursor, """
        SELECT DISTINCT b.batch_id, b.product_name AS batch_name
        FROM batch_materials bm
        JOIN batches b ON bm.batch_id = b.batch_id
        WHERE bm.lot_id = %s
        """, (lot_id,))
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        logging.error(f"Error fetching batches for lot {lot_id}: {e}")
        return []
    finally:
        db.close()


def delete_lot(lot_id):
    """
    Permanently deletes a lot from raw_material_lots.
    Returns True on success, False on failure.
    """
    db = get_db_connection()
    cursor = db.cursor()
    try:
        # GUARD (defense in depth): never delete a lot still referenced by a batch.
        # On Postgres the FK would reject the delete anyway; on SQLite (no FK enforcement)
        # it would silently orphan batch_materials.lot_id. Block it explicitly on both.
        db.execute(cursor, "SELECT 1 FROM batch_materials WHERE lot_id = %s LIMIT 1", (lot_id,))
        if cursor.fetchone():
            logging.warning(f"Refusing to delete lot {lot_id}: still referenced by batch_materials.")
            return False

        db.execute(cursor, """
        DELETE FROM raw_material_lots
        WHERE lot_id = %s
        """, (lot_id,))
        db.commit()
        logging.info(f"Deleted lot with id:{lot_id}")
        return True
    except Exception as e:
        logging.error(f"Error deleting lot with id {lot_id}. Error: {e}")
        db.rollback()
        return False
    finally:
        db.close()








