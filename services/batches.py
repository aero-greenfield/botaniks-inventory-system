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

# ADDED: batches.py calls get_recipe (for deduction and promotion), which lives in its own
#        service module now, so we import it explicitly.
# REMOVED: edit-path safety fix — get_raw_material import; the mix output material is now
#          resolved by _resolve_housemade_material on the caller's transaction.
from services.recipes import get_recipe, get_recipe_unit_and_dimension
from services.lots import exhaust_lot_if_depleted
# ADDED: edit-path safety fix — a quantity edit must re-derive shipment status the same way
# shipments do. services.shipments imports nothing from this module, so there is no cycle.
from services.shipments import _recompute_batch_status

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


class BatchReconcileError(ValueError):
    """A batch quantity edit cannot be honoured — insufficient stock to cover an increase, or a
    new quantity below what has already been consumed from the batch's output lot.

    ADDED: quantity-edit-desync fix. Distinct from the other ValueErrors update_batch can raise
    (e.g. "batch ID not found") so that those keep their existing contract of being swallowed to
    a None return, while a reconciliation refusal carries its specific, user-facing reason up to
    the route. Subclasses ValueError so any caller already catching ValueError still works.
    """

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

def _resolve_housemade_material(db, cursor, product_name):
    """Find the house-made material a mix product produces into. Returns (material_id or None,
    recipe_id or None); None material means "create one". Uses the CALLER's transaction.

    ADDED: edit-path safety fix. Order of trust:
      1. recipes.housemade_material_id — set the first time the recipe produces, survives renames.
      2. A house-made material with the product's name — recipes made before this fix, not yet linked.
      3. A material NOT flagged house-made with that name is reused only if it already holds this
         product's MIX-BATCH lots (production's Vendi Bean row is exactly that). Any other
         same-named material is a purchased one: refuse rather than pour mix output into it.
    """
    db.execute(cursor, """
        SELECT recipe_id, housemade_material_id FROM recipes
        WHERE LOWER(product_name) = LOWER(%s) LIMIT 1
    """, (product_name,))
    recipe_row = cursor.fetchone()
    recipe_id, linked_id = (recipe_row[0], recipe_row[1]) if recipe_row else (None, None)

    if linked_id is not None:
        db.execute(cursor, "SELECT 1 FROM raw_materials WHERE material_id = %s", (linked_id,))
        if cursor.fetchone():
            return linked_id, recipe_id
        # linked material was deleted — fall through and resolve again

    db.execute(cursor, """
        SELECT material_id, is_housemade FROM raw_materials WHERE LOWER(name) = LOWER(%s)
    """, (product_name,))
    material_row = cursor.fetchone()
    if material_row is None:
        return None, recipe_id
    material_id, is_housemade = material_row
    if is_housemade:
        return material_id, recipe_id

    db.execute(cursor, """
        SELECT 1 FROM raw_material_lots WHERE material_id = %s AND lot_number LIKE %s LIMIT 1
    """, (material_id, 'MIX-BATCH-%'))
    if cursor.fetchone():
        return material_id, recipe_id

    raise ValueError(
        f"A purchased material is already named '{product_name}', so this component's output "
        f"can't be stored under that name. Rename the purchased material (or the recipe) first.")


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

    # CHANGED: edit-path safety fix — was get_raw_material(product_name), a bare name lookup. Renaming
    # a recipe made the next mix batch create a NEW material (production: Foxtail V2 split from the
    # materials finished recipes consume), and a purchased material sharing the name was silently
    # reused as the output (production: Vendi Bean). The recipe now remembers its material by id.
    mix_material_id, recipe_id = _resolve_housemade_material(db, cursor, product_name)
    if mix_material_id is not None:
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

    # ADDED: edit-path safety fix — remember the link so later renames keep feeding this material.
    if recipe_id is not None:
        db.execute(cursor, """
            UPDATE recipes SET housemade_material_id = %s WHERE recipe_id = %s
        """, (mix_material_id, recipe_id))

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

