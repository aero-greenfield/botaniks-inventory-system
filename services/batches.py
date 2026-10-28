# ADDED: these imports were all at the top of inventory_app.py — only the ones
#        actually used by the functions in this file are kept here.
from database import get_db_connection
import pandas as pd
from datetime import datetime
import logging
from config import business_today, business_now  # calendar-day-boundary checks — see config.py
import time
import json

_BATCH_UPDATABLE_COLS = frozenset({"product_name", "quantity", "date_completed", "notes",
                                    "batch_number", "expiration_date", "planned_completion_date"})

# ADDED: batches.py calls get_raw_material (for mix batch logic in add_to_batches
#        and promote_planned_batches) and get_recipe (for deduction and promotion).
#        They live in their own service modules now, so we import them explicitly.
from services.materials import get_raw_material
from services.recipes import get_recipe, get_recipe_unit_and_dimension
from services.lots import exhaust_lot_if_depleted

# ADDED: units-conversion-layer feature — mix batches store the produced housemade material's
#        display unit/dimension from the recipe's product_unit and store the produced lot
#        quantity in the canonical base unit (grams for mass) so finished-batch deduction lines up.
from services import units

# ADDED: promote_planned_batches calls log_action directly to record promotions.
#        log_action moved to services.audit, so we import it here.
from services.audit import log_action

# ADDED: _UNSET was defined at the top of inventory_app.py (line 13).
#        It only affects update_batch, so it moves here with the batch functions.
_UNSET = object()  # sentinel for optional fields that can be explicitly set to None

# ADDED: _last_promote_time was defined at the top of inventory_app.py (line 14).
#        It's only read/written by get_batches(), so it moves here.
_last_promote_time = 0

# ADDED: lot-selection-unit fix — tolerance for "is there enough stock?" comparisons.
# Mass quantities are stored at 0.1 mg (4-dp gram) resolution, so a comparison epsilon TIGHTER
# than that resolution wrongly rejects stock that is, in reality, exactly enough (e.g. 176 lb of
# mix vs an 11x16 lb requirement that differed by ~0.00002 g). 1e-3 g (1 mg) sits safely above
# the storage resolution yet is negligible for any realistic quantity. Use this for every
# coverage/per-lot "have >= need" check instead of a hardcoded 1e-6.
_QTY_EPSILON_G = 1e-3


def _covers(have, need):
    """True if `have` is at least `need`, within the storage tolerance (_QTY_EPSILON_G).

    Single source of truth for every "is there enough stock?" comparison. Hand-copying this
    check is exactly how one site (check_batch_materials_stock) ended up with NO tolerance and
    another with a too-tight 1e-6 — route them all through here so the epsilon is applied once.
    """
    return float(have) + _QTY_EPSILON_G >= float(need)


def _quantities_match(a, b):
    """True if two quantities are equal within the storage tolerance — used to confirm the
    selected lots sum to the required amount (neither short nor over)."""
    return abs(float(a) - float(b)) <= _QTY_EPSILON_G


# ========================
# BATCHES FUNCTIONS
# ========================

def _create_housemade_lot(db, cursor, product_name, quantity, batch_number, received_date, batch_id):
    """Create (or reuse) the house-made raw_material for a mix/Component product and insert a
    lot for the produced quantity. Shared by add_to_batches (immediate mix) and
    promote_planned_batches (deferred Planned mix) so both create the material + lot identically.

    The produced lot quantity is stored in the canonical base unit (grams for mass) — derived
    from the recipe's product_unit + declared dimension — so a finished recipe consuming this
    component deducts cleanly. Returns the new lot_id.

    The house-made material's is_organic is computed from the mix batch's own inputs (this
    batch_id's batch_materials), using the SAME derivation get_batches() applies to display a
    batch's organic flag. Storing it here is what lets organic-ness propagate one level up: a
    finished batch consuming this material reads rm.is_organic on it. Reusing an existing
    house-made material overwrites the flag with the latest batch's value.
    """
    product_unit, product_dimension = get_recipe_unit_and_dimension(product_name)
    product_unit = product_unit or 'units'
    # prefer the dimension the user declared on the recipe; fall back to inferring from the unit
    # text for legacy recipes created before product_dimension existed (NULL).
    dimension = product_dimension or units.dimension_of(product_unit)
    # CHANGED: lot-selection-unit fix — store the produced mass at full precision (like regular
    # lot receipts in app.py do), NOT round()ed to 4 dp. Rounding here threw away up to ~0.00005 g,
    # which made a recipe needing the exact produced amount (e.g. 11x16 lb == 176 lb of mix) read
    # as "insufficient". Display is handled by the disp/disp_full filters, so no float spew leaks
    # to the UI. Counts are stored as-is.
    lot_quantity = float(units.to_base(quantity, product_unit)) if dimension == 'mass' else quantity

    # Derive the mix's organic status from the inputs it just consumed — byte-identical to the
    # batch organic expression in get_batches() (batches.py ~line 331) so the house-made material
    # agrees with the mix batch's own displayed organic flag.
    db.execute(cursor, """
        SELECT CASE
                 WHEN COUNT(CASE WHEN rm.is_edible THEN 1 END) > 0
                  AND COUNT(CASE WHEN rm.is_edible AND NOT rm.is_organic THEN 1 END) = 0
               THEN 1 ELSE 0 END
        FROM batch_materials bm
        JOIN raw_materials rm ON bm.material_id = rm.material_id
        WHERE bm.batch_id = %s
    """, (batch_id,))
    is_organic = bool(cursor.fetchone()[0])

    existing_mix = get_raw_material(product_name)
    if existing_mix:
        mix_material_id = existing_mix[0]
        # overwrite with the latest batch's organic value (per "update to latest batch").
        # Also force is_edible TRUE: a house-made mix is a food product, and the organic flag
        # only propagates to finished batches if the mix counts as an edible input. Re-making the
        # component self-heals a stale is_edible (e.g. left FALSE on a row from before this logic).
        db.execute(cursor, """
            UPDATE raw_materials SET is_organic = %s, is_edible = %s WHERE material_id = %s
        """, (is_organic, True, mix_material_id))
    else:
        db.execute(cursor, """
            INSERT INTO raw_materials (name, category, unit, reorder_level, is_housemade, is_edible, is_organic, dimension)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (product_name, 'Mix', product_unit, 0, True, True, is_organic, dimension))
        mix_material_id = db.get_last_insert_id(cursor)

    # cost-of-component feature — give the house-made lot a cost_per_unit so the component's cost
    # rolls up into any finished batch that consumes it (the finished batch records this lot's
    # cost_per_unit into batch_materials, which feeds the batch_cost subquery in get_batches).
    # Basis: the mix's own input cost (what it consumed to be produced) ÷ produced base quantity.
    # Without this the lot had a NULL cost and the component contributed $0 to finished costs.
    db.execute(cursor, """
        SELECT COALESCE(SUM(quantity_used * COALESCE(cost_per_unit, 0)), 0)
        FROM batch_materials WHERE batch_id = %s
    """, (batch_id,))
    mix_input_cost = float(cursor.fetchone()[0] or 0)
    cost_per_unit = (mix_input_cost / float(lot_quantity)) if lot_quantity else 0

    lot_number = f"MIX-BATCH-{batch_number}"
    db.execute(cursor, """
        INSERT INTO raw_material_lots (lot_number, material_id, quantity, received_date, status, cost_per_unit)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (lot_number, mix_material_id, lot_quantity, received_date, 'active', cost_per_unit))
    if cursor.rowcount == 0:
        raise ValueError(f"Failed to create lot for mixed batch {batch_number}")
    return db.get_last_insert_id(cursor)

