# ADDED: these imports were all at the top of inventory_app.py — only the ones
#        actually used by the functions in this file are kept here.
from database import get_db_connection
import pandas as pd
import logging
from services import order_by, search_where
from config import business_today  # lot-expiration checks — see config.py



# CHANGED: organic feature — added is_edible/is_organic to the updatable allowlist so
#          update_raw_material (and the inline toggle route) can change them.
# CHANGED: units-conversion-layer feature — added "dimension" ('mass'|'count') to the
#          allowlist. `unit` holds the display unit (e.g. 'lb'); `dimension` says whether
#          quantities convert (mass -> grams base) or are a plain count.
_MATERIAL_UPDATABLE_COLS = frozenset({"name", "category", "unit", "reorder_level", "is_edible", "is_organic", "dimension"})


# CHANGED: organic feature — added is_edible/is_organic params (default edible, non-organic).
# CHANGED: units-conversion-layer feature — added `dimension` param.
def add_raw_material(name, category, unit, reorder_level, is_housemade=False, is_edible=True, is_organic=False, dimension=None):
    # adds material to raw_materials

    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, "SELECT material_id FROM raw_materials WHERE LOWER(name) = LOWER(%s)", (name,))
        if cursor.fetchone():
            return "duplicate"

        # CHANGED: organic feature — insert the two new boolean columns.
        # CHANGED: units-conversion-layer feature — insert `dimension`.
        db.execute(cursor, """
            INSERT INTO raw_materials (name, category, unit, reorder_level, is_housemade, is_edible, is_organic, dimension)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)

                        """, (name, category, unit, reorder_level, is_housemade, is_edible, is_organic, dimension))

        db.commit()
        logging.info(f"added new material: {name}")
        return db.get_last_insert_id(cursor)

    except Exception as e:
        logging.error(f"Error: {e}")
        db.rollback()
        return None
    finally:
        db.close()




# ADDED: sort feature — whitelist of sortable columns → SQL expression. The sort key arrives
#        from the URL (?sort=), so it must never be interpolated into SQL directly; only values
#        in this map are allowed. Aggregates (stock_level, total_cost) are sorted by their
#        SELECT alias, which PostgreSQL permits in ORDER BY.
_MATERIAL_SORT_COLS = {
    "name": "rm.name",
    "category": "rm.category",
    "stock_level": "stock_level",
    "unit": "rm.unit",
    "reorder_level": "rm.reorder_level",
    "total_cost": "total_cost",
    "is_edible": "rm.is_edible",
    "is_organic": "rm.is_organic",
}