def _reconcile_mix_output_lot(db, cursor, batch_id, new_quantity):
    """Resize a mix batch's house-made OUTPUT lot after its quantity was edited, using the
    CALLER's transaction.

    ADDED: quantity-edit-desync fix. update_batch used to be a bare `UPDATE batches` that never
    touched mix_lot_id, so editing a Component's quantity left its output lot reflecting the
    ORIGINAL amount — in both directions: edited down left surplus stock in the lot, edited up
    left the lot short and starved the finished batches that consume it. services/lots.py
    refuses the mirror-image edit from the lot side, pointing at "the batch field" — here.
    Precedent for a batch-side write to this lot is delete_batch, which zeroes it.

    The lot's `quantity` column is what REMAINS, not what was produced, and nothing stores the
    produced figure — so it is derived:

        produced = remaining + SUM(batch_materials.quantity_used for this lot)

    Unit/dimension are resolved through the FOREIGN KEYS (mix_lot_id -> lot.material_id ->
    raw_materials), deliberately NOT via get_recipe_unit_and_dimension(product_name): batches
    reference recipes by free-text name, and a recipe rename has already orphaned 43 production
    mix batches. A name lookup returns None for those, falls back to 'units'/'count', skips the
    mass conversion, and would compute a wildly wrong remainder (e.g. 236 - 236000). This is the
    same FK chain get_batches() uses to recover a renamed batch's display unit.

    Returns None when there is nothing to reconcile (not a mix batch, or a Planned mix whose
    output lot is not created until promotion). Raises ValueError if the new quantity is below
    what has already been consumed from the lot.
    """
    db.execute(cursor, """
        SELECT b.mix_lot_id, l.quantity, rm.unit, rm.dimension
        FROM batches b
        JOIN raw_material_lots l ON l.lot_id = b.mix_lot_id
        JOIN raw_materials rm    ON rm.material_id = l.material_id
        WHERE b.batch_id = %s AND b.batch_type = 'mix'
    """, (batch_id,))
    row = cursor.fetchone()
    if row is None:
        return None

    lot_id, remaining, unit, dimension = row
    unit = unit or 'units'
    dimension = dimension or units.dimension_of(unit)

    db.execute(cursor, """
        SELECT COALESCE(SUM(quantity_used), 0) FROM batch_materials WHERE lot_id = %s
    """, (lot_id,))
    consumed = float(cursor.fetchone()[0] or 0)

    new_produced = float(units.to_base(new_quantity, unit)) if dimension == 'mass' else float(new_quantity)
    new_remaining = new_produced - consumed

    if new_remaining < -_QTY_EPSILON_G:
        # Report in the user's display unit — the base-unit grams would be meaningless on screen.
        raise BatchReconcileError(
            f"Cannot reduce this batch to {units.from_base(new_produced, unit):g} {unit}: "
            f"{units.from_base(consumed, unit):g} {unit} has already been consumed from its "
            f"component lot by other batches.")
    if new_remaining < 0:
        new_remaining = 0.0  # swallow a sub-epsilon float residual rather than storing it negative

    # Keep cost_per_unit consistent with the resized output: the mix's own input cost spread over
    # what it now claims to have produced (same basis as _create_housemade_lot). Inputs must
    # already have been reconciled before this runs — update_batch guarantees that ordering.
    db.execute(cursor, """
        SELECT COALESCE(SUM(quantity_used * COALESCE(cost_per_unit, 0)), 0)
        FROM batch_materials WHERE batch_id = %s
    """, (batch_id,))
    input_cost = float(cursor.fetchone()[0] or 0)
    cost_per_unit = (input_cost / new_produced) if new_produced else 0

    db.execute(cursor, """
        UPDATE raw_material_lots SET quantity = %s, cost_per_unit = %s, status = 'active'
        WHERE lot_id = %s
    """, (new_remaining, cost_per_unit, lot_id))
    # re-exhaust if the resize left nothing; this also un-exhausts a revived lot via the
    # status='active' above, which is why that is set unconditionally first.
    exhaust_lot_if_depleted(db, cursor, lot_id)

    logging.info(
        f"Reconciled mix output lot {lot_id} for batch {batch_id}: "
        f"produced {new_produced} (base), consumed {consumed}, remaining {float(remaining)} -> {new_remaining}")
    return (lot_id, float(remaining), new_remaining, unit)


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