def add_to_batches(product_name, quantity, notes=None, batch_number=None, deduct_resources=True, expiration_date=None, planned_completion_date=None, batch_type='standard', allow_negative=False, lot_selections=None, deduction_mode='immediate'):
    """
    adds batch to ready to ship, but asks user if they want to deduct from resources, or just add it.
    If planned_completion_date is provided, batch is created with status 'Planned' instead of 'Ready'.
    batch_type can be 'standard', 'mix', or 'finished'.

    CHANGED: planned-deduction-mode feature — deferral is now the USER'S choice, not derived from
    batch_type. deduction_mode ('immediate' | 'deferred') only has an effect on a Planned
    finished/mix batch; 'standard' and any Ready batch always deduct at creation. A Planned mix
    produces its house-made output lot at promotion in BOTH modes: its inputs may be consumed now,
    but the mixed product does not exist as stock until the day it's actually made.


    lot number changes:
    1. mixed batches now insert into raw_material_lots table. with lot_number = MIX-BATCH-{batch_number}
    2. lot_selection paramater --> a paramater that is passed to funciton is a dictionary where the key is the material in recipe,
        each value to said key is a LIST each lot for the material, but each lot is in the form of a dicitonary.
        ex: lot_selections = {
            "Matcha": [
                {"lot_id": 1, "quantity_used": 10},
                {"lot_id": 2, "quantity_used": 5}
            ],
            "Sugar": [
                {"lot_id": 5, "quantity_used": 8}
            ]
        }

    3.
    """
    # Connect to the database
    db = get_db_connection()
    cursor = db.cursor()



    try:

        #get recipe data frame
        recipe_df = get_recipe(product_name)

        if recipe_df is None or recipe_df.empty:
            raise ValueError(
                f"Recipe '{product_name}' has no materials. "
                "A material may have been deleted — please recreate it or update the recipe."
            )

        # Determine status and date_completed based on whether a planned date was given
        if planned_completion_date:
            status = 'Planned'
            date_completed = None
        else:
            status = 'Ready'
            date_completed = datetime.now().strftime('%Y-%m-%d %H:%M:%S')


        # CHANGED: planned-deduction-mode feature — deferral used to be implied by
        # (batch_type in finished/mix AND status == 'Planned'). It is now the user's explicit
        # choice on the create form, so a Planned batch can consume its materials up front
        # ('immediate', the default) or reserve them for the planned date ('deferred').
        # deduction_mode is ignored for standard batches and for Ready batches, which always
        # deduct at creation — there is nothing to defer to.
        defer_deduction = (batch_type in ('finished', 'mix') and status == 'Planned' and deduction_mode == 'deferred')

        # Normalize what we persist so the column never claims a deferral that isn't happening.
        stored_deduction_mode = 'deferred' if defer_deduction else 'immediate'

        # serialize lot_selections to JSON for deferred batches so promote_planned_batches
        # can use the same user-picked lots at promotion time instead of falling back to FIFO.
        # JSON keys must be strings, so material_id ints become strings — we'll re-cast on read.
        # Immediate batches don't need this: their picks are already recorded as batch_materials
        # rows, which stay the single source of truth for what a batch consumed.
        planned_lot_selections_json = json.dumps(lot_selections) if (defer_deduction and lot_selections is not None) else None

        db.execute(cursor, """
            INSERT INTO batches (batch_number, product_name, quantity, date_completed, status, notes, expiration_date, planned_completion_date, batch_type, planned_lot_selections, deduction_mode)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (batch_number, product_name, quantity, date_completed, status, notes, expiration_date, planned_completion_date, batch_type, planned_lot_selections_json, stored_deduction_mode))
        batch_id = db.get_last_insert_id(cursor)



        #Deducting resources:


        if deduct_resources and not defer_deduction: # if defer_deduction, will be deducted later in promotion from planned.



            #validate lot selection is not None
            if lot_selections is None:
                raise ValueError("Lot selections must be provided for batch creation to validate stock and deduct from specific lots.")

            #check in there is needed resources
            for _, row in recipe_df.iterrows():
                material_id = row['material_id']
                material_name = row['material_name']
                # float() guards against Decimal (PostgreSQL numeric) * float (quantity) TypeError
                required_amount = float(row['quantity_needed']) * quantity
                # get recipe mats



                #validation section for material and lots:




                #get lot list for each material
                lot_list = lot_selections.get(material_id)
                if lot_list is None:
                    raise ValueError(f"No lot selection provided for material '{material_name}'")



                #validate total sum of lots selected matches neededamount for recipe
                # allow_negative is the user's confirmed "proceed anyway" override from the
                # negative-stock warning page: it skips the coverage requirement so a batch can be
                # created even when the selected lots don't fully cover the recipe (stock goes negative).
                material_sum = sum(lot['qty'] for lot in lot_list) # sum up the total quantity from the lots for this material
                if not allow_negative and not _quantities_match(material_sum, required_amount): # if the sum of the lots is less than the required amount, raise error before doing any deduction
                        raise ValueError(f"Total quantity from selected lots for material {material_name} is insufficient. Required: {required_amount}")



                # specific lot validation:
                for lot in lot_list:
                    lot_id = lot['lot_id']
                    qty = lot['qty']

                    #check each lot for validation
                    db.execute (cursor, """
                    SELECT quantity, cost_per_unit
                    FROM raw_material_lots
                    WHERE lot_id = %s AND material_id = %s AND status = 'active' AND (expiration_date IS NULL OR expiration_date > %s)

                        """, (lot_id, material_id, business_now().strftime('%Y-%m-%d %H:%M:%S')))  # CHANGED: server-timezone-vs-business-timezone fix
                    lot_row = cursor.fetchone() # we got our raw lot data

                    #existance validation
                    if not lot_row:
                        raise ValueError(f"Lot ID {lot_id} for material {material_name} not found or is not active/expired.")

                    available_qty = lot_row[0] #got  our quantity for lot

                    #quantity validation
                    # Compare with a small tolerance (matching the sum check above). The UI
                    # auto-fills a lot to *exactly* its available quantity when one lot can't
                    # cover the whole recipe need, and that value round-trips through a JS float.
                    # A strict `available_qty < qty` would then reject the lot you have exactly
                    # enough of, because float(qty) can land a sub-ULP above the stored Decimal.
                    #
                    # FUTURE (optional cleanup): this epsilon is the pragmatic fix. The principled
                    # one is to thread decimal.Decimal end-to-end (coerce form/JSON/DB values via
                    # Decimal(str(x)) at every boundary, do arithmetic in Decimal, and write the
                    # absolute new quantity instead of `SET quantity = quantity - %s`). Then the
                    # comparison is exact and no tolerance is needed. Scoped out for now; the 1e-6
                    # is safe (0.001 mg) for any realistic quantity. Same note applies to the
                    # promotion path's per-lot check in promote_planned_batches.
                    if not allow_negative and not _covers(available_qty, qty): # available must cover qty user wants from said lot
                        raise ValueError(f"Insufficient quantity in lot {lot_id} for material {material_name}. Requested: {qty}, Available: {available_qty}")

                    if allow_negative:
                        # Confirmed override: deduct exactly what the user asked for, even past the
                        # lot's available quantity (the lot is allowed to go negative).
                        deduct_qty = qty
                    else:
                        # If the request is within tolerance of the whole lot, deduct the lot
                        # exactly so we never leave a tiny negative residual from float drift.
                        deduct_qty = available_qty if float(qty) >= float(available_qty) else qty

                    cost_per_unit = lot_row[1] # got our cost per unit for lot, will need it for batch_materials log

                    # if we pass all validation, then we can do the deduction from the lots and materials.

                    # deduct immediately after validation passes for this lot
                    db.execute(cursor, """
                        UPDATE raw_material_lots
                        SET quantity = quantity - %s
                        WHERE lot_id = %s
                    """, (deduct_qty, lot_id))
                    #deduction

                    if cursor.rowcount == 0:
                        raise ValueError(f"Lot {lot_id} not found during deduction.")
                    #deduciton validation

                    # if the deduction left only a negligible residual, exhaust the lot
                    exhaust_lot_if_depleted(db, cursor, lot_id)

                    db.execute(cursor, """
                        INSERT INTO batch_materials (batch_id, material_id, lot_id, quantity_used, cost_per_unit)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (batch_id, material_id, lot_id, deduct_qty, cost_per_unit))
                    #log into batch_materials (record what was actually deducted)

                    #validaiton and deduciton done for lot



        # ALSO IF BATCH TYPE IS MIX, ADD THE MIXED PRODUCT TO RAW_MATERIALS WITH is_housemade = True, SO IT CAN BE USED IN FUTURE BATCHES.if not already in raw_materials,
        # otherwise if its already in raw_materials, just update the stock level by adding the quantity of the batch we just made.
        # CHANGED: planned-deduction-mode feature — guard was `not defer_deduction`. Now ANY Planned
        # mix defers its output lot to promotion, including an immediate-deduction one: consuming the
        # inputs today doesn't mean the mixed product exists today. It becomes available stock on its
        # planned completion date, when promote_planned_batches creates the lot.
        if batch_type == 'mix' and status == 'Ready':
            mix_lot_id = _create_housemade_lot(
                db, cursor, product_name, quantity, batch_number,
                datetime.now().strftime('%Y-%m-%d'), batch_id)
            db.execute(cursor, """
                UPDATE batches SET mix_lot_id = %s WHERE batch_id = %s
            """, (mix_lot_id, batch_id))

        db.commit()
        print(f"Added {quantity} units of {product_name} (Batch {batch_id})")
        return batch_id


    except ValueError as e:
        # Re-raise ValueError so app.py can catch it and show to user
        logging.error(f"Error adding to batches: {e}")
        db.rollback()
        raise  # ← Re-raises the ValueError to app.py

    except Exception as e:
        # If any error occurs during the process, print it and undo all changes
        logging.error(f"Error adding to batches: {e}")
        db.rollback()  # Rollback ensures database stays consistent
        return None

    finally:
        # Always close the database connection, even if an error occurred
        db.close()


