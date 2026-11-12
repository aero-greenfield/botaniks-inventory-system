from flask import Blueprint, jsonify, request
from datetime import datetime
import logging
import json

from auth import requires_auth
from database import get_db_connection
# REMOVED: get_all_materials — /api/materials and /api/material-unit no longer fetch the full table
# REMOVED: get_all_recipes — /api/recipes no longer fetches the full table
# ADDED: get_material_by_name, get_material_names, get_recipe_names — targeted lookups used below
from services.materials import get_material_names, get_unit_and_dimension
from services.lots import get_all_lots_for_material, get_lots_for_material
from services.recipes import get_recipe_names, get_recipe, get_recipe_unit, get_recipe_batch_type
# ADDED: units-conversion-layer feature — base-unit labels for the lot-selection UI.
from services import units

api_bp = Blueprint('api', __name__, url_prefix='/api')


# checks db connection and returns service status
@api_bp.route('/health')
def health_check():
    try:
        db = get_db_connection()
        cursor = db.cursor()
        db.execute(cursor, "SELECT 1")
        db.close()
        db_status = 'connected'
    except Exception as e:
        logging.error(f"health_check: {e}", exc_info=True)
        db_status = 'error'
    return jsonify({'status': 'healthy', 'db': db_status, 'service': 'matcha-inventory', 'timestamp': datetime.now().isoformat()})


# autocomplete dropdown for material search (requires JS on frontend)
# CHANGED: was get_all_materials() full-table fetch + client-side filter.
#          Now accepts ?q= and filters in SQL; returns at most 50 results.
@api_bp.route('/materials')
@requires_auth
def api_materials():
    q = request.args.get('q', '').strip()
    return jsonify(get_material_names(q))


# autocomplete dropdown for recipe search when making a batch (requires JS on frontend)
# CHANGED: was get_all_recipes() full-table fetch + client-side filter.
#          Now accepts ?q= and filters in SQL; returns at most 50 results.
@api_bp.route('/recipes')
@requires_auth
def api_recipes():
    q = request.args.get('q', '').strip()
    return jsonify(get_recipe_names(q))


# returns all lots for a given material (used to populate lot selection dropdowns)
@api_bp.route('/lots/<material_id>')
@requires_auth
def api_lots(material_id):
    # ADDED: coerce to int — a non-integer segment (e.g. "abc") would cause an unhandled
    #        exception inside the service layer instead of a clean 400 response.
    try:
        material_id = int(material_id)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid material_id'}), 400
    df = get_all_lots_for_material(material_id=material_id)
    if df is None or df.empty:
        return jsonify([])
    return jsonify(json.loads(df.to_json(orient='records')))


# looks up a material's unit by name (used to auto-fill unit field on forms)
# CHANGED: was get_all_materials() (full JOIN+GROUP BY+SUM table) + pandas string filter.
#          Now calls get_material_by_name() which runs a single indexed SELECT with no JOIN.
@api_bp.route('/material-unit')
@requires_auth
def api_material_unit():
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'exists': False})
    # CHANGED: also return the material's dimension ('mass' or 'count') so the recipe
    #          material-row UI can show a unit dropdown for mass materials (conversion) and a
    #          fixed unit label for count materials (no conversion).
    row = get_unit_and_dimension(name)
    if row is None:
        return jsonify({'exists': False})
    return jsonify({'exists': True, 'unit': row[0], 'dimension': row[1]})


# looks up a recipe's product unit-of-measurement by name (used to show the product's unit
# next to the quantity field on the create/edit batch pages). Mirrors /api/material-unit.
@api_bp.route('/recipe-unit')
@requires_auth
def api_recipe_unit():
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'exists': False})
    unit = get_recipe_unit(name)
    # ADDED: batch-type-on-recipe feature — also return the recipe's batch type so the
    #        create-batch page can show it read-only and adapt its fields (Planned date +
    #        housemade warning only apply to 'finished' batches).
    batch_type = get_recipe_batch_type(name)
    return jsonify({'exists': unit is not None, 'unit': unit or '', 'batch_type': batch_type or ''})



#will return all AVAILABLE lots for batch creation drop down.
@api_bp.route('/available-lots/<material_id>')
@requires_auth
def api_available_lots(material_id):
    # ADDED: same int coercion as api_lots — prevents unhandled exceptions on non-integer input
    try:
        material_id = int(material_id)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid material_id'}), 400
    df = get_lots_for_material(material_id=material_id)
    if df is None or df.empty:
        return jsonify([])
    lots = json.loads(df.to_json(orient='records'))
    # ADDED: lot-picker-location — quantity is in the base unit (grams); also send it in the
    #        material's own unit, which is what the Edit Batch page shows next to each ingredient.
    for lot in lots:
        lot['quantity_display'] = float(units.from_base(lot['quantity'], lot['unit'] or 'units'))
    return jsonify(lots)
# json.loads(df.to_json(orient='records')) converts the DataFrame to a list of dictionaries, 
# which can be easily consumed by the frontend JavaScript code.

#will return recipe given a name, for frontend of batch creation. needs to know which lots to ask user about. 
@api_bp.route('/recipe-materials/<path:product_name>')
@requires_auth
def api_recipe_materials(product_name):
    df = get_recipe(product_name)
    if df is None or df.empty:
        return jsonify([])
    rows = df.to_dict(orient='records')
    # CHANGED: lot-selection-unit fix — quantity_needed and lot quantities are stored in the base
    #          unit (grams for mass), but the warehouse thinks in the unit the recipe line was
    #          entered in (e.g. lb). Tell the create-batch UI which unit to DISPLAY in and the
    #          factor to convert stored grams <-> that display unit, so the lot picker shows lbs.
    #          The submission still converts back to grams client-side, so the backend protocol
    #          is unchanged.
    for r in rows:
        if r.get('dimension') == 'mass':
            display_unit = r.get('line_unit') or units.MASS_BASE
            r['display_unit'] = display_unit
            r['base_per_display'] = float(units.to_base(1, display_unit))
        else:
            # count units never convert (a 'tin' is a 'tin'); display in the material's own unit.
            r['display_unit'] = r.get('unit') or ''
            r['base_per_display'] = 1.0
    return jsonify(rows)