# REMOVED: edit-path safety fix — mark_as_shipped(batch_id) set status='Shipped' with no
#          shipment_batches rows. Its only caller (/batches/<id>/mark-shipped) is gone;
#          shipment status comes from services.shipments._recompute_batch_status.

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

def update_batch(batch_id, product_name=None, quantity=None, date_completed=None, notes=None,
                 expiration_date=_UNSET, planned_completion_date=_UNSET, batch_number=None,
                 material_adjustments=None, lot_selections=None, reconcile_materials=True,
                 materials_basis_quantity=None):
    """
    changes the details of batch, BESIDES STATUS, doesnt change status.

    note: similar to update_materials func

    CHANGED: quantity-edit-desync fix — this docstring used to PROMISE that a quantity change
    deducts from / returns to raw materials, while the body did nothing of the kind: it was a
    bare `UPDATE batches`. Editing a batch's quantity therefore left both its input deductions
    and (for a mix batch) its house-made output lot reflecting the ORIGINAL quantity. That
    silently corrupted production stock across 8 batches. It is now actually implemented:

      * material_adjustments {material_id: new_quantity_used} — applied via
        _apply_material_adjustments, which deducts more from the source lot on an increase
        (checking availability) and returns stock on a decrease.
      * a mix batch's output lot is resized by _reconcile_mix_output_lot whenever `quantity`
        changes, so the component stock it represents tracks the edit.

    All of it runs in ONE transaction with the batches row, in that order — inputs first,
    because the output lot's cost_per_unit is derived from the reconciled input cost. A failure
    anywhere rolls the whole edit back, rather than leaving the half-applied state this fix
    exists to prevent.

    Raises ValueError when the edit cannot be honoured (insufficient stock to cover an increase,
    or a new quantity below what has already been consumed from the output lot) so the caller
    can surface the reason. Returns None on any other failure, as before.
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
        # the pre-edit quantity is the basis for proportional input scaling below, so it must be
        # read inside this transaction and BEFORE the UPDATE overwrites it.
        old_quantity = None
        if "quantity" in field:
            if materials_basis_quantity is not None:
                # REPAIR path: the stored quantity is NOT what the existing deductions were sized
                # for, because an earlier edit changed it without reconciling. Caller states the
                # quantity the deductions actually correspond to, so the ratio is computed against
                # reality instead of against the already-corrupted column.
                old_quantity = float(materials_basis_quantity)
            else:
                db.execute(cursor, "SELECT quantity FROM batches WHERE batch_id = %s", (batch_id,))
                _row = cursor.fetchone()
                old_quantity = float(_row[0]) if _row and _row[0] is not None else None

        set_clause = ', '.join(f"{key} = %s" for key in field)
        params = (*field.values(), batch_id)
        db.execute(cursor, f"UPDATE batches SET {set_clause} WHERE batch_id = %s", params)

        if cursor.rowcount == 0:
                raise ValueError(f"batch ID {batch_id} not found — nothing updated")

        # ADDED: quantity-edit-desync fix — reconcile what the batch consumed and produced,
        # inside this same transaction. Inputs FIRST: _reconcile_mix_output_lot derives the
        # output lot's cost_per_unit from batch_materials, so it must see adjusted inputs.
        if material_adjustments:
            # explicit per-material override (the user typed quantities on the edit form)
            _apply_material_adjustments(db, cursor, batch_id, material_adjustments, lot_selections)
        elif reconcile_materials and "quantity" in field and old_quantity:
            # the DEFAULT path, and the heart of this fix. Reconciliation used to happen only if
            # the user ticked "Also adjust raw material deductions" on the edit form — unchecked
            # by default. Every one of the 8 desynced production batches is someone editing a
            # quantity without ticking it, so proportional scaling is now what happens unless a
            # caller explicitly overrides or opts out.
            new_q = float(field["quantity"])
            old_q = float(old_quantity)
            if old_q > 0 and abs(new_q - old_q) > 1e-12:
                _scale_batch_material_rows(db, cursor, batch_id, new_q / old_q)

        if "quantity" in field:
            _reconcile_mix_output_lot(db, cursor, batch_id, field["quantity"])

            # ADDED: edit-path safety fix — a quantity edit used to ignore shipments entirely.
            # Lowering a batch below what has already shipped made its remaining negative, and the
            # status was never re-derived (production batch 206: 8 of 8 shipped, still 'Partially
            # Shipped'). Refuse the impossible edit, then re-derive status from the allocations.
            db.execute(cursor, """
                SELECT COALESCE(SUM(quantity), 0) FROM shipment_batches WHERE batch_id = %s
            """, (batch_id,))
            shipped = float(cursor.fetchone()[0] or 0)
            if float(field["quantity"]) + _QTY_EPSILON_G < shipped:
                raise BatchReconcileError(
                    f"Cannot reduce this batch to {float(field['quantity']):g}: "
                    f"{shipped:g} has already been shipped.")
            _recompute_batch_status(cursor, db, batch_id)

        db.commit()
        logging.info(f"Updated batch with id:{batch_id}")

        return True

    except BatchReconcileError:
        # the edit is not possible (insufficient stock / below already-consumed). Roll back and
        # let the caller report the specific reason instead of flattening it to a generic None.
        # Other ValueErrors (e.g. "batch ID not found") keep falling through to the None return.
        db.rollback()
        raise

    except Exception as e:
        logging.error(f"Error changing batch details: {e}")
        db.rollback()
        return None

    finally:
        db.close()


class BatchStatusError(ValueError):
    """A manual status change that would skip or repeat a stock movement.

    ADDED: edit-path safety fix. Subclasses ValueError like BatchReconcileError; carries a
    user-facing reason for the route to flash.
    """


def _status_change_error(db, cursor, batch_id, new_status):
    """Return why a MANUAL status change is not allowed, or None if it is.

    ADDED: edit-path safety fix. The status buttons used to only relabel the batch, which let a
    user skip or repeat stock movements:
      * Planned -> Shipped never ran promotion, so a deferred batch never deducted its materials.
      * Ready mix -> Planned -> Ready re-ran promotion and created a SECOND output lot.
      * A hand-set 'Shipped' (or 'Ready' on a shipped batch) contradicted shipment_batches, which
        is the real source of a batch's shipment status (_recompute_batch_status).
    Planned -> Ready stays allowed: the route sends it through promote_batch_now.
    """
    db.execute(cursor, """
        SELECT status, mix_lot_id,
               (SELECT COUNT(*) FROM batch_materials bm WHERE bm.batch_id = b.batch_id),
               (SELECT COUNT(*) FROM shipment_batches sb WHERE sb.batch_id = b.batch_id)
        FROM batches b WHERE b.batch_id = %s
    """, (batch_id,))
    row = cursor.fetchone()
    if row is None:
        return "Batch not found."
    current, mix_lot_id, material_rows, shipment_rows = row

    if new_status == current:
        return None
    if new_status not in ('Ready', 'Planned'):
        return ("Shipped status is set automatically when a batch is added to a shipment. "
                "Create a shipment for this batch instead.")
    if shipment_rows:
        return ("This batch is on a shipment, so its status follows its shipments. "
                "Edit or delete the shipment instead.")
    if new_status == 'Planned' and (material_rows or mix_lot_id):
        return ("This batch has already used its materials, so it can't go back to Planned. "
                "Delete and re-create it if it was entered by mistake.")
    return None


def get_status_change_error(batch_id, new_status):
    """Public, read-only wrapper around _status_change_error for the route to check up front."""
    db = get_db_connection()
    cursor = db.cursor()
    try:
        return _status_change_error(db, cursor, batch_id, new_status)
    finally:
        db.close()


def update_batch_status(batch_id, new_status):
    """
    changes the status of batch, only changes status, not other details.

    CHANGED: edit-path safety fix — raises BatchStatusError for a change that would skip or
    repeat a stock movement (see _status_change_error).
    """

    db = get_db_connection()
    cursor = db.cursor()

    try:
        reason = _status_change_error(db, cursor, batch_id, new_status)
        if reason:
            raise BatchStatusError(reason)

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

    except BatchStatusError:
        db.rollback()
        raise

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


def _set_material_usage(db, cursor, batch_id, material_id, new_total, preferred_lot_id=None):
    """Make a batch's total usage of one material equal `new_total` (base units), moving stock to
    or from lots to match. Uses the CALLER's transaction; reads everything before writing, and
    raises BatchReconcileError (nothing written) when the increase can't be covered.

    ADDED: edit-path safety fix — the single place both edit paths (proportional quantity edit and
    the manual per-material override) change a batch's deductions. Previously an increase could
    only come from the lot each row originally used, so once that lot was drained by another batch
    the edit was refused even with plenty of the same material in other lots.

      decrease: every row of the material shrinks in proportion and returns its share to its own
                lot (the original split across lots is preserved).
      increase: taken from, in order -
                1. preferred_lot_id, if the user picked one on the edit form;
                2. each existing row's proportional share from its own lot (keeps the split when
                   stock allows);
                3. whatever is left in lots the batch already uses;
                4. the material's other lots, oldest first.
                Only active, unexpired lots with stock are used - the same rule as FIFO promotion.
    """
    db.execute(cursor, """
        SELECT batch_material_id, lot_id, quantity_used FROM batch_materials
        WHERE batch_id = %s AND material_id = %s ORDER BY batch_material_id
    """, (batch_id, material_id))
    rows = [(bm_id, lot_id, float(used or 0)) for bm_id, lot_id, used in cursor.fetchall()]
    if not rows:
        raise BatchReconcileError(f"No batch_material record for batch {batch_id}, material {material_id}")

    current = sum(used for _, _, used in rows)
    new_total = float(new_total)
    delta = new_total - current
    if abs(delta) <= _QTY_EPSILON_G:
        return

    if delta < 0:
        ratio = (new_total / current) if current else 0
        for bm_id, lot_id, used in rows:
            give_back = used - used * ratio
            if lot_id and give_back > 0:
                db.execute(cursor, """
                    UPDATE raw_material_lots SET quantity = quantity + %s WHERE lot_id = %s
                """, (give_back, lot_id))
                # returned stock can revive a lot the original deduction had exhausted
                db.execute(cursor, """
                    UPDATE raw_material_lots SET status = 'active'
                    WHERE lot_id = %s AND quantity > %s AND status <> 'active'
                """, (lot_id, _QTY_EPSILON_G))
            db.execute(cursor, """
                UPDATE batch_materials SET quantity_used = %s WHERE batch_material_id = %s
            """, (used * ratio, bm_id))
        return

    # --- increase: plan every take first (reads only), then write ---
    today = business_today().strftime('%Y-%m-%d')
    db.execute(cursor, """
        SELECT lot_id, quantity, cost_per_unit FROM raw_material_lots
        WHERE material_id = %s AND status = 'active' AND quantity > 0
          AND (expiration_date IS NULL OR expiration_date > %s)
        ORDER BY received_date ASC, lot_id ASC
    """, (material_id, today))
    usable = [(lot_id, float(qty), cost) for lot_id, qty, cost in cursor.fetchall()]
    available = {lot_id: qty for lot_id, qty, _ in usable}
    costs = {lot_id: cost for lot_id, _, cost in usable}

    takes = {}  # lot_id -> amount to take
    remaining = delta

    def take_from(lot_id, wanted):
        nonlocal remaining
        amount = min(wanted, available.get(lot_id, 0.0), remaining)
        if amount > 0:
            takes[lot_id] = takes.get(lot_id, 0.0) + amount
            available[lot_id] -= amount
            remaining -= amount

    if preferred_lot_id is not None:
        take_from(preferred_lot_id, remaining)
    if current > 0:
        for _, lot_id, used in rows:                  # proportional share from each own lot
            take_from(lot_id, delta * used / current)
    for _, lot_id, _ in rows:                         # leftovers in lots already on the batch
        take_from(lot_id, remaining)
    for lot_id, _, _ in usable:                       # then the oldest other lots
        take_from(lot_id, remaining)

    if remaining > _QTY_EPSILON_G:
        db.execute(cursor, "SELECT name FROM raw_materials WHERE material_id = %s", (material_id,))
        name_row = cursor.fetchone()
        label = name_row[0] if name_row else f"material {material_id}"
        have = delta - remaining
        raise BatchReconcileError(
            f"Not enough {label} in stock: this change needs {delta:g} more, but only {have:g} "
            f"is available across usable lots.")

    row_for_lot = {lot_id: (bm_id, used) for bm_id, lot_id, used in rows if lot_id}
    for lot_id, amount in takes.items():
        db.execute(cursor, """
            UPDATE raw_material_lots SET quantity = quantity - %s WHERE lot_id = %s
        """, (amount, lot_id))
        exhaust_lot_if_depleted(db, cursor, lot_id)
        if lot_id in row_for_lot:
            bm_id, used = row_for_lot[lot_id]
            db.execute(cursor, """
                UPDATE batch_materials SET quantity_used = %s WHERE batch_material_id = %s
            """, (used + amount, bm_id))
        else:
            db.execute(cursor, """
                INSERT INTO batch_materials (batch_id, material_id, lot_id, quantity_used, cost_per_unit)
                VALUES (%s, %s, %s, %s, %s)
            """, (batch_id, material_id, lot_id, amount, costs.get(lot_id)))


def _apply_material_adjustments(db, cursor, batch_id, new_quantities: dict, lot_selections: dict = None):
    """Apply per-material input totals for a batch, using the CALLER's transaction.

    ADDED: quantity-edit-desync fix — extracted from adjust_batch_material so the batch row, its
    input deductions and its mix output lot are written in ONE transaction (see update_batch).
    CHANGED: edit-path safety fix — delegates to _set_material_usage. This used to move stock for
    the first lot row only and then write the new total onto EVERY row of the material.

    new_quantities: {material_id: new_total_used (base units)}
    lot_selections: {material_id: lot_id} — lot to draw an increase from first (optional)
    Raises BatchReconcileError on insufficient stock or a missing batch_material row. Does NOT
    commit or close; adjust_batch_material is the committing wrapper for standalone callers.
    """
    for material_id, new_qty in new_quantities.items():
        _set_material_usage(db, cursor, batch_id, material_id, new_qty,
                            preferred_lot_id=(lot_selections or {}).get(material_id))

    logging.info(f"Adjusted materials for batch {batch_id} with changes: {new_quantities}")
    return True


def _scale_batch_material_rows(db, cursor, batch_id, ratio):
    """Scale a batch's usage of every material by `ratio`. Uses the CALLER's transaction.

    ADDED: quantity-edit-desync fix — the automatic path for a quantity edit, where the inputs
    track the new quantity in proportion.
    CHANGED: edit-path safety fix — per material via _set_material_usage, so an increase can fall
    back to other lots when the original one is used up (it used to refuse). A decrease still
    returns each row's share to its own lot, preserving the original split.
    """
    db.execute(cursor, """
        SELECT material_id, SUM(quantity_used) FROM batch_materials
        WHERE batch_id = %s GROUP BY material_id
    """, (batch_id,))
    totals = cursor.fetchall()
    for material_id, used in totals:
        _set_material_usage(db, cursor, batch_id, material_id, float(used or 0) * ratio)

    logging.info(f"Scaled {len(totals)} materials for batch {batch_id} by ratio {ratio}")
    return len(totals)


def adjust_batch_material(batch_id, new_quantities: dict, lot_selections: dict = None):
    """
    Updates batch_materials.quantity_used and applies the delta to the source lots.
    new_quantities: {material_id (int): new_quantity_used (float)}
    lot_selections: {material_id (int): lot_id (int)} — lot to deduct from when delta > 0
    Delta logic: the lot loses (new - old), so increasing qty deducts more, decreasing returns stock.

    Committing wrapper around _apply_material_adjustments for standalone callers. When the
    adjustment must be atomic with a batch-row change, call update_batch(..., material_adjustments=)
    instead so both land in one transaction.
    """
    db = get_db_connection()
    cursor = db.cursor()
    try:
        _apply_material_adjustments(db, cursor, batch_id, new_quantities, lot_selections)
        db.commit()
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
    lot_selections: {material_id: lot_id} — lot to draw an increase from first.

    CHANGED: edit-path safety fix — a dry run of the real write (_set_material_usage), always
    rolled back, so this pre-check can never disagree with what the edit will actually do.
    """
    db = get_db_connection()
    cursor = db.cursor()

    try:
        _apply_material_adjustments(db, cursor, batch_id, new_quantities, lot_selections)
        return True

    except Exception as e:
        logging.warning(f"Batch materials stock check failed for batch {batch_id}: {e}")
        return False

    finally:
        db.rollback()
        db.close()