def get_batches(page=None, per_page=50, sort='desc'):
    """Gets all batches ready to ship, ordered by date_completed (newest first by default)."""
    global _last_promote_time
    # sort feature — whitelist direction; never interpolate raw user input into SQL
    direction = 'ASC' if sort == 'asc' else 'DESC'
    now = time.time()
    # PRESERVED: time-gated side effect — promotes overdue planned batches at most once per 60 s
    if now - _last_promote_time > 60:
        promote_planned_batches()
        _last_promote_time = now

    db = get_db_connection()
    cursor = db.cursor()

    # ADDED: converted from pd.read_sql_query(query, db.conn) so LIMIT/OFFSET parameters
    #        can go through the db.execute %s→? wrapper.
    # CHANGED: organic feature — added derived is_organic flag (see subquery below).
    # ADDED: cost-of-material feature — batch_cost column added to columns list.
    columns = ['batch_id', 'batch_number', 'product_name', 'batch_type', 'quantity',
               'date_completed', 'notes', 'expiration_date', 'is_organic', 'batch_cost', 'remaining', 'product_unit',
               # mix-usage feature — only meaningful for 'mix' rows (NULL otherwise, see subqueries below).
               'mix_remaining', 'mix_consumed_count']
    # ADDED: organic feature — is_organic is a read-only derived property, NOT stored.
    #        A batch is organic when it used at least one edible material and every edible
    #        material it used is organic (non-edible materials are ignored). Computed via a
    #        correlated subquery so the flat SELECT/ORDER BY structure is untouched (no GROUP BY,
    #        no N+1). CASE WHEN <bool col> works for both SQLite 0/1 ints and Postgres booleans.
    # ADDED: cost-of-material feature — batch_cost sums quantity_used * cost_per_unit from batch_materials.
    #        Uses the same correlated subquery pattern as is_organic. COALESCE handles lots that
    #        had no cost_per_unit set at deduction time.
    base_query = f"""
    SELECT batch_id, batch_number, product_name, batch_type, quantity, date_completed, notes, expiration_date,
           (SELECT CASE
                     WHEN COUNT(CASE WHEN rm.is_edible THEN 1 END) > 0
                      AND COUNT(CASE WHEN rm.is_edible AND NOT rm.is_organic THEN 1 END) = 0
                   THEN 1 ELSE 0 END
            FROM batch_materials bm
            JOIN raw_materials rm ON bm.material_id = rm.material_id
            WHERE bm.batch_id = batches.batch_id) AS is_organic,
           (SELECT COALESCE(SUM(bm2.quantity_used * COALESCE(bm2.cost_per_unit, 0)), 0) -- cost-of-material feature
            FROM batch_materials bm2
            WHERE bm2.batch_id = batches.batch_id) AS batch_cost,
           -- partial-batch shipments: remaining = produced quantity minus what's already allocated to
           -- shipments. The shipment picker shows/caps against this, not the produced total.
           (quantity - (SELECT COALESCE(SUM(sb.quantity), 0)
                        FROM shipment_batches sb
                        WHERE sb.batch_id = batches.batch_id)) AS remaining,
           -- product unit-of-measurement, so the list can show the unit next to the quantity.
           -- CHANGED: renamed-recipe fix — the recipe lookup matches on free-text product_name,
           -- which a recipe rename silently breaks (renaming recipe 13 "Foxtail Cafe Style
           -- Matcha" -> "...V1" orphaned 43 existing mix batches). A NULL unit here makes the
           -- route skip its base->display conversion (app.py ~1268), so a mix row printed its
           -- output lot raw: "4000" instead of "4 kg".
           -- Fall back to the unit on the house-made material, reached through actual foreign
           -- keys (mix_lot_id -> lot.material_id -> raw_materials.unit). That material's unit
           -- was written by _create_housemade_lot() from the recipe's product_unit at creation
           -- time, i.e. the very unit the lot quantity was converted with — so it is both
           -- correct and immune to later renames. Recipe precedence is kept, so nothing changes
           -- for batches whose recipe still matches. Only reaches mix rows (mix_lot_id is NULL
           -- otherwise); standard/finished batches stay NULL when their recipe is renamed.
           COALESCE(
             (SELECT product_unit FROM recipes
              WHERE LOWER(recipes.product_name) = LOWER(batches.product_name) LIMIT 1),
             (SELECT rm.unit FROM raw_material_lots l
              JOIN raw_materials rm ON rm.material_id = l.material_id
              WHERE l.lot_id = batches.mix_lot_id)
           ) AS product_unit,
           -- mix-usage feature: for a Component (mix) batch, how much of its house-made output lot
           -- is still on hand (in BASE units — the route converts to product_unit) and how many
           -- finished batches have drawn from that lot. NULL for non-mix rows (mix_lot_id is NULL).
           (SELECT quantity FROM raw_material_lots
            WHERE raw_material_lots.lot_id = batches.mix_lot_id) AS mix_remaining,
           (SELECT COUNT(DISTINCT bm.batch_id) FROM batch_materials bm
            WHERE bm.lot_id = batches.mix_lot_id) AS mix_consumed_count
    FROM batches
    WHERE status IN ('Ready', 'Partially Shipped')
    ORDER BY date_completed {direction}, batch_id DESC
    """

    try:
        # ADDED: page=None → full table without LIMIT (used by export routes)
        if page is None:
            db.execute(cursor, base_query)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: count query so the route can compute total_pages
        db.execute(cursor, "SELECT COUNT(*) FROM batches WHERE status IN ('Ready', 'Partially Shipped')")
        total = cursor.fetchone()[0]

        # ADDED: LIMIT/OFFSET for pagination
        offset = (page - 1) * per_page
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", (per_page, offset))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error getting batches: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()


def get_batches_shipped(page=None, per_page=50, sort='desc'):
    """Gets all batches that have been shipped, ordered by date_shipped (newest first by default)."""
    db = get_db_connection()
    cursor = db.cursor()
    # sort feature — whitelist direction; never interpolate raw user input into SQL
    direction = 'ASC' if sort == 'asc' else 'DESC'

    # ADDED: converted from pd.read_sql_query to cursor approach for LIMIT/OFFSET support
    # CHANGED: organic feature — added derived is_organic flag (same correlated subquery as get_batches).
    # CHANGED: partial-batch shipments — driven off shipment_batches, so this returns ONE ROW PER
    #          ALLOCATION (a batch split across two shipments yields two rows). `quantity` is the
    #          amount shipped on that shipment (not the batch total), and shipment_id/number/date come
    #          from the joined shipment. `batch_quantity` carries the produced total for reference.
    columns = ['batch_id', 'batch_number', 'product_name', 'quantity', 'batch_quantity',
               'date_completed', 'date_shipped', 'notes', 'expiration_date', 'is_organic',
               'shipment_id', 'shipment_number']
    base_query = f"""
    SELECT b.batch_id, b.batch_number, b.product_name, sb.quantity, b.quantity,
           b.date_completed, s.date_shipped, b.notes, b.expiration_date,
           (SELECT CASE
                     WHEN COUNT(CASE WHEN rm.is_edible THEN 1 END) > 0
                      AND COUNT(CASE WHEN rm.is_edible AND NOT rm.is_organic THEN 1 END) = 0
                   THEN 1 ELSE 0 END
            FROM batch_materials bm
            JOIN raw_materials rm ON bm.material_id = rm.material_id
            WHERE bm.batch_id = b.batch_id) AS is_organic,
           s.shipment_id,
           s.shipment_number
    FROM shipment_batches sb
    JOIN batches b ON sb.batch_id = b.batch_id
    JOIN shipments s ON sb.shipment_id = s.shipment_id
    ORDER BY s.date_shipped {direction}, b.batch_id DESC
    """

    try:
        # ADDED: page=None → full table without LIMIT (used by export routes)
        if page is None:
            db.execute(cursor, base_query)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: count query so the route can compute total_pages
        db.execute(cursor, "SELECT COUNT(*) FROM shipment_batches")
        total = cursor.fetchone()[0]

        # ADDED: LIMIT/OFFSET for pagination
        offset = (page - 1) * per_page
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", (per_page, offset))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error getting shipped batches: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()