def get_all_materials(page=None, per_page=50, sort_by=None, sort_dir='asc', q=None):
    """
    Returns all materials. Stock is derived from SUM of active lot quantities.

    # ADDED: page/per_page pagination parameters.
    # page=None  → full table, no LIMIT (used by export routes and create-batch); returns (df, None)
    # page=int   → paginated slice; returns (df, total_material_count)

    # ADDED: sort feature — sort_by must be a key in _MATERIAL_SORT_COLS (anything else,
    #        including None, falls back to the default category/name order). sort_dir is
    #        sanitized to ASC/DESC.
    """
    db = get_db_connection()
    cursor = db.cursor()
    try:
        # CHANGED: server-timezone-vs-business-timezone fix — was datetime.now().date().isoformat().
        now = business_today().isoformat()  # TEXT column — pass ISO string so PostgreSQL doesn't see TEXT > timestamp
        # CHANGED: organic feature — surface is_edible/is_organic so the manage-materials
        #          page can display and inline-toggle them.
        # ADDED: cost-of-material feature — total_cost column added to columns list.
        columns = ['material_id', 'name', 'category', 'stock_level', 'total_cost', 'unit', 'reorder_level', 'is_housemade', 'is_edible', 'is_organic']

        # ADDED: sort feature — build the ORDER BY clause from the whitelist. A valid sort_by
        #        sorts by that column with rm.name as a stable tiebreaker; otherwise keep the
        #        original default order.
        order_clause = order_by(_MATERIAL_SORT_COLS, sort_by, sort_dir, "category, name", "rm.name ASC")
        # search feature — optional name/category filter; applied to the COUNT below too.
        where, where_params = search_where(["rm.name", "rm.category"], q)
        where_sql = f"WHERE {where}" if where else ""

        # does not include expired lots in the stock quantity
        # ADDED: cost-of-material feature — total_cost aggregates quantity * cost_per_unit for active, non-expired lots.
        #        COALESCE on cost_per_unit treats lots with no cost as $0 so the sum stays numeric.
        # CHANGED: sort feature — ORDER BY is now the computed order_clause (was hard-coded
        #          "ORDER BY category, name").
        base_query = f"""
        SELECT rm.material_id, rm.name, rm.category,
               -- COALESCE so a material with no active lots reports 0, not NULL. Without it the
               -- LEFT JOIN yields SUM(NULL) → NULL, which slips past both `== 0` and `<= reorder`
               -- in the status badge and falsely renders "In Stock". (total_cost below already does this.)
               COALESCE(SUM(CASE WHEN rm_lot.quantity > 0 AND (rm_lot.expiration_date IS NULL OR rm_lot.expiration_date > %s)
                   THEN rm_lot.quantity ELSE 0 END), 0) as stock_level,
               COALESCE(SUM(CASE WHEN rm_lot.quantity > 0 AND (rm_lot.expiration_date IS NULL OR rm_lot.expiration_date > %s)
                   THEN rm_lot.quantity * COALESCE(rm_lot.cost_per_unit, 0) ELSE 0 END), 0) as total_cost,
               rm.unit, rm.reorder_level, rm.is_housemade, rm.is_edible, rm.is_organic
        FROM raw_materials rm
        LEFT JOIN raw_material_lots rm_lot ON rm.material_id = rm_lot.material_id
        {where_sql}
        GROUP BY rm.material_id
        {order_clause}
        """

        if page is None:
            # ADDED: page=None → return full table without LIMIT (exports, create-batch)
            # CHANGED: cost-of-material feature — pass now twice (stock_level and total_cost both use the date filter).
            db.execute(cursor, base_query, (now, now) + where_params)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: get total row count for pagination metadata (one count per material, not per lot)
        db.execute(cursor, f"SELECT COUNT(DISTINCT rm.material_id) FROM raw_materials rm {where_sql}", where_params or None)
        total = cursor.fetchone()[0]

        # ADDED: append LIMIT/OFFSET for the requested page
        # CHANGED: cost-of-material feature — pass now twice before LIMIT/OFFSET params.
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", (now, now) + where_params + (per_page, (page - 1) * per_page))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error getting all materials, (get_all_materials function) e: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()


# ADDED: returns all materials where current non-expired stock is below their reorder level.
# Called by /low-stock and /export/low-stock-excel in app.py.
# Was missing — those routes raised NameError on every visit.
def get_low_stock_materials():
    db = get_db_connection()
    cursor = db.cursor()
    try:
        now = business_today().isoformat()  # CHANGED: server-timezone-vs-business-timezone fix
        columns = ['material_id', 'name', 'category', 'stock_level', 'unit', 'reorder_level']
        db.execute(cursor, """
            SELECT rm.material_id, rm.name, rm.category,
                   SUM(CASE WHEN rm_lot.quantity > 0 AND (rm_lot.expiration_date IS NULL OR rm_lot.expiration_date > %s)
                       THEN rm_lot.quantity ELSE 0 END) AS stock_level,
                   rm.unit, rm.reorder_level
            FROM raw_materials rm
            LEFT JOIN raw_material_lots rm_lot ON rm.material_id = rm_lot.material_id
            GROUP BY rm.material_id
            HAVING SUM(CASE WHEN rm_lot.quantity > 0 AND (rm_lot.expiration_date IS NULL OR rm_lot.expiration_date > %s)
                        THEN rm_lot.quantity ELSE 0 END) <= rm.reorder_level
            ORDER BY category, name
        """, (now, now))
        result = cursor.fetchall()
        return pd.DataFrame(result, columns=columns)
    except Exception as e:
        logging.error(f"get_low_stock_materials: {e}")
        return pd.DataFrame()
    finally:
        db.close()