def mark_as_shipped(batch_id):
    """Marks a batch as shipped"""
    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, """
        UPDATE batches
        SET status = 'Shipped', date_shipped = %s
        WHERE batch_id = %s
        """, (datetime.now().strftime('%Y-%m-%d'), batch_id))


        if cursor.rowcount == 0: # make sure the batch id exists and was updated
            print(f"Batch ID {batch_id} not found. No batch marked as shipped.") # if doesnt exist, there was an error marking batch as shipped.
            db.rollback()
            return False


        db.commit()
        print(f" Marked batch {batch_id} as shipped")
        return True

    except Exception as e:
        logging.error(f" Error: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def delete_batch(batch_id, materials_to_reallocate=None, reallocate=False):
    """
    Deletes a batch from batches.
    Optionally reallocates raw materials back into inventory.
    materials_to_reallocate: list of dicts with keys 'material_id', 'quantity_used', 'material_name' for each material to reallocate back to inventory. Only used if reallocate is True.
    """

    db = get_db_connection()
    cursor = db.cursor()

    try:
        # Get batch info
        db.execute(cursor, """
            SELECT product_name, quantity, batch_type, mix_lot_id
            FROM batches
            WHERE batch_id = %s
        """, (batch_id,))
        row = cursor.fetchone()

        if not row:
            logging.info(f"Batch ID {batch_id} not found in batches.")
            return None

        product_name, quantity, batch_type, mix_lot_id = row

        # A shipped batch is referenced by shipment_batches; deleting it would
        # orphan the shipment (and is blocked by the FK on PostgreSQL). Refuse
        # explicitly so the route can tell the user why, instead of the generic
        # "may not exist" message from a swallowed FK error.
        db.execute(cursor, """
            SELECT 1 FROM shipment_batches WHERE batch_id = %s LIMIT 1
        """, (batch_id,))
        if cursor.fetchone():
            logging.info(f"Batch ID {batch_id} is referenced by a shipment; refusing to delete.")
            return "shipped"




        if reallocate and materials_to_reallocate: # if reallocate is True and there are materials to reallocate, add them back to inventory and then delete the batch and its materials. otherwise just delete batch and its materials without adding back to inventory.

            materials_added = []
            #get batch material info from batch id


            for mat in materials_to_reallocate: #for each material to reallocate, add back to stock level
                material_id = mat['material_id']
                material_name = mat['material_name']

                # get all lot rows for this material in this batch (may span multiple lots)
                db.execute(cursor, """
                    SELECT lot_id, quantity_used
                    FROM batch_materials
                    WHERE material_id = %s AND batch_id = %s
                """, (material_id, batch_id))
                lot_rows = cursor.fetchall()

                if not lot_rows:
                    raise ValueError(f"No batch_materials record found for material {material_name} in batch {batch_id}. Cannot reallocate.")

                for lot_id, qty_to_restore in lot_rows:
                    if lot_id is None:
                        raise ValueError(f"Lot ID for material {material_name} in batch {batch_id} is not tracked. Cannot reallocate.")

                    # look up lot_number for logging
                    db.execute(cursor, "SELECT lot_number FROM raw_material_lots WHERE lot_id = %s", (lot_id,))
                    rml_row = cursor.fetchone()
                    lot_number = rml_row[0] if rml_row else str(lot_id)


                    #Reallocate — also reactivate the lot, since it may have been
                    # marked inactive when the batch fully depleted it (exhaust_lot_if_depleted).
                    # Without this the restored quantity counts toward stock level but the lot
                    # stays unusable in dropdowns/deduction (which require status = 'active').
                    db.execute(cursor, """
                        UPDATE raw_material_lots
                        SET quantity = quantity + %s, status = 'active'
                        WHERE lot_id = %s
                    """, (qty_to_restore, lot_id))

                    materials_added.append({
                        "material": material_name,
                        "quantity_added": qty_to_restore,
                        "lot_number": lot_number
                    })
                
                

            # delete batch materials after adding to list
            db.execute(cursor, """
                DELETE
                FROM batch_materials
                WHERE batch_id = %s

                   """,(batch_id,))
            # Delete batch
            db.execute(cursor, """
                DELETE FROM batches
                WHERE batch_id = %s
            """, (batch_id,))
            #make sure it deleted it
            if cursor.rowcount == 0:
                raise ValueError(f"batch ID {batch_id} not found — nothing deleted")

            if batch_type == 'mix' and mix_lot_id:
                db.execute(cursor, """
                    UPDATE raw_material_lots
                    SET quantity = 0, status = 'inactive'
                    WHERE lot_id = %s
                """, (mix_lot_id,))

            db.commit()

            logging.info(
                f"Successfully deleted batch {batch_id}.\n"
                f"Reallocated materials: {materials_added}"
                )
            return True

        else:
            db.execute(cursor, """
                DELETE FROM batch_materials
                WHERE batch_id = %s
            """, (batch_id,))
            db.execute(cursor, """
                DELETE FROM batches
                WHERE batch_id = %s
            """, (batch_id,))
            #check it deleted
            if cursor.rowcount == 0:
                raise ValueError(f"batch ID {batch_id} not found — nothing deleted")

            if batch_type == 'mix' and mix_lot_id:
                db.execute(cursor, """
                    UPDATE raw_material_lots
                    SET quantity = 0, status = 'inactive'
                    WHERE lot_id = %s
                """, (mix_lot_id,))

            db.commit()
            logging.info(f"Successfully deleted batch {batch_id}.")
            return True




    except Exception as e:
        db.rollback()
        logging.error(f"Error deleting batch: {e}")
        return None

    finally:
        db.close()

def get_all_batches_with_id(page=None, per_page=50):
    """
    Get all batches including shipped ones, using batch_id

    used for manage page
    """
    # PRESERVED: unconditional promote_planned_batches() call — pagination does not affect this
    promote_planned_batches()
    db = get_db_connection()
    cursor = db.cursor()

    # ADDED: converted from pd.read_sql_query to cursor approach for LIMIT/OFFSET support
    # CHANGED: organic feature — added derived is_organic flag (same correlated subquery as
    #          get_batches/get_batches_shipped). Planned batches have no batch_materials yet, so
    #          this yields 0 for them; the manage-batches template shows a dash for Planned rows.
    # ADDED: planned-deduction-mode feature — deduction_mode lets manage_batches.html caption a
    #        Planned row's materials drawer as reserved-but-not-yet-deducted.
    columns = ['batch_id', 'batch_number', 'product_name', 'quantity', 'date_completed',
               'status', 'notes', 'date_shipped', 'expiration_date', 'planned_completion_date', 'batch_type',
               'promotion_failure_reason', 'deduction_mode', 'is_organic', 'product_unit']
    base_query = """
    SELECT batch_id, batch_number, product_name, quantity, date_completed, status, notes, date_shipped, expiration_date, planned_completion_date, batch_type,
           promotion_failure_reason, deduction_mode,
           (SELECT CASE
                     WHEN COUNT(CASE WHEN rm.is_edible THEN 1 END) > 0
                      AND COUNT(CASE WHEN rm.is_edible AND NOT rm.is_organic THEN 1 END) = 0
                   THEN 1 ELSE 0 END
            FROM batch_materials bm
            JOIN raw_materials rm ON bm.material_id = rm.material_id
            WHERE bm.batch_id = batches.batch_id) AS is_organic,
           -- product unit-of-measurement from the matching recipe (NULL when no recipe matches)
           (SELECT product_unit FROM recipes
            WHERE LOWER(recipes.product_name) = LOWER(batches.product_name) LIMIT 1) AS product_unit
    FROM batches
    ORDER BY date_completed DESC
    """

    try:
        # ADDED: page=None → full table without LIMIT (used by export routes)
        if page is None:
            db.execute(cursor, base_query)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: count query so the route can compute total_pages
        db.execute(cursor, "SELECT COUNT(*) FROM batches")
        total = cursor.fetchone()[0]

        # ADDED: LIMIT/OFFSET for pagination
        offset = (page - 1) * per_page
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", (per_page, offset))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"error getting all batches with id: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()

def get_batch_by_id(batch_id):
    """
    get batch using its id,
    for manage page.

    """

    db = get_db_connection()
    cursor = db.cursor()

    try:
        # ADDED: planned-deduction-mode feature — deduction_mode is appended LAST on purpose:
        #        edit_batch.html indexes this tuple positionally (batch[0]..batch[10]).
        query = """
        SELECT batch_id, product_name, quantity, date_completed, status, notes, date_shipped, expiration_date, planned_completion_date, batch_type, batch_number, deduction_mode
        FROM batches
        WHERE batch_id = %s

        """
        db.execute(cursor, query, (batch_id,))
        result = cursor.fetchone()
        return result

    except Exception as e:
        logging.error(f"error getting batch by id: {e}")

        return None

    finally:
        db.close()

def update_batch(batch_id, product_name=None, quantity=None, date_completed=None, notes=None, expiration_date=_UNSET, planned_completion_date=_UNSET, batch_number=None):
    """
    changes the details of batch, BESIDES STATUS, doesnt change status.

    note: similar to update_materials func

    for quantity change, must deduct from raw materials if quantity is increased, and add back to raw materials if quantity is decreased. will check for sufficient stock if quantity is increased.
    """

    db = get_db_connection()
    cursor = db.cursor()

    field = {} # dictionary to hold fields to update, only the ones that are not None

    #iterate through params, if not None, add to field dict to update
    for key, value in [("product_name", product_name), ("quantity", quantity), ("date_completed", date_completed), ("notes", notes), ("batch_number", batch_number)]:
        if value is not None:
            field[key] = value
    # expiration_date and planned_completion_date use sentinel so None can explicitly clear the field
    if expiration_date is not _UNSET:
        field["expiration_date"] = expiration_date

    if planned_completion_date is not _UNSET:
        field["planned_completion_date"] = planned_completion_date

    if not field: #empty dictionary, no fields to update
        logging.info("No fields to update")
        return

    invalid = set(field) - _BATCH_UPDATABLE_COLS
    if invalid:
        raise ValueError(f"Invalid column name(s): {invalid}")

    try:
        set_clause = ', '.join(f"{key} = %s" for key in field)
        params = (*field.values(), batch_id)
        db.execute(cursor, f"UPDATE batches SET {set_clause} WHERE batch_id = %s", params)

        if cursor.rowcount == 0:
                raise ValueError(f"batch ID {batch_id} not found — nothing updated")
        db.commit()
        logging.info(f"Updated batch with id:{batch_id}")

        return True

    except Exception as e:
        logging.error(f"Error changing batch details: {e}")
        db.rollback()
        return None

    finally:
        db.close()


def update_batch_status(batch_id, new_status):
    """
    changes the status of batch, only changes status, not other details.
    """

    db = get_db_connection()
    cursor = db.cursor()

    try:
        if new_status == 'Shipped':
            db.execute(cursor, """
            UPDATE batches
            SET status = %s, date_shipped = %s
            WHERE batch_id = %s
            """, (new_status, datetime.now().strftime('%Y-%m-%d'), batch_id))


        elif new_status == 'Planned':
            # Batch is not yet done — clear completion and ship dates
            db.execute(cursor, """
            UPDATE batches
            SET status = 'Planned', date_completed = NULL, date_shipped = NULL
            WHERE batch_id = %s
            """, (batch_id,))

        else:
            # Manually marking Ready — stamp the actual completion time
            db.execute(cursor, """
            UPDATE batches
            SET status = %s, date_shipped = NULL, date_completed = %s
            WHERE batch_id = %s
            """, (new_status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), batch_id))

        if cursor.rowcount == 0:
                raise ValueError(f"batch ID {batch_id} not found — nothing updated")

        db.commit()
        logging.info(f"Batch {batch_id} status updated to {new_status}")
        return True

    except Exception as e:
        logging.error(f"Error updating batch status: {e}")
        db.rollback()
        return None

    finally:
        db.close()

def _fetch_overdue(db, cursor, now):
    """
    Reads the Planned batches whose completion date has arrived.

    NOTE: this read is NOT a claim. Whatever it returns may already have been promoted by another
    worker by the time we act on it — see _claim_batch, which is the only thing that decides who
    gets to promote a batch. Extracted so tests can inject a deliberately stale row.
    """
    overdue_query = """
        SELECT batch_id, batch_number, product_name, quantity, batch_type, planned_completion_date, planned_lot_selections, deduction_mode
        FROM batches
        WHERE status = 'Planned'
        AND planned_completion_date IS NOT NULL
        AND planned_completion_date <= %s
    """
    if db.is_postgres:
        overdue_query += " FOR UPDATE SKIP LOCKED"
    db.execute(cursor, overdue_query, (now,))
    return cursor.fetchall()


def _claim_batch(db, cursor, batch_id, date_completed):
    """
    Atomically takes ownership of one overdue batch, flipping it Planned -> Ready.

    Returns True if THIS caller won the batch, False if someone else already promoted it.

    WHY THIS EXISTS (bug: planned batches were deducted twice).
    promote_planned_batches used to SELECT the overdue batches and then, with no further check,
    deduct their lots and flip status with an unconditional `WHERE batch_id = %s`. Python's sqlite3
    opens a transaction on the first INSERT/UPDATE, never on a SELECT, so that read was unprotected:
    two requests that both call promote (/batches via get_batches, /manage-batches via
    get_all_batches_with_id) could both read the same batch as Planned, then serialize their writes
    and each deduct the full amount — double deduction, duplicate batch_materials rows, and two
    house-made output lots for a mix.

    The `AND status = 'Planned'` predicate below is what fixes it: it is evaluated at write time,
    under the write lock, against committed data. Exactly one racer can match it; every other racer
    gets rowcount 0 and skips the batch. It must be the FIRST statement of the batch's transaction,
    so that a later rollback (insufficient stock) also releases the claim.
    """
    db.execute(cursor, """
        UPDATE batches
        SET status = 'Ready',
            date_completed = %s,
            promotion_failure_reason = NULL
        WHERE batch_id = %s AND status = 'Planned'
    """, (date_completed, batch_id))
    return cursor.rowcount > 0


def _has_batch_materials(db, cursor, batch_id):
    """True if the batch already has recorded deductions. A deferred batch must have none until it
    promotes, so any row means it was already processed — a second safety net behind _claim_batch."""
    db.execute(cursor, "SELECT 1 FROM batch_materials WHERE batch_id = %s LIMIT 1", (batch_id,))
    return cursor.fetchone() is not None


def _record_promotion_failure(db, cursor, batch_id, failure_reason):
    """Leaves the batch Planned with the reason it couldn't promote. Runs AFTER the claim has been
    rolled back, so it must re-assert nothing else — the row is Planned again at this point."""
    db.execute(cursor, """
        UPDATE batches
        SET promotion_failure_reason = %s
        WHERE batch_id = %s
    """, (failure_reason, batch_id))
    db.commit()