def get_material_by_id(material_id):
    "Returns material given its material_id"
    db = get_db_connection()
    cursor = db.cursor()

    try:
        # CHANGED: organic feature — return is_edible/is_organic for the edit form.
        # CHANGED: units-conversion-layer feature — return dimension for the edit form.
        db.execute(cursor, """
        SELECT material_id, name, category, unit, reorder_level, is_edible, is_organic, dimension
        FROM raw_materials
        WHERE material_id = %s
                       """,(material_id,))
        result = cursor.fetchone()
        return result

    except Exception as e:
        logging.error(f"Error: {e}")
        return None

    finally:
        db.close()


# CHANGED: organic feature — added is_edible/is_organic params so the edit form and the
#          inline toggle route can update them (None = leave unchanged, as with other fields).
def update_raw_material(material_id, name=None, category=None, stock_level=None, unit=None, reorder_level=None, is_edible=None, is_organic=None, dimension=None):
    """changes raw material info given id and which parameters are not None (ONLY CHANGES details, not stock level)"""

    db = get_db_connection()
    cursor = db.cursor()

    field = {} # dictionary to hold fields to update, only the ones that are not None

    #iterate through params, if not None, add to field dict to update
    # CHANGED: organic feature — include is_edible/is_organic in the field-collection loop.
    # CHANGED: units-conversion-layer feature — include dimension.
    for key, value in [("name", name), ("category", category), ("stock_level", stock_level), ("unit", unit), ("reorder_level", reorder_level), ("is_edible", is_edible), ("is_organic", is_organic), ("dimension", dimension)]:
        if value is not None:
            field[key] = value

    if not field: #empty dictionary, no fields to update
        print("No fields to update")
        return

    invalid = set(field) - _MATERIAL_UPDATABLE_COLS
    if invalid:
        raise ValueError(f"Invalid column name(s): {invalid}")

    try:
        # ISSUE: the loop issued one UPDATE per field — a separate round-trip for each column
        #        changed. 5 changed fields = 5 sequential SQL statements.
        # FIX: build the SET clause dynamically and run one multi-column UPDATE.
        #      Keys come from the hardcoded list above (not user input) so f-string is safe.

        # REMOVED: per-field UPDATE loop
        # for key in field:
        #     db.execute(cursor, f"UPDATE raw_materials SET {key} = %s WHERE material_id = %s", ...)

        # ADDED: single multi-column UPDATE replacing the loop
        set_clause = ', '.join(f"{key} = %s" for key in field)
        params = (*field.values(), material_id)  # *field.values() unpacks column values; material_id goes last for the WHERE
        db.execute(cursor, f"UPDATE raw_materials SET {set_clause} WHERE material_id = %s", params)

        if cursor.rowcount == 0:
            raise ValueError(f"material ID {material_id} not found — nothing updated")
        db.commit()
        logging.info(f"Updated material with id:{material_id}")

        return True

    except Exception as e:
        logging.error(f"Error changing material details: {e}")
        db.rollback()
        return None

    finally:
        db.close()




def get_raw_material(name):

    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, """
        SELECT material_id, name, reorder_level
        FROM raw_materials
        WHERE LOWER(name) = LOWER(%s)
                       """,(name,))
        result = cursor.fetchone()


        return result

    except Exception as e:
        logging.error(f"Error: {e}")
        db.rollback()
        return None

    finally:
        db.close()