def _promote_one(db, cursor, batch, completion_date, lot_date):
    """
    Promotes ONE Planned batch to Ready inside the caller's connection: claim, allocate and deduct
    (deferred finished/mix only), create the mix output lot, commit.

    Shared by promote_planned_batches (planned date reached) and promote_batch_now (user clicked
    Mark Ready early) so both deduct stock identically. Previously the manual button only flipped
    the status, leaving a deferred batch Ready with nothing deducted and no batch_materials.

    batch: (batch_id, batch_number, product_name, quantity, batch_type,
            planned_lot_selections_json, deduction_mode)
    completion_date: stored as date_completed. lot_date: received date of a mix's output lot.

    Returns ('promoted', None) | ('skipped', None) lost the race / already processed |
            ('failed', reason) stock could not cover it — rolled back, batch left Planned.
    Exceptions propagate; the caller rolls back.
    """
    batch_id, batch_number, product_name, quantity, batch_type, planned_lot_selections_json, deduction_mode = batch
    now = business_today().strftime('%Y-%m-%d')

    # a finished/mix batch only owes a deduction here if the user chose to defer it. Rows
    # written before the planned-deduction-mode feature existed were backfilled to
    # 'deferred' by the migration, so a NULL/unknown value can only mean an immediate batch.
    is_deferred = (batch_type in ('finished', 'mix') and deduction_mode == 'deferred')

    # Take the batch before touching any stock. Lost the race -> another worker has
    # already deducted and promoted it; do nothing.
    if not _claim_batch(db, cursor, batch_id, completion_date):
        db.rollback()
        return 'skipped', None

    if is_deferred and _has_batch_materials(db, cursor, batch_id):
        logging.warning(f"Batch {batch_id} is deferred but already has batch_materials — skipping promotion")
        db.rollback()
        return 'skipped', None

    if batch_type == 'standard':
        # standard batches already had their raw_material_lots deducted at creation
        # time, so the claim is the whole promotion.
        pass

    # An immediate-deduction finished/mix batch already deducted its lots and wrote its
    # batch_materials rows at creation, so promotion has nothing to allocate and CANNOT
    # fail: there is no stock check to lose, and promotion_failure_reason can never be
    # set on this path. A mix still gets its house-made output lot here — that was
    # always deferred to the completion date.
    elif not is_deferred:
        if batch_type == 'mix':
            mix_lot_id = _create_housemade_lot(
                db, cursor, product_name, quantity, batch_number,
                lot_date, batch_id)
            db.execute(cursor, """
                UPDATE batches SET mix_lot_id = %s WHERE batch_id = %s
            """, (mix_lot_id, batch_id))

    # DEFERRED finished AND mix batches held back their input deduction at creation time
    # (defer_deduction=True in add_to_batches). Now at promotion we do the actual
    # lot-level deduction, mirroring add_to_batches' lot logic. A mix batch ADDITIONALLY
    # produces its house-made output lot here (deferred from creation too).
    else:
        recipe_df = get_recipe(product_name)
        if recipe_df is None or recipe_df.empty:
            db.rollback()  # release the claim: the batch stays Planned
            reason = f"No recipe found for {product_name}"
            _record_promotion_failure(db, cursor, batch_id, reason)
            return 'failed', reason

        failure_reason = None  # will be set if any material can't be covered by its lots
        all_allocations = {}   # material_id -> [(lot_id, qty_to_take, cost_per_unit)]

        # parse stored lot selections if the user picked specific lots at creation time.
        # JSON keys are always strings, so re-cast material_id back to int to match recipe_df.
        stored_lot_selections = None
        if planned_lot_selections_json:
            raw = json.loads(planned_lot_selections_json)
            stored_lot_selections = {int(k): v for k, v in raw.items()}

        # --- PASS 1: lot allocation + coverage check (reads only, no writes yet) ---
        for _, row in recipe_df.iterrows():
            material_id = row['material_id']
            material_name = row['material_name']
            # float() guards against Decimal (PostgreSQL numeric) * float (quantity) TypeError
            required = float(row['quantity_needed']) * quantity

            # Use the user-picked lots only when this material actually HAS a stored
            # selection. A material can be absent from stored_lot_selections when the
            # create-batch UI couldn't auto-fill it (no/insufficient active lots at
            # creation) — in that case fall through to FIFO, which reports the real
            # "Insufficient lot stock" reason instead of a misleading "no stored lot
            # selection" message (and still succeeds if other lots can cover it).
            if stored_lot_selections is not None and stored_lot_selections.get(material_id) is not None:
                # --- user-picked lots path: validate stored selections same as add_to_batches ---

                lot_list = stored_lot_selections.get(material_id)

                # total qty across stored lots must equal required (mirrors add_to_batches sum check)
                material_sum = sum(lot['qty'] for lot in lot_list)
                if not _quantities_match(material_sum, required):
                    failure_reason = (
                        f"Stored lot quantities for '{material_name}' don't match required "
                        f"(stored: {material_sum}, required: {required})"
                    )
                    break

                # validate each stored lot individually: must exist, be active, not expired, have enough qty
                allocations = []
                for lot in lot_list:
                    lot_id = lot['lot_id']
                    qty = lot['qty']
                    db.execute(cursor, """
                        SELECT quantity, cost_per_unit FROM raw_material_lots
                        WHERE lot_id = %s AND material_id = %s AND status = 'active'
                          AND (expiration_date IS NULL OR expiration_date > %s)
                    """, (lot_id, material_id, now))
                    lot_row = cursor.fetchone()
                    if not lot_row:
                        failure_reason = f"Lot {lot_id} for '{material_name}' not found, inactive, or expired."
                        break
                    # tolerant compare (mirrors add_to_batches): a lot auto-filled to its
                    # exact available qty round-trips through a JS float and can land a
                    # sub-ULP above the stored Decimal — don't reject what's exactly enough.
                    # See the Decimal-end-to-end FUTURE note in add_to_batches for the
                    # principled fix that would remove this epsilon.
                    if not _covers(lot_row[0], qty):
                        failure_reason = (
                            f"Lot {lot_id} for '{material_name}' has insufficient quantity "
                            f"(need {qty}, have {lot_row[0]})."
                        )
                        break
                    # clamp to the whole lot when within tolerance so we don't leave a
                    # tiny negative residual from float drift
                    take = lot_row[0] if float(qty) >= float(lot_row[0]) else qty
                    allocations.append((lot_id, take, lot_row[1]))

                if failure_reason:
                    break  # stop checking other materials

            else:
                # --- FIFO fallback path: auto-select oldest active lots first ---

                # fetch active, non-expired lots for this material, oldest first
                db.execute(cursor, """
                    SELECT lot_id, quantity, cost_per_unit FROM raw_material_lots
                    WHERE material_id = %s
                      AND status = 'active'
                      AND (expiration_date IS NULL OR expiration_date > %s)
                      AND quantity > 0
                    ORDER BY received_date ASC
                """, (material_id, now))
                lots = cursor.fetchall()

                # greedily fill required from lots oldest-first
                remaining = required
                allocations = []
                for lot_id, lot_qty, cost in lots:
                    if remaining <= 0:
                        break
                    take = min(lot_qty, remaining)
                    allocations.append((lot_id, take, cost))
                    remaining -= take

                if remaining > _QTY_EPSILON_G:
                    available = required - remaining
                    failure_reason = (
                        f"Insufficient lot stock: {material_name} "
                        f"(need {required}, have {round(available, 4)} across active lots)"
                    )
                    break  # no point checking other materials

            # both paths confirmed coverage for this material — store allocations for the write pass
            all_allocations[material_id] = allocations

        # --- handle failure: release the claim, leave batch Planned, record why ---
        if failure_reason:
            # rollback undoes the claim (status is Planned again) along with anything
            # PASS 1 touched, so the reason is written to a batch that really is Planned.
            db.rollback()
            _record_promotion_failure(db, cursor, batch_id, failure_reason)
            logging.warning(f"Batch {batch_id} could not promote: {failure_reason}")
            return 'failed', failure_reason

        # --- PASS 2: deduct from lots + insert batch_materials (writes) ---
        # all materials passed the coverage check, so now we do the actual deductions.
        # for each allocated lot: subtract the taken quantity and log it in batch_materials with lot_id.
        for _, row in recipe_df.iterrows():
            material_id = row['material_id']
            for lot_id, take, cost in all_allocations[material_id]:

                # deduct the allocated quantity from this specific lot
                db.execute(cursor, """
                    UPDATE raw_material_lots
                    SET quantity = quantity - %s
                    WHERE lot_id = %s
                """, (take, lot_id))

                # if the deduction left only a negligible residual, exhaust the lot
                exhaust_lot_if_depleted(db, cursor, lot_id)

                # record the usage in batch_materials with lot_id so we have full traceability
                db.execute(cursor, """
                    INSERT INTO batch_materials (batch_id, material_id, lot_id, quantity_used, cost_per_unit)
                    VALUES (%s, %s, %s, %s, %s)
                """, (batch_id, material_id, lot_id, take, cost))

        # A mix (Component) batch also produces its house-made output lot — deferred
        # from creation along with the input deduction. Create it now (and the
        # raw_materials row if this product is new), then record it on the batch.
        if batch_type == 'mix':
            mix_lot_id = _create_housemade_lot(
                db, cursor, product_name, quantity, batch_number,
                lot_date, batch_id)
            db.execute(cursor, """
                UPDATE batches SET mix_lot_id = %s WHERE batch_id = %s
            """, (mix_lot_id, batch_id))

    # The claim already set status/date_completed; commit this batch on its own so a
    # later batch failing to promote can't undo it.

    # The claim already set status/date_completed; commit this batch on its own so a
    # later batch failing to promote can't undo it.
    db.commit()
    return 'promoted', None


def promote_batch_now(batch_id):
    """
    Manual "Mark Ready" for a Planned batch before (or on) its planned date. Runs the same
    promotion as the automatic path, so a deferred batch deducts its lots and records
    batch_materials instead of just changing status.

    Returns (True, None) on success, else (False, reason). On a stock shortfall the batch stays
    Planned with promotion_failure_reason set; a batch that is not Planned (already promoted,
    double-click) is reported as not promotable and nothing is touched.
    """
    db = get_db_connection()
    cursor = db.cursor()
    promoted = None
    try:
        db.execute(cursor, """
            SELECT batch_id, batch_number, product_name, quantity, batch_type,
                   planned_lot_selections, deduction_mode, status
            FROM batches WHERE batch_id = %s
        """, (batch_id,))
        row = cursor.fetchone()
        if row is None or row[7] != 'Planned':
            return False, "Batch is not in Planned status."

        today = business_today().strftime('%Y-%m-%d')
        outcome, reason = _promote_one(
            db, cursor, row[:7],
            completion_date=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), lot_date=today)
        if outcome == 'promoted':
            promoted = (row[0], row[2], row[4], row[6])
            return True, None
        return False, reason or "Batch is not in Planned status."

    except Exception as e:
        logging.error(f"Error promoting batch {batch_id} manually: {e}")
        db.rollback()
        return False, "Unexpected error while completing the batch."

    finally:
        db.close()
        # Audit AFTER the write connection is closed (see promote_planned_batches).
        if promoted:
            log_action('planned_batch_promoted',
                       f"batch_id={promoted[0]}, product={promoted[1]}, type={promoted[2]}, "
                       f"deduction={promoted[3]}, manual=True")



def promote_planned_batches():

    """
    Promotes overdue Planned batches to Ready.
    Called lazily from get_batches() and get_all_batches_with_id().

    Which batches still owe a deduction at promotion is decided by batches.deduction_mode, not by
    batch_type:
    - standard: flip status directly (always deducted at creation).
    - finished/mix, deduction_mode='immediate': flip status directly (deducted at creation, and
      batch_materials rows already written).
    - finished/mix, deduction_mode='deferred': deduct from lots now and promote; leave as Planned
      with a promotion_failure_reason if stock is insufficient.

    For a DEFERRED batch, lot deduction uses one of two paths depending on what was stored at creation:
    1. stored lots (planned_lot_selections JSON column): user picked specific lots when creating the batch —
       validate and deduct from exactly those lots, same logic as add_to_batches.
    2. FIFO fallback: no stored selections — automatically deduct from oldest active lots first.

    For mix batches (BOTH modes): also inserts a new lot into raw_material_lots for the produced
    quantity so the housemade material is available for use in future batches. The output lot is
    always born here, because the mixed product doesn't exist as stock until its completion date.

    CONCURRENCY (this function runs lot deductions from GET handlers, under several workers):
    every batch is claimed with _claim_batch before any stock moves, and each batch gets its OWN
    transaction — so a batch that can't be covered rolls back its claim alone, without discarding
    the batches already promoted in this pass.
    """


    db = get_db_connection()
    cursor = db.cursor()
    # Audit rows are written after each batch's transaction commits. log_action opens a SECOND
    # connection; while our transaction holds SQLite's write lock, its INSERT fails with "database
    # is locked" and audit.py swallows the error — which is why no promotion was ever audited.
    promoted = []
    try:
        # CHANGED: server-timezone-vs-business-timezone fix — was datetime.now().strftime(...), the
        # server's own UTC clock on Render. Botaniks runs Pacific: for several hours every evening
        # the server's UTC calendar date is already a day ahead, so a batch planned for Pacific
        # "tomorrow" was already <= the server's UTC "today" and got promoted immediately. Also
        # feeds the PASS-1/FIFO lot-expiration checks below via this same variable.
        now = business_today().strftime('%Y-%m-%d')
        overdue = _fetch_overdue(db, cursor, now)

        for batch_id, batch_number, product_name, quantity, batch_type, planned_completion_date, planned_lot_selections_json, deduction_mode in overdue:
            try:
                outcome, _ = _promote_one(
                    db, cursor,
                    (batch_id, batch_number, product_name, quantity, batch_type,
                     planned_lot_selections_json, deduction_mode),
                    completion_date=planned_completion_date, lot_date=planned_completion_date)
                if outcome == 'promoted':
                    promoted.append((batch_id, product_name, batch_type, deduction_mode))

            except Exception as e:
                # One bad batch must not abort the rest of the pass.
                logging.error(f"Error promoting batch {batch_id}: {e}")
                db.rollback()

    except Exception as e:
        logging.error(f"Error transitioning planned batches: {e}")
        db.rollback()

    finally:
        db.close()

    # Audit AFTER the write connection is closed: log_action opens its own connection, and on SQLite
    # it cannot insert while this one holds the write lock.
    for batch_id, product_name, batch_type, deduction_mode in promoted:
        log_action('planned_batch_promoted',
                   f"batch_id={batch_id}, product={product_name}, type={batch_type}, deduction={deduction_mode}")


def get_batches_planned(page=None, per_page=50, sort='desc'):
    """Gets all Planned batches ordered by planned_completion_date (newest first by default)."""
    db = get_db_connection()
    cursor = db.cursor()
    # sort feature — whitelist direction; never interpolate raw user input into SQL
    direction = 'ASC' if sort == 'asc' else 'DESC'

    # ADDED: converted from pd.read_sql_query to cursor approach for LIMIT/OFFSET support
    # ADDED: planned-deduction-mode feature — deduction_mode drives the "Deducted" / "Not yet
    #        deducted" column on the Planned tabs of batches.html.
    columns = ['batch_id', 'batch_number', 'product_name', 'batch_type', 'quantity',
               'planned_completion_date', 'notes', 'expiration_date', 'promotion_failure_reason',
               'deduction_mode', 'product_unit']
    base_query = f"""
    SELECT batch_id, batch_number, product_name, batch_type, quantity, planned_completion_date, notes, expiration_date, promotion_failure_reason,
           deduction_mode,
           -- product unit-of-measurement from the matching recipe (NULL when no recipe matches)
           (SELECT product_unit FROM recipes
            WHERE LOWER(recipes.product_name) = LOWER(batches.product_name) LIMIT 1) AS product_unit
    FROM batches
    WHERE status = 'Planned'
    ORDER BY planned_completion_date {direction}, batch_id DESC
    """

    try:
        # ADDED: page=None → full table without LIMIT (no planned-batch export route today,
        #        but sentinel keeps the signature consistent with the other three functions)
        if page is None:
            db.execute(cursor, base_query)
            result = cursor.fetchall()
            return (pd.DataFrame(result, columns=columns), None)

        # ADDED: count query so the route can compute total_pages
        db.execute(cursor, "SELECT COUNT(*) FROM batches WHERE status = 'Planned'")
        total = cursor.fetchone()[0]

        # ADDED: LIMIT/OFFSET for pagination
        offset = (page - 1) * per_page
        db.execute(cursor, base_query + " LIMIT %s OFFSET %s", (per_page, offset))
        result = cursor.fetchall()
        return (pd.DataFrame(result, columns=columns), total)

    except Exception as e:
        logging.error(f"Error getting planned batches: {e}")
        return (pd.DataFrame(), 0)

    finally:
        db.close()


def get_batch_materials(batch_id):
    """
    Gets all batch materials for specific batch_id

    will return: material name, quantity used, unit, lot_id (not visible to user), lot_number, and material_id(not visible to user)

    """
    db = get_db_connection()
    cursor = db.cursor()

    try:

        # ADDED: cost-of-material feature — include cost_per_unit (r[6]) so the API route can
        #        expose it to the batch materials expansion panel in batches.html.
        query="""
        SELECT rm.name AS material_name, bm.quantity_used, rm.unit, bm.lot_id, rml.lot_number, bm.material_id, bm.cost_per_unit
        FROM batch_materials bm
        JOIN raw_materials rm ON bm.material_id = rm.material_id
        JOIN raw_material_lots rml ON bm.lot_id = rml.lot_id
        WHERE bm.batch_id = %s
        ORDER BY rm.name ASC
        """
        db.execute(cursor, query, (batch_id,))
        result = cursor.fetchall()
        return result if result else []

    except Exception as e:
        logging.error(f"Error getting batch materials: {e}")
        return []

    finally:
        db.close()


# ADDED: planned-deduction-mode feature — a deferred Planned batch has NO batch_materials rows
# (nothing has been deducted yet); its lot picks live only in the planned_lot_selections JSON
# column. These three helpers make those picks readable and editable before promotion, so the
# batches-page drawer and the edit page aren't blank for a batch whose lots were clearly chosen.