def delete_raw_material(material_id):

    """Deletes raw material from database given its material_id"""

    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, """
        SELECT DISTINCT batch_id FROM batch_materials
        WHERE material_id = %s
        """, (material_id,))
        affected_batches = [row[0] for row in cursor.fetchall()]

        # Delete in FK-dependency order: batch_materials references both
        # raw_material_lots (lot_id) and raw_materials (material_id), so it must
        # go first. recipe_materials and raw_material_lots reference raw_materials,
        # so they come before raw_materials itself.
        db.execute(cursor, """
        DELETE FROM batch_materials
        WHERE material_id = %s
        """, (material_id,))

        db.execute(cursor, """
        DELETE FROM recipe_materials
        WHERE material_id = %s
        """, (material_id,))

        db.execute(cursor, """
        DELETE FROM raw_material_lots
        WHERE material_id = %s
        """, (material_id,))

        db.execute(cursor, """
        DELETE FROM raw_materials
        WHERE material_id = %s
        """, (material_id,))

        if cursor.rowcount == 0:
            raise ValueError(f"Material ID {material_id} not found — nothing deleted")

        db.commit()
        logging.info(f"Deleted material with ID {material_id} from raw materials")
        return {"deleted": True, "affected_batches": affected_batches}

    except Exception as e:
        logging.error(f"Error: {e}")
        db.rollback()
        return None

    finally:
        db.close()







def get_material_by_name(name):
    # ADDED: single-row lookup replacing the get_all_materials() + pandas filter pattern
    #        in /api/material-unit. One indexed seek; no JOIN, no GROUP BY, no aggregation.
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor,
            "SELECT name, unit FROM raw_materials WHERE LOWER(name) = LOWER(%s) LIMIT 1",
            (name,))
        return cursor.fetchone()  # (name, unit) tuple or None
    except Exception as e:
        logging.error(f"get_material_by_name: {e}")
        return None
    finally:
        db.close()


# ADDED: units-conversion-layer feature — fetch a material's display unit + dimension by name.
#        Used by the recipe routes to decide how to convert each ingredient amount:
#        mass -> grams via the entered unit; count -> identity (store the material's own unit).
def get_unit_and_dimension(name):
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor,
            "SELECT unit, dimension FROM raw_materials WHERE LOWER(name) = LOWER(%s) LIMIT 1",
            (name,))
        return cursor.fetchone()  # (unit, dimension) or None
    except Exception as e:
        logging.error(f"get_unit_and_dimension: {e}")
        return None
    finally:
        db.close()


def get_material_names(q=""):
    # ADDED: lightweight name-search for the /api/materials autocomplete endpoint.
    #        Returns up to 50 matches for the search string; filtering happens in SQL,
    #        not by fetching the full table into Python.
    # CHANGED: component-badge feature — returns [{name, is_housemade}] (was a list of bare
    #          names) so the autocomplete can flag house-made (Component) materials, mirroring
    #          how /api/recipes flags mix recipes. is_housemade is coerced to a bool for clean JSON.
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor,
            "SELECT name, is_housemade FROM raw_materials WHERE LOWER(name) LIKE LOWER(%s) ORDER BY name LIMIT 50",
            (f"%{q}%",))
        return [{"name": row[0], "is_housemade": bool(row[1])} for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"get_material_names: {e}")
        return []
    finally:
        db.close()


def get_material_id(name):

    """
    get name of a product,
    returns its material_id

    used for recieve lot func in app.py


    """

    db = get_db_connection()
    cursor = db.cursor()

    try:

        db.execute(cursor, """
        SELECT material_id
        FROM raw_materials
        WHERE LOWER(name) = LOWER(%s)
        """, (name,))
        row = cursor.fetchone()
        return (row[0]) if row else None

    except Exception as e:
        logging.error(f"didnt find material_id for name,(for recieve lot func)")
        return None

    finally:
        db.close()