def get_planned_lot_selections(batch_id):
    """
    Reads the planned (not-yet-deducted) lot picks of a deferred Planned batch.

    Returns rows in the SAME shape as get_batch_materials, plus an 8th element:
        (material_name, quantity_used, unit, lot_id, lot_number, material_id, cost_per_unit, lot_status)
    so callers can reuse the same display path. quantity_used is the RESERVED quantity in the base
    unit (grams), matching batch_materials.quantity_used.

    lot_status is 'active' when the lot is still usable, otherwise a reason ('unavailable' /
    'missing'). Stale rows are returned rather than dropped: a lot that has since been exhausted or
    expired is exactly what the user needs to see and re-pick before the batch fails to promote.

    Returns [] when the batch has no stored picks (promotion will fall back to FIFO).
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, "SELECT planned_lot_selections FROM batches WHERE batch_id = %s", (batch_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            return []

        # JSON keys are always strings — re-cast material_id back to int, as promote does.
        stored = {int(k): v for k, v in json.loads(row[0]).items()}
        now = business_now().strftime('%Y-%m-%d %H:%M:%S')  # CHANGED: server-timezone-vs-business-timezone fix

        rows = []
        for material_id, lot_list in stored.items():
            for lot in lot_list:
                db.execute(cursor, """
                    SELECT rm.name, rm.unit, rml.lot_number, rml.cost_per_unit,
                           CASE WHEN rml.status = 'active'
                                 AND (rml.expiration_date IS NULL OR rml.expiration_date > %s)
                                THEN 'active' ELSE 'unavailable' END
                    FROM raw_materials rm
                    LEFT JOIN raw_material_lots rml
                           ON rml.lot_id = %s AND rml.material_id = rm.material_id
                    WHERE rm.material_id = %s
                """, (now, lot['lot_id'], material_id))
                info = cursor.fetchone()
                if not info:
                    continue  # material itself was deleted — nothing meaningful to show
                name, unit, lot_number, cost_per_unit, lot_status = info
                if lot_number is None:
                    lot_number, lot_status = f"Lot {lot['lot_id']}", 'missing'
                rows.append((name, lot['qty'], unit, lot['lot_id'], lot_number,
                             material_id, cost_per_unit, lot_status))

        rows.sort(key=lambda r: (r[0] or '', r[4] or ''))
        return rows

    except Exception as e:
        logging.error(f"Error getting planned lot selections for batch {batch_id}: {e}")
        return []

    finally:
        db.close()


def update_planned_lot_selections(batch_id, lot_selections):
    """
    Replaces the stored lot picks of a deferred Planned batch. Pass None/{} to clear them.

    Only valid on a Planned + deduction_mode='deferred' batch: any other batch has already moved
    stock, and its consumption is recorded in batch_materials — editing it there (adjust_batch_material)
    is what actually returns quantity to the lots.

    Coverage is NOT enforced: a planned batch may deliberately under-reserve against stock that
    hasn't arrived yet, and promotion falls back to FIFO for any material without picks.

    Raises ValueError on an unknown batch, a non-deferred batch, or a lot that doesn't exist / isn't
    active / is expired / doesn't belong to the material it's claimed under.
    Returns True on success.
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, "SELECT status, deduction_mode FROM batches WHERE batch_id = %s", (batch_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Batch {batch_id} not found")
        status, deduction_mode = row
        if status != 'Planned' or deduction_mode != 'deferred':
            raise ValueError(
                f"Batch {batch_id} is not a deferred Planned batch (status={status}, "
                f"deduction_mode={deduction_mode}) — its materials are already deducted."
            )

        if not lot_selections:
            db.execute(cursor, "UPDATE batches SET planned_lot_selections = NULL WHERE batch_id = %s", (batch_id,))
            db.commit()
            log_action('planned_lots_updated', f"batch_id={batch_id}, cleared")
            return True

        now = business_now().strftime('%Y-%m-%d %H:%M:%S')  # CHANGED: server-timezone-vs-business-timezone fix
        for material_id, lot_list in lot_selections.items():
            for lot in lot_list:
                qty = float(lot['qty'])
                if qty <= 0:
                    raise ValueError(f"Lot {lot['lot_id']} has a non-positive quantity")
                # Same existence/active/expiry/ownership check promotion will run (see PASS 1) —
                # fail here, where the user can fix it, rather than silently at the planned date.
                db.execute(cursor, """
                    SELECT 1 FROM raw_material_lots
                    WHERE lot_id = %s AND material_id = %s AND status = 'active'
                      AND (expiration_date IS NULL OR expiration_date > %s)
                """, (lot['lot_id'], material_id, now))
                if not cursor.fetchone():
                    raise ValueError(f"Lot {lot['lot_id']} is not an active, unexpired lot of this material")

        # JSON keys must be strings; promote_planned_batches re-casts them to int on read.
        payload = json.dumps({str(k): v for k, v in lot_selections.items()})
        db.execute(cursor, "UPDATE batches SET planned_lot_selections = %s WHERE batch_id = %s", (payload, batch_id))
        if cursor.rowcount == 0:
            raise ValueError(f"Batch {batch_id} not found during update")
        db.commit()
        log_action('planned_lots_updated', f"batch_id={batch_id}, materials={list(lot_selections)}")
        return True

    except ValueError:
        db.rollback()
        raise

    except Exception as e:
        logging.error(f"Error updating planned lot selections for batch {batch_id}: {e}")
        db.rollback()
        return False

    finally:
        db.close()


def clear_planned_lot_selections(batch_id):
    """
    Drops the stored lot picks of a batch (no stock is touched — nothing was deducted).

    Called when a deferred Planned batch's quantity changes: promotion validates each material's
    stored per-lot sum against quantity_needed * quantity, so picks made for the old quantity would
    strand the batch as Planned with a "don't match required" promotion_failure_reason. Clearing
    them means the user re-picks, or FIFO covers it at the planned date.
    """
    db = get_db_connection()
    cursor = db.cursor()
    try:
        db.execute(cursor, "UPDATE batches SET planned_lot_selections = NULL WHERE batch_id = %s", (batch_id,))
        db.commit()
        return True
    except Exception as e:
        logging.error(f"Error clearing planned lot selections for batch {batch_id}: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def get_batch_materials_for_reallocation(batch_id):
    """
    Returns the distinct materials on a batch as a list of dicts
    {'material_id', 'material_name', 'quantity_used'} suitable for passing to
    delete_batch(..., reallocate=True). One row per material (deduplicated across
    lots) — delete_batch reads the real per-lot quantities back from the DB, so
    quantity_used here is only the summed total for callers that want it.
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        db.execute(cursor, """
            SELECT bm.material_id, rm.name AS material_name, SUM(bm.quantity_used)
            FROM batch_materials bm
            JOIN raw_materials rm ON bm.material_id = rm.material_id
            WHERE bm.batch_id = %s
            GROUP BY bm.material_id, rm.name
        """, (batch_id,))
        rows = cursor.fetchall()
        return [
            {'material_id': r[0], 'material_name': r[1], 'quantity_used': r[2]}
            for r in rows
        ]
    except Exception as e:
        logging.error(f"Error getting batch materials for reallocation: {e}")
        return []
    finally:
        db.close()


def adjust_batch_material(batch_id, new_quantities: dict, lot_selections: dict = None):
    """
    Updates batch_materials.quantity_used and applies delta to raw_materials.stock_level.
    new_quantities: {material_id (int): new_quantity_used (float)}
    lot_selections: {material_id (int): lot_id (int)} — lot to deduct from when delta > 0
    Delta logic: stock_level -= (new - old), so increasing qty deducts more, decreasing returns stock.
    When lot_selections provided: also tracks deduction/return at the lot level.
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        for material_id, new_qty in new_quantities.items():
            db.execute(cursor, """
                SELECT quantity_used, lot_id FROM batch_materials
                WHERE batch_id = %s AND material_id = %s
            """, (batch_id, material_id))
            row = cursor.fetchone()
            if row is None:
                logging.warning(f"No batch_material record for batch {batch_id}, material {material_id} — aborting")
                db.rollback()
                return None

            old_qty = row[0]
            original_lot_id = row[1]
            delta = new_qty - old_qty

            if delta > 0:
                lot_id = (lot_selections or {}).get(material_id, original_lot_id)
                if lot_id:
                    db.execute(cursor, """
                        SELECT quantity FROM raw_material_lots WHERE lot_id = %s
                    """, (lot_id,))
                    lot_row = cursor.fetchone()
                    if not lot_row or lot_row[0] < delta:
                        logging.warning(f"Insufficient lot stock for material {material_id} in lot {lot_id} for batch {batch_id}: need {delta}, have {lot_row[0] if lot_row else 0} — aborting")
                        db.rollback()
                        return None
                    db.execute(cursor, """
                        UPDATE raw_material_lots SET quantity = quantity - %s WHERE lot_id = %s
                    """, (delta, lot_id))
                    # if the deduction left only a negligible residual, exhaust the lot
                    exhaust_lot_if_depleted(db, cursor, lot_id)
                else:
                    db.execute(cursor, """
                        SELECT COALESCE(SUM(quantity), 0) FROM raw_material_lots WHERE material_id = %s
                    """, (material_id,))
                    total = cursor.fetchone()[0]
                    if total < delta:
                        logging.warning(f"Insufficient total lot stock for material {material_id} for batch {batch_id}: need {delta}, have {total} — aborting")
                        db.rollback()
                        return None

            elif delta < 0:
                if original_lot_id:
                    db.execute(cursor, """
                        UPDATE raw_material_lots SET quantity = quantity + %s WHERE lot_id = %s
                    """, (abs(delta), original_lot_id))

            db.execute(cursor, """
                UPDATE batch_materials SET quantity_used = %s
                WHERE batch_id = %s AND material_id = %s
            """, (new_qty, batch_id, material_id))

        db.commit()
        logging.info(f"Adjusted materials for batch {batch_id} with changes: {new_quantities}")
        return True

    except Exception as e:
        logging.error(f"Error adjusting batch materials: {e}")
        db.rollback()
        return None

    finally:
        db.close()


def check_batch_materials_stock(batch_id, new_quantities: dict, lot_selections: dict = None):
    """
    Checks if new quantities for batch materials are available in stock before adjusting.
    lot_selections: {material_id: lot_id} — when provided, also validates the specific lot has enough.
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        for material_id, new_qty in new_quantities.items():
            db.execute(cursor, """
                SELECT quantity_used, lot_id FROM batch_materials
                WHERE batch_id = %s AND material_id = %s
            """, (batch_id, material_id))
            row = cursor.fetchone()
            if row is None:
                logging.warning(f"No batch_material record for batch {batch_id}, material {material_id} during stock check")
                return False

            old_qty = row[0]
            original_lot_id = row[1]
            delta = new_qty - old_qty

            if delta > 0:
                lot_id = (lot_selections or {}).get(material_id, original_lot_id)
                if lot_id:
                    db.execute(cursor, """
                        SELECT quantity FROM raw_material_lots WHERE lot_id = %s
                    """, (lot_id,))
                    lot_row = cursor.fetchone()
                    if not lot_row or not _covers(lot_row[0], delta):
                        logging.warning(f"Lot {lot_id} has insufficient quantity for material {material_id}: need {delta}, have {lot_row[0] if lot_row else 0}")
                        return False
                else:
                    db.execute(cursor, """
                        SELECT COALESCE(SUM(quantity), 0) FROM raw_material_lots WHERE material_id = %s
                    """, (material_id,))
                    total = cursor.fetchone()[0]
                    if not _covers(total, delta):
                        logging.warning(f"Insufficient total lot stock for material {material_id}: need {delta}, have {total}")
                        return False

        return True

    except Exception as e:
        logging.error(f"Error checking batch materials stock: {e}")
        return False

    finally:
        db.close()
