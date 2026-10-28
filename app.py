
try:
    from dotenv import load_dotenv # load enviroment varibales, python has build in libracy that loads them in. 
    load_dotenv()
    
except ImportError:
   
    pass

# =======================
# IMPORTS
# =======================




from flask import Flask, json, request, redirect, url_for, jsonify, send_file, render_template, flash  # ADDED: flash for session-backed messages

# - Flask: The main application class
# - request: obejct that give you access to income http request data. 
# - redirect:  instructs the browser to go to a different URL
# - url_for:  builds urls for different functions (useful for redirects)
# - jsonify:  converests python data structures (lists, dicts) to JSON format 
# - send_file:  sends file to browser for download

import os  # Operating system functions (file paths, environment variables)
import math  # ADDED: math.ceil for computing total_pages in paginated routes
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation  # ADDED: Decimal-based display formatting (kills float spew, keeps precision)
from datetime import datetime  # For timestamps in exports
from config import business_today  # planned-batch date checks — never bare datetime.now(), see config.py

from flask_wtf.csrf import CSRFProtect # security necesity. 

#Service layer imports. :




from services.setup import create_database
from services.audit import log_action, view_logs
# ADDED: units-conversion-layer feature — conversion helpers (entry units -> stored base unit).
from services import units
from services.materials import (
    add_raw_material, get_all_materials,
    get_material_by_id,
    update_raw_material,
    delete_raw_material, get_material_id,
    get_low_stock_materials,  # ADDED: was missing — /low-stock route raised NameError without this
    get_unit_and_dimension,  # ADDED: units-conversion-layer feature — per-material unit/dimension lookup
)
from services.lots import (
    get_all_lots,
    # ADDED: excel-exports feature — full unfiltered lot list (incl. expired/exhausted) for the
    #        inventory workbook's Lots sheet.
    get_all_lots_joined,
    get_lot_by_id,
    receive_lot as inventory_receive_lot,
    update_lot,
    delete_lot,
    get_batches_for_lot,
    # ADDED: table-redesign feature — bulk active-lots fetch for the inventory drawers.
    get_active_lots_for_materials,
)
from services.recipes import (
    add_recipe, get_all_recipes, get_all_recipes_with_id,
    get_recipe_by_id, update_recipe, delete_recipe_by_id,
    check_negative_stock, get_recipe_batch_type,
)
from services.batches import (
    add_to_batches, get_batches, get_batches_shipped, get_batches_planned,
    get_all_batches_with_id, get_batch_by_id, mark_as_shipped, delete_batch,
    update_batch, update_batch_status, promote_batch_now, get_batch_materials,
    adjust_batch_material, check_batch_materials_stock,
    get_batch_materials_for_reallocation,
    # ADDED: planned-deduction-mode feature — a deferred Planned batch has no batch_materials rows;
    #        its lot picks live in the planned_lot_selections JSON column and are read/written here.
    get_planned_lot_selections, update_planned_lot_selections, clear_planned_lot_selections,
)
from services.shipments import (
    create_shipment, get_all_shipments, get_shipment_by_id,
    update_shipment, delete_shipment,
    # ADDED: excel-exports feature — summary (one row/shipment) + detail (one row/split) queries
    #        for the shipments workbook.
    get_shipments_summary, get_shipment_details,
    # ADDED: materials-on-shipments — lot-level traceability sheet for the per-shipment manifest.
    get_shipment_materials,
)


# Import helper functions for exporting data
# - export_to_csv: Exports DataFrames to CSV files (not currently used)
# - export_to_excel: NEW - Exports DataFrames to Excel files (.xlsx format)
# - export_multi_sheet_to_excel: excel-exports feature - one workbook, many named sheets

from helper_functions import ( export_to_excel, export_multi_sheet_to_excel,)

from auth import requires_auth
from routes.api import api_bp

# logging for actions.
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)

"""
instead of print statements for actions (ex. adding batche, etc). it logs them with time stamp. will show up in render logs. 

use logging.info("...") instead of print
""" 

# =======================
# CREATE FLASK APP
# =======================

app = Flask(__name__)  # creates flask instance, so this web app name is 'app' which can be used to run the server and define routes.

# CSRFprotect 
secret = os.environ.get('SECRET_KEY')
if not secret:
    raise RuntimeError("SECRET_KEY environment variable must be set")
app.config['SECRET_KEY'] = secret

# ADDED: harden session cookie — HTTPONLY blocks JS access, Lax prevents cross-site leakage.
# SESSION_COOKIE_SECURE is omitted here; enable it at the deployment level when HTTPS is enforced
# (setting it to True over plain HTTP would break sessions entirely).
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

csrf = CSRFProtect(app)
app.register_blueprint(api_bp)

# Negative stock is blocked for users right now (a cluster of negative-quantity bugs).
# Flip to True to restore the "Proceed Anyway" override (confirm_negative_stock.html +
# add_to_batches(allow_negative=True)). All that code is intact, just gated off.
ALLOW_NEGATIVE_STOCK_OVERRIDE = False


# =======================
# JINJA TEMPLATE FILTERS
# =======================
# Reusable display-formatting filters. DEFINED here only — applying them across
# templates is deferred per-page work. Each is defensive and never raises on bad input.

EMPTY_DISPLAY = "—"  # em dash shown for any empty/missing value


def _is_empty(value):
    """True for None, NaN, or blank/whitespace-only strings."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


@app.template_filter("fmt_num")
def fmt_num(value, places=6):
    """Format a number for display: exact, no float spew, trailing zeros stripped.
    Usage: {{ qty | fmt_num }}  ->  48.980000000000004 becomes "48.98"; 5.0 becomes "5".

    Precision contract: work in Decimal end-to-end. Values reach here either as an
    exact Decimal (from units.from_base / a NUMERIC column) or as a float (SQLite REAL
    / PG DOUBLE PRECISION columns); a stray float is routed through str() first, which
    yields Python's clean shortest repr (str(3.4568999999999999) == "3.4569"), so the
    IEEE-754 noise never appears. We then quantize to `places` decimals — this caps
    genuinely-long results (e.g. grams->lb division) without the old force-formatting
    that EXPANDED float noise. `places=6` is lossless for lb display; raise it if a
    future column needs finer precision, but never drop back to a float round-trip."""
    if _is_empty(value):
        return EMPTY_DISPLAY
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        # normalize() strips trailing zeros; format(..., 'f') forces plain notation
        # (normalize() alone can emit scientific notation like 1E+3 for round numbers).
        return format(d.normalize(), 'f')
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


# ADDED: table-redesign feature — `qty` rounds a quantity to 2 dp for display.
# Distinct from `fmt_num` (which keeps 15 places for the boss's extreme-precision entries):
# qty is the scannable-table display filter — 2 dp is plenty for stock/quantity columns,
# and it strips both float spew (48.980000000000004) and trailing-zero noise (5.0 -> 5).
@app.template_filter("qty")
def qty(value):
    """Round a quantity to 2 dp for display, trimming float noise and trailing zeros.
    Usage: {{ row.stock_level | qty }}  ->  48.980000000000004 becomes "48.98"; 5.0 becomes "5".
    Returns the em dash for None/nan/empty.

    Decimal-based like fmt_num (no float round-trip): a stray float is cleaned via
    str() before quantizing, so no IEEE-754 spew leaks into the 2-dp cells."""
    if _is_empty(value):
        return EMPTY_DISPLAY
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(d.normalize(), 'f')
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


@app.template_filter("dash")
def dash(value):
    """Render empty/missing values as an em dash, never None/nan/blank.
    Usage: {{ value | dash }}  ->  None becomes "—"."""
    if _is_empty(value):
        return EMPTY_DISPLAY
    return value


# ADDED: units-conversion-layer feature — convert a stored base-unit quantity into the
# material's display unit and format it for a table cell. Quantities are stored in grams
# (mass) or the material's own count unit; this shows them back in `unit` (e.g. lb), rounded
# via the existing `qty` rules. Empty/missing -> em dash.
#   Usage: {{ row.stock_level | disp(row.unit) }}  -> 907.18474 (g), 'lb'  ->  "2"
@app.template_filter("disp")
def disp(value, unit=None):
    if _is_empty(value):
        return EMPTY_DISPLAY
    try:
        converted = units.from_base(value, unit) if unit else value
    except (TypeError, ValueError):
        return str(value)
    return qty(converted)  # reuse the 2-dp, noise-stripping table formatter


# ADDED: units-conversion-layer feature — same base->display conversion as `disp`, but keeps
# full precision (via fmt_num) for "exact" drawer values and per-lot quantities.
@app.template_filter("disp_full")
def disp_full(value, unit=None):
    if _is_empty(value):
        return EMPTY_DISPLAY
    try:
        converted = units.from_base(value, unit) if unit else value
    except (TypeError, ValueError):
        return str(value)
    return fmt_num(converted)


@app.template_filter("humanize")
def humanize(value):
    """Turn a machine label into readable text.
    Usage: {{ action | humanize }}  ->  "material_updated" becomes "Material updated"."""
    if _is_empty(value):
        return EMPTY_DISPLAY
    text = str(value).replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else EMPTY_DISPLAY


@app.template_filter("fmt_date")
def fmt_date(value, fmt="%d %b %Y"):
    """One consistent date format; tolerant of datetime, ISO string, or None.
    Usage: {{ row.received_date | fmt_date }}  ->  "30 Jun 2026"."""
    if _is_empty(value):
        return EMPTY_DISPLAY
    if isinstance(value, datetime):
        return value.strftime(fmt)
    try:
        # accept ISO strings, with or without a time component
        parsed = datetime.fromisoformat(str(value).replace("Z", "").strip())
        return parsed.strftime(fmt)
    except (TypeError, ValueError):
        return str(value)


# Sub-milligram residual left by float deductions (SET quantity = quantity - x) counts as zero
# stock — mirrors the _QTY_EPSILON_G storage tolerance in services/batches.py. Without it a
# material with e.g. 0.0001 g left fails `== 0`, falls into the reorder check, and shows
# "Low Stock" while its displayed stock rounds to "0".
_STOCK_EPSILON = 1e-3


def material_status(stock_level, reorder_level):
    """Derive an inventory status key ('out' | 'low' | 'in') from current stock vs reorder level.

    Single source of truth for the inventory badge, the low-stock page, and the Excel export —
    all of which previously hand-rolled `stock == 0` checks that float noise slipped past.
    Stock and reorder are both in base units (grams for mass), so they compare directly."""
    stock = stock_level or 0
    try:
        stock = float(stock)
        reorder = float(reorder_level or 0)
    except (TypeError, ValueError):
        return "out"
    if stock <= _STOCK_EPSILON:
        return "out"
    if stock <= reorder:
        return "low"
    return "in"


# Expose material_status to templates so the inventory/low-stock badges share the export's logic.
app.add_template_filter(material_status, "material_status")


@app.template_filter("batch_no")
def batch_no(number, batch_type=None):
    """Display a batch number, prefixing component (mix) batches with MIX-BATCH- so the batch#
    matches its house-made lot number (lots already use this prefix; see services/batches.py).
    Display-only — the stored batch_number is unchanged. No-op for standard/finished batches."""
    if _is_empty(number):
        return EMPTY_DISPLAY
    if batch_type == "mix":
        return f"MIX-BATCH-{number}"
    return number


# WHAT IS 'app':
# Think of 'app' as your web server. When you do @app.route('/inventory'),
# you're telling this server "when someone visits /inventory, run this function"






# =======================
# DATABASE SETUP & CHECKS
# ========================




# for local development. 
if not os.path.exists('data'): # Make sure we have a 'data' folder for local SQLite database 
    os.makedirs('data') 


DATABASE_URL = os.environ.get('DATABASE_URL')
try:
    from database import get_connection
    conn = get_connection()
    conn.close()
    logging.info("[SUCCESS] Database connection successful")
    if not DATABASE_URL:
        create_database()  # SQLite: always ensure tables exist
except:
    logging.info("[INIT] Initializing database...")
    create_database()



# =======================
# AUTHENTICATION SYSTEM
# ========================




# ========================
# RATE LIMITING
# ========================
# ADDED: prevents brute-force attacks against the HTTP Basic Auth endpoint.
# Limits are applied per source IP. flask-limiter added to requirements.txt.
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "2000 per hour"]
)
# ========================

# ========================
# HEALTH CHECK
# ========================
# ADDED: lightweight liveness+DB probe for uptime monitoring (Render health check / UptimeRobot).
# Unauthenticated and rate-limit-exempt so the monitor can hit it freely. GET-only, so CSRF
# does not apply. Returns 200 only if a trivial query round-trips to the database, otherwise 503 —
# this catches connection-pool exhaustion and Supabase outages, the failure modes that would
# otherwise be invisible until a coworker calls.
from database import get_db_connection

@app.route('/health')
@limiter.exempt
def health_check():
    db = None
    try:
        db = get_db_connection()
        cursor = db.cursor()
        db.execute(cursor, "SELECT 1")
        cursor.fetchone()
        return {"status": "ok"}, 200
    except Exception as e:
        logging.error(f"health check failed: {e}", exc_info=True)
        return {"status": "error", "detail": "database unavailable"}, 503
    finally:
        if db is not None:
            db.close()


# ========================
# ERROR HANDLERS
# ========================
# ADDED: friendly catch-all pages so an unhandled crash or a mistyped URL shows a usable page
# instead of a bare stack trace / blank 500. The real error is logged server-side (with traceback
# for 500s) so it shows up in Render logs; users only ever see a generic, non-technical message.

@app.errorhandler(404)
def handle_404(e):
    return render_template('error.html',
        title="Page Not Found",
        message="That page doesn't exist. Use the menu on the left to get where you need to go.",
        back_link=True, back_link_url="/", back_link_label="Back to Home"
    ), 404


@app.errorhandler(500)
def handle_500(e):
    logging.error(f"Unhandled 500 error: {e}", exc_info=True)
    return render_template('error.html',
        title="Something Went Wrong",
        message="An unexpected error occurred. Please try again. If it keeps happening, take a "
                "screenshot and contact whoever manages this system.",
        back_link=True, back_link_url="/", back_link_label="Back to Home"
    ), 500


# ADDED: upper bound for ?page= query param.
# Without this, ?page=99999999 generates a massive OFFSET query that stalls the database.
MAX_PAGE = 10_000











# ========================
# WEB ROUTES (PAGES)
# ========================



# =======================
# HOME PAGE ROUTE
# =======================
# This is the main landing page of your app

@app.route('/')  # using app name: 'app', it uses '/' to route as main page. (landing page)
@requires_auth   # User must login to see this page
def index():
    df_materials, _ = get_all_materials(page=None)
    df_low = get_low_stock_materials()
    df_batches, _ = get_batches(page=None)
    batch_count = len(df_batches)
    df_recipes, _ = get_all_recipes(page=None)
    # page=None returns None for the count, and df_recipes has one row per ingredient,
    # so count distinct recipes by product name.
    recipe_count = df_recipes['recipe_product_name'].nunique() if not df_recipes.empty else 0
    df_logs = view_logs()

    return render_template("index.html",
        total_materials=len(df_materials),
        low_stock_count=len(df_low),
        low_stock_items=df_low.to_dict(orient='records'),
        batch_count=batch_count,
        recipe_count=recipe_count,
        recent_logs=df_logs.head(5).to_dict(orient='records'),
    )



#=================
#LOGGING PAGE
#===============

@app.route('/audit-log')
@requires_auth
def view_audit_log():
    df = view_logs()
    logs = df.to_dict(orient='records') if not df.empty else []
    return render_template("audit_log.html",
        logs=logs,
        count=len(logs),
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
    )






# ========================
# INVENTORY PAGES
# ========================

"""
This section is all routes related to raw materials. 

1. view inventory page (/inventory) [GET route only, no POST since this page is just for viewing]

    - also export to excell button. (/export/inventory-excel) [GET route that triggers excel export and download]

2. add material page (/add-material) [GET and POST routes]

3. low stock alerts page (/low-stock) [just GET route for now, no POST route since this page is just for viewing alerts]

4. manage materials page (/manage-materials) - view all materials with edit/delete options [GET and POST routes]


"""

@app.route('/inventory')  # URL path 
# when only using GET, Flask assumes it's a GET route, so we don't need to specify methods=['GET'] here.
@requires_auth
def view_inventory():
    """
    View All Inventory Page

    STEP-BY-STEP FLOW:
    1. User visits /inventory
    2. Flask calls this function
    3. Function queries database for all materials
    4. Converts data to HTML table
    5. Wraps table in styled HTML page
    6. Returns HTML to browser
    """

    
    # ADDED: read page number from URL (?page=N); default to page 1
    PER_PAGE = 50
    page = min(max(1, int(request.args.get('page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # ADDED: sort feature — column + direction from URL (validated/whitelisted in the service)
    sort = request.args.get('sort')
    direction = request.args.get('dir', 'asc')

    # CHANGED: default the materials page to stock high->low when the user hasn't picked a sort.
    #          `sort`/`direction` stay None/asc so the Sort dropdown still shows "Default"; only
    #          the order passed to the service changes. Other callers of get_all_materials keep
    #          the service's own default.
    effective_sort = sort or 'stock_level'
    effective_dir = direction if sort else 'desc'

    # CHANGED: was get_all_materials() returning a plain df.
    #          Now returns (df, total) tuple; page= triggers LIMIT/OFFSET in the query.
    df, total = get_all_materials(page=page, per_page=PER_PAGE, sort_by=effective_sort, sort_dir=effective_dir)

    materials = df.to_dict(orient='records') if not df.empty else []
    # ADDED: compute total page count for pagination controls in the template
    total_pages = math.ceil(total / PER_PAGE) if total else 1

    # row-numbering fix — the page splits into two tables (Raw / Housemade) AFTER pagination, so a
    # single page offset can't keep both numbered continuously across pages. Compute a per-group
    # offset = how many materials of that group appear before this page in the SAME ordering.
    offset_raw = 0
    offset_housemade = 0
    if page > 1:
        full_df, _ = get_all_materials(page=None, sort_by=effective_sort, sort_dir=effective_dir)
        if not full_df.empty:
            preceding = full_df.iloc[:(page - 1) * PER_PAGE]
            offset_housemade = int(preceding['is_housemade'].fillna(0).astype(bool).sum())
            offset_raw = len(preceding) - offset_housemade

    # ADDED: table-redesign feature — attach each material's active lots so the template
    #        can server-render them inside the per-row expand drawer (replaces the old
    #        per-row /api/lots AJAX). One bulk query for the whole page, then grouped.
    lots_by_id = get_active_lots_for_materials([row['material_id'] for row in materials])
    for row in materials:
        row['lots'] = lots_by_id.get(row['material_id'], [])

    return render_template("inventory.html",
        materials=materials,
        count=len(df),
        page=page,
        # row-numbering feature — per-group offsets so each table's # column continues across pages.
        offset_raw=offset_raw,
        offset_housemade=offset_housemade,
        total_pages=total_pages,
        total=total,
        sort=sort,
        direction=direction,
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
)



@app.route('/export/inventory-excel') # URL path for exporting inventory to excel, this is a button in inventory.html. 
@requires_auth
def export_inventory_excel():
    """
    Export inventory data to a multi-sheet Excel workbook and trigger download.

    CHANGED (excel-exports feature): was a single flat sheet of materials. Now produces ONE
    workbook with two tabs:
      - "Materials": every material (raw + housemade) with a derived Status and Type column.
      - "Lots": every lot, including expired/exhausted ones, so the reader can filter in Excel.

    How it works:
    1. Get all materials (page=None → full table, no pagination) and derive Status/Type.
    2. Get all lots via get_all_lots_joined() (no UI hiding — completeness).
    3. Write both DataFrames to one workbook with export_multi_sheet_to_excel().
    4. send_file() triggers the browser download.
    """
    # --- Materials sheet ---
    # get_all_materials returns (df, total); page=None skips LIMIT/OFFSET so we get every
    # material — and it already includes housemade materials, so no extra query is needed.
    mats, _ = get_all_materials(page=None)

    if not mats.empty:
        # Status is DERIVED here (not stored): out of stock first, then at/below reorder = low,
        # otherwise in stock. This mirrors the badge logic the inventory page computes inline,
        # but we bake it into a real column so the spreadsheet reader can sort/filter on it.
        _STATUS_LABELS = {"out": "Out of Stock", "low": "Low Stock", "in": "In Stock"}
        def _material_status(row):
            # Reuse the shared material_status() helper so the export, the inventory badge, and
            # the low-stock page agree — including treating sub-epsilon float residual as zero.
            return _STATUS_LABELS[material_status(row['stock_level'], row['reorder_level'])]
        mats['Status'] = mats.apply(_material_status, axis=1)

        # Type comes from the is_housemade boolean. Housemade "materials" are intermediate
        # house-made products (mix batches) surfaced as pseudo-materials; labeling them lets the
        # reader tell raw purchases apart from in-house production at a glance.
        mats['Type'] = mats['is_housemade'].apply(lambda h: "Housemade" if h else "Raw")

        # Keep only the columns we want in the export, in reading order. (get_all_materials also
        # returns material_id, total_cost, is_edible, is_organic — not needed for this sheet.)
        mats = mats[['name', 'category', 'stock_level', 'unit', 'reorder_level', 'Status', 'Type']]

    # --- Lots sheet ---
    # Unfiltered, completeness-first list (active + expired + exhausted), with a status column.
    lots = get_all_lots_joined()

    # Guard: if there's genuinely nothing in either sheet, don't hand back an empty workbook.
    if mats.empty and lots.empty:
        return "No data to export", 400

    # One workbook, two tabs. Insertion order of the dict = tab order in the file.
    filepath = export_multi_sheet_to_excel({'Materials': mats, 'Lots': lots}, 'inventory')

    # Send file to browser as download
    # - as_attachment=True: Forces download instead of opening in browser
    # - download_name: Sets the filename shown in download dialog
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


@app.route('/receive-lot', methods=['GET', 'POST'])
@requires_auth
def receive_lot():
    """
    Route to receive a new lot of raw materials.

    GET: Show form to input lot details (material, quantity, etc.)
    POST: Process form submission, add lot to database, update inventory

    This route allows users to log the receipt of new raw material lots, which is essential for inventory management.
    
    adds row to raw_material_lots table, 

    inputs needed: (when calling receive_lot)
    material_id, lot_number, quantity, received_date, expiry_date=None, location=None, supplier=None, cost_per_unit=None):

    """

    if request.method == 'POST':
        # all string inputs
        name          = request.form.get('material_name', '').strip()
        lot_number    = request.form.get('lot_number', '').strip()
        quantity_str  = request.form.get('quantity', '').strip()
        received_date = request.form.get('received_date', '').strip()
        expiry_date       = request.form.get('expiry_date', '').strip() or None
        location          = request.form.get('location', '').strip() or None
        supplier          = request.form.get('supplier', '').strip() or None
        cost_per_unit_str = request.form.get('cost_per_unit', '').strip()


        # for the required fields, check if they are blank 
        if not name:
            return render_template('error.html', title="Invalid Input", message="Material name cannot be blank.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 400
        if not lot_number:
            return render_template('error.html', title="Invalid Input", message="Lot number cannot be blank.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 400
        if not quantity_str:
            return render_template('error.html', title="Invalid Input", message="Quantity cannot be blank.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 400
        if not received_date:
            return render_template('error.html', title="Invalid Input", message="Received date cannot be blank.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 400

        #get material_id, if no material_id redirect user to add_material route. 
        material_id = get_material_id(name)
        if material_id is None:
            return redirect(url_for('add_material_route',
                from_receive_lot='1',
                material_name=name,
                lot_number=lot_number,
                quantity=quantity_str,
                received_date=received_date,
                expiry_date=expiry_date or '',
                location=location or '',
                supplier=supplier or '',
                cost_per_unit_lot=cost_per_unit_str,
            ))

            #user will fill out needed on this route, 
            #then it will be directed back here. 

        #validate non string inputs.
        try:
            quantity = float(quantity_str)
            cost_per_unit = float(cost_per_unit_str) if cost_per_unit_str else None
        except ValueError:
            return render_template('error.html', title="Invalid Input", message="Quantity and Cost per Unit must be valid numbers.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 400

        #quant cant be negative
        if quantity <= 0:
            return render_template('error.html', title="Invalid Input", message="Quantity must be greater than 0.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 400

        if cost_per_unit is not None and cost_per_unit <= 0:
            return render_template('error.html', title="Invalid Input", message="Cost per Unit must be greater than 0.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 400

        # ADDED: units-conversion-layer feature — the quantity and cost are entered in the
        #        material's display unit (e.g. lb). Convert both to the stored base unit:
        #        quantity -> grams (to_base); cost-per-display-unit -> cost-per-gram, which is
        #        from_base() of the cost (cost/factor) so that quantity*cost stays the same total.
        #        For count materials the factor is 1, so both are identities.
        material_row = get_material_by_id(material_id)
        mat_unit = material_row[3] if material_row else None
        quantity = float(units.to_base(quantity, mat_unit))
        if cost_per_unit is not None:
            cost_per_unit = float(units.from_base(cost_per_unit, mat_unit))

        #call function, add to lots
        lot_id = inventory_receive_lot(material_id, lot_number, quantity, received_date, expiry_date, location, supplier, cost_per_unit)
        
        if lot_id is None: # if failed to add lot, return error page.
            return render_template('error.html', title="Error", message="Failed to add lot. Check logs for details.",
                back_link=True, back_link_url="/receive-lot", back_link_label="Go back"), 500

        #success. 
        log_action('lot_received', f"material={name}, lot={lot_number}, qty={quantity}, date={received_date}")
        return redirect(url_for('view_inventory'))



    # if GET request, show the receive lot form. if redirected from add_material route due to missing material, pre-fill the form with the data they entered.
    return render_template('receive_lot.html',
        material_name=request.args.get('material_name', ''),
        lot_number=request.args.get('lot_number', ''),
        quantity=request.args.get('quantity', ''),
        received_date=request.args.get('received_date', ''),
        expiry_date=request.args.get('expiry_date', ''),
        location=request.args.get('location', ''),
        supplier=request.args.get('supplier', ''),
        cost_per_unit=request.args.get('cost_per_unit', ''),
    )




@app.route('/add-material', methods=['GET', 'POST'])
# methods=['GET', 'POST'] tells Flask this route handles both GET and POST requests
@requires_auth
def add_material_route():
    """
    Add Material Route - Handles both showing form and processing submission

    This is a common pattern in web development:
    1. User visits /add-material (GET) → Show empty form (add_material.html)
    2. User fills form and clicks submit → Browser sends POST request
    3. Server processes POST → Adds material to database
    4. Server redirects to inventory page to show the new material
    """

    # Check which HTTP method was used
    if request.method == 'POST':
        """
        POST:

        request.form is a dictionary containing all form fields:
        - request.form.get('name') → Value from input with name="name"
        - request.form.get('category') → Value from input with name="category"
        - etc.

        Note: HTML form sends everything as strings, so we convert:
        - float() for decimal numbers (stock_level, reorder_level)
        - int() for whole numbers (if needed)
        """
        #input string parsing
        name = request.form.get('name') # Get value from <input name="name"> handled in add_material.html
        category=request.form.get('category')
        unit=request.form.get('unit')
        # ADDED: units-conversion-layer feature — material_type is the Weight/Count toggle
        #        ('mass' or 'count'); it becomes the material's `dimension`.
        dimension = request.form.get('material_type')

        name = name.strip() if name else None
        category = category.strip() if category else None
        unit = unit.strip() if unit else None
        dimension = dimension.strip().lower() if dimension else None

        
        
        #string input validation ( cant be none)
        if not name:
                return render_template('error.html',
                    title="Invalid Input",
                    message="Material name cannot be blank.",
                    back_link=True, back_link_url="/add-material", back_link_label="Go back"
                ), 400
        
        if not category:
                return render_template('error.html',
                    title="Invalid Input",
                    message="Material Category cannot be blank.",
                    back_link=True, back_link_url="/add-material", back_link_label="Go back"
                ), 400
        
        if not unit:
                return render_template('error.html',
                    title="Invalid Input",
                    message="Material Unit cannot be blank.",
                    back_link=True, back_link_url="/add-material", back_link_label="Go back"
                ), 400

        # ADDED: units-conversion-layer feature — validate the Weight/Count type and that the
        #        unit matches it. Weight materials must use a known mass unit (so quantities can
        #        convert to grams); Count materials use a free-text label and never convert.
        if dimension not in ("mass", "count"):
                return render_template('error.html',
                    title="Invalid Input",
                    message="Please choose whether this material is tracked by Weight or Count.",
                    back_link=True, back_link_url="/add-material", back_link_label="Go back"
                ), 400

        if dimension == "mass" and units.dimension_of(unit) != "mass":
                return render_template('error.html',
                    title="Invalid Input",
                    message=f"'{unit}' is not a recognized weight unit. Use one of: {', '.join(units.MASS_UNITS)}.",
                    back_link=True, back_link_url="/add-material", back_link_label="Go back"
                ), 400


        # numeric input validation (cant be <0 )
        try:
            reorder_level=float(request.form.get('reorder_level', 0))

        except ValueError:
            return render_template('error.html',
                title="Invalid Input",
                message="Reorder Level must be a valid number.",
                back_link=True, back_link_url="/add-material", back_link_label="Go back"
            ), 400

        if reorder_level < 0:
            return render_template('error.html',
                title="Invalid Input",
                message="Reorder Level must be a positive number.",
                back_link=True, back_link_url="/add-material", back_link_label="Go back"
            ), 400


        # ADDED: organic feature — read the edible/organic checkboxes (value="1" when checked,
        #        absent when unchecked). is_edible defaults checked in the form.
        is_edible = request.form.get('is_edible') == '1'
        is_organic = request.form.get('is_organic') == '1'

        # ADDED: units-conversion-layer feature — reorder_level is entered in the material's
        #        display unit; store it in the base unit (grams for mass, unchanged for count).
        reorder_level = float(units.to_base(reorder_level, unit))

        #function call.
        result = add_raw_material(
                name=name,
                category=category,
                unit=unit,
                reorder_level=reorder_level,
                is_edible=is_edible,      # ADDED: organic feature
                is_organic=is_organic,    # ADDED: organic feature
                dimension=dimension,      # ADDED: units-conversion-layer feature
            )

        from_receive_lot = request.form.get('from_receive_lot', '')

        if result == "duplicate": # error if name exists.
            return render_template("add_material.html",
                error=f"A material named '{name}' already exists. Please use a different name or update the existing one.",
                from_receive_lot=from_receive_lot,
                material_name=name,
                lot_number=request.form.get('lot_number', ''),
                quantity=request.form.get('quantity', ''),
                received_date=request.form.get('received_date', ''),
                expiry_date=request.form.get('expiry_date', ''),
                location=request.form.get('location', ''),
                supplier_lot=request.form.get('supplier_lot', ''),
                cost_per_unit_lot=request.form.get('cost_per_unit_lot', ''),
            )

        elif result: # if result is good.
            logging.info(f"Material added: '{name}' | category={category}, unit={unit}")
            log_action('material_added', f"name={name}, category={category}, unit={unit}")
            if from_receive_lot == '1': #if this route was directed via lot recieve func, go back to that when done.
                return redirect(url_for('receive_lot',
                    material_name=name,
                    lot_number=request.form.get('lot_number', ''),
                    quantity=request.form.get('quantity', ''),
                    received_date=request.form.get('received_date', ''),
                    expiry_date=request.form.get('expiry_date', ''),
                    location=request.form.get('location', ''),
                    supplier=request.form.get('supplier_lot', ''),
                    cost_per_unit=request.form.get('cost_per_unit_lot', ''),
                ))
            return redirect(url_for('view_inventory'))
        else:
            return ("Error adding material", 500)

        

   
 #note: we dont need if request.method == 'GET' here, because if it's not POST, it must be GET (since we only specified those two methods). 
 # so we can just put the code to show the form outside of the if statement, and it will run when it's a GET request.

    # if GET request, show the add material form. if redirected from receive_lot route due to missing material, pre-fill the form with the data they entered in receive_lot.
    from_receive_lot = request.args.get('from_receive_lot', '')
    return render_template("add_material.html",
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home",
        from_receive_lot=from_receive_lot,
        material_name=request.args.get('material_name', ''),
        lot_number=request.args.get('lot_number', ''),
        quantity=request.args.get('quantity', ''),
        received_date=request.args.get('received_date', ''),
        expiry_date=request.args.get('expiry_date', ''),
        location=request.args.get('location', ''),
        supplier_lot=request.args.get('supplier', ''),
    )






# ========================
# LOW STOCK ALERTS
# ========================

@app.route('/low-stock')
@requires_auth
def low_stock():
    """
    to show stock that is below the reorder level.
    simple GET request
    """
    # Get all materials where stock is below reorder level
    df = get_low_stock_materials()
    materials = df.to_dict(orient='records') if not df.empty else [] # convert each material (row) into dictionary, all dictionarys in a list. 
    return render_template("low_stock.html",
        materials=materials,
        count=len(df),
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
)



@app.route('/export/low-stock-excel')
@requires_auth
def export_low_stock_excel():
   """
   uses GET request to export low stock materials to excel, similar to export_inventory_excel but for low stock items. this is a button in low_stock.html.
   """
   
   df = get_low_stock_materials()  # Get materials below reorder level

   if df.empty:
        return "No data to export", 400  # No low stock items to export

    # Create Excel file (e.g., low_stock_20260203_143022.xlsx)
   filepath = export_to_excel(df, 'low_stock')

    # Trigger browser download
   return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))



#=============
#INVENTORY MANAGEMENT PAGES (edit/delete materials)
#=============

@app.route('/manage-materials') # Page to view all materials with edit/delete options 
@requires_auth
def manage_materials():
    """
    This is a page to view all materials with edit/delete options.
    simple GET, just viewing, but with buttons. 
    """
    # ADDED: pagination for manage-materials page
    PER_PAGE = 50
    page = min(max(1, int(request.args.get('page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # ADDED: sort feature — column + direction from URL (validated/whitelisted in the service)
    sort = request.args.get('sort')
    direction = request.args.get('dir', 'asc')

    # CHANGED: get_all_materials now returns (df, total) tuple
    df, total = get_all_materials(page=page, per_page=PER_PAGE, sort_by=sort, sort_dir=direction)

    materials = df.to_dict(orient='records') if (df is not None and not df.empty) else []
    # ADDED: total_pages for pagination controls in template
    total_pages = math.ceil(total / PER_PAGE) if total else 1

    return render_template("manage_materials.html",
        materials=materials,
        count=len(materials),
        page=page,
        total_pages=total_pages,
        total=total,
        sort=sort,
        direction=direction,
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
)



# edit material page
@app.route('/edit-material/<int:material_id>') # dynamic URL, the main difference of this url is that it takes material_id as a parameter, 
# which is used to identify which material to edit. this is needed because we have multiple 
# materials and we need to know which one we're editing. the <int:material_id> part tells Flask to expect an 
# integer parameter in that part of the URL, and it will pass it to the function as material_id.

# it knows which material to edit based on the material_id in the URL, which is passed to the function as a parameter. when user clicks "edit" button for a material, 
# it takes them to /edit-material/<material_id>, the route to this funciton is in manage_materials.html, where the edit button for each material has a link to 
# /edit-material/{{ material.material_id }}.
@requires_auth

def edit_material(material_id):
    """
    function to edit a material, 

    uses GET to show the edit form with current material details.
    """
    material = get_material_by_id(material_id) # get the material being edited. 
    if not material:
        return render_template('error.html', # if not found, show error page.
            title="Material Not Found",
            message="The material you requested could not be found.",
            back_link=True, back_link_url="/manage-materials", back_link_label="Back to Manage Materials"
        ), 404


    
    # CHANGED: organic feature — get_material_by_id now also returns is_edible/is_organic.
    # CHANGED: units-conversion-layer feature — also returns dimension.
    mat_id, name, category,  unit, reorder_level, is_edible, is_organic, dimension = material # unpack material details for display in edit form.

    # ADDED: units-conversion-layer feature — reorder_level is stored in the base unit (grams
    #        for mass); convert back to the display unit so the user edits the value they see.
    if reorder_level is not None:
        reorder_level = float(units.from_base(reorder_level, unit))

    msg = request.args.get('msg', '')
    err = request.args.get('err', '')
    return render_template("edit_material.html",
        mat_id=mat_id, name=name, category=category,
        unit=unit, reorder_level=reorder_level,
        dimension=dimension,  # ADDED: units-conversion-layer feature
        is_edible=is_edible, is_organic=is_organic,  # ADDED: organic feature
        msg=msg, err=err,
        back_link=True,
        back_link_url="/manage-materials",
        back_link_label="Back to Manage Materials"
)





@app.route('/edit-material/<int:material_id>/update-details', methods=['POST'])  # very similar to adjust_stock route, but for updating material details instead of stock level. it also takes material_id to know which material to update, and only accepts POST requests since it's processing a form submission.
# Route to handle updating material details (name, category, etc)
@requires_auth

def update_material_details(material_id): 
    
    try:
    
        name = request.form.get('name') # Get updated name from form
        name = name.strip() if name else None#validate
        category = request.form.get('category')  # Get updated category (or None if empty)
        category = category.strip() if category else None#validate
        unit = request.form.get('unit') # Get updated unit (or None if empty)
        unit = unit.strip() if unit else None #validate
        # ADDED: units-conversion-layer feature — Weight/Count toggle -> dimension.
        dimension = request.form.get('material_type')
        dimension = dimension.strip().lower() if dimension else None
        reorder_level_str = request.form.get('reorder_level')
        reorder_level = float(reorder_level_str) if reorder_level_str else 0

        # ADDED: organic feature — checkboxes absent from a POST mean unchecked, so pass
        #        explicit True/False (not None) to persist an unchecked box.
        is_edible = request.form.get('is_edible') == '1'
        is_organic = request.form.get('is_organic') == '1'

    except ValueError:
        return render_template('error.html',
            title="Invalid Input",
            message = "Reorder Level must be a valid number.",
            back_link=True, back_link_url=f"/edit-material/{material_id}", back_link_label="Go back"
        ), 400

    if reorder_level is not None and reorder_level < 0:
        return render_template('error.html',
            title="Invalid Input",
            message = "Reorder Level cannot be negative.",
            back_link=True, back_link_url=f"/edit-material/{material_id}", back_link_label="Go back"
        ), 400

    # ADDED: units-conversion-layer feature — validate the type/unit pairing, then convert the
    #        reorder level from the displayed unit back to the stored base unit.
    if dimension not in ("mass", "count"):
        return render_template('error.html',
            title="Invalid Input",
            message="Please choose whether this material is tracked by Weight or Count.",
            back_link=True, back_link_url=f"/edit-material/{material_id}", back_link_label="Go back"
        ), 400
    if dimension == "mass" and units.dimension_of(unit) != "mass":
        return render_template('error.html',
            title="Invalid Input",
            message=f"'{unit}' is not a recognized weight unit. Use one of: {', '.join(units.MASS_UNITS)}.",
            back_link=True, back_link_url=f"/edit-material/{material_id}", back_link_label="Go back"
        ), 400
    reorder_level = float(units.to_base(reorder_level, unit))

    result = update_raw_material(material_id, name=name, category=category, unit=unit,
                                 reorder_level=reorder_level,
                                 dimension=dimension,  # ADDED: units-conversion-layer feature
                                 is_edible=is_edible, is_organic=is_organic)  # ADDED: organic feature

    if result:
        logging.info(f"Material details updated: material_id={material_id}, name={name}, category={category}, unit={unit}")
        log_action('material_updated', f"material_id={material_id}, name={name}, category={category}")
        # CHANGED: was url_for(..., msg=...) — moved to flash() so messages are session-backed
        flash('Details updated successfully', 'success')
        return redirect(url_for('edit_material', material_id=material_id))
    else:
        flash('Failed to update details', 'error')
        return redirect(url_for('edit_material', material_id=material_id))
# Note: The update_raw_material function would need to be implemented in the database module to handle updating the material details based on the provided parameters.

@app.route('/edit-material/<int:material_id>/delete', methods=['POST']) 
# Route to handle deleting a material from inventory
@requires_auth
def delete_material(material_id):
    
    result = delete_raw_material(material_id)
    if not result:
        return render_template('error.html',
            title="Delete Failed",
            message="Could not delete material. Please try again.",
        ), 500
    logging.info(f"Material deleted: material_id={material_id}")
    log_action('material_deleted', f"material_id={material_id}")
    if result.get("affected_batches"):
        count = len(result["affected_batches"])
        warning = f"Material deleted. Note: it was used in {count} batch(es) — those records no longer show this ingredient."
        # CHANGED: was url_for(..., warning=...) — moved to flash()
        flash(warning, 'warning')
        return redirect(url_for('manage_materials'))
    return redirect(url_for('manage_materials'))


# ADDED: organic feature — inline toggle for is_edible/is_organic straight from the
#        manage-materials table (no full edit-form round trip). Flips a single boolean flag.
@app.route('/material/<int:material_id>/toggle/<field>', methods=['POST'])
@requires_auth
def toggle_material_flag(material_id, field):
    # Allowlist guards which columns this route may touch (defense in depth alongside
    # update_raw_material's _MATERIAL_UPDATABLE_COLS check).
    if field not in ('is_edible', 'is_organic'):
        return render_template('error.html',
            title="Invalid Request",
            message="Unknown material flag.",
            back_link=True, back_link_url="/manage-materials", back_link_label="Back to Manage Materials"
        ), 400

    material = get_material_by_id(material_id)
    if not material:
        return render_template('error.html',
            title="Material Not Found",
            message="The material you requested could not be found.",
            back_link=True, back_link_url="/manage-materials", back_link_label="Back to Manage Materials"
        ), 404

    # get_material_by_id returns (mat_id, name, category, unit, reorder_level, is_edible, is_organic)
    current = bool(material[5] if field == 'is_edible' else material[6])
    update_raw_material(material_id, **{field: not current})
    log_action('material_flag_toggled', f"material_id={material_id}, {field}={not current}")

    # Preserve the manage-materials pagination position the user clicked from.
    page = request.args.get('page', 1)
    return redirect(url_for('manage_materials', page=page))



##############
# BATCHES PAGES
###############

"""
1. View ready batches page (/batches) - shows all batches with details and status (ready only). NEW IMPLIMENTATION: 
button to move batch as shipped from ready [POST route to mark as shipped], and button to export ready batches to excel [GET to see batches page, then click export button to trigger excel export and download]

2. create batch page (/create-batch) - form to create a new batch by selecting product recipe, quantity, and optional notes [GET and POST routes]

3. view shipped batches page (/shipped-batches) - shows all shipped batches with details [GET route]
   - has export to excell option as well [GET route to trigger excel export and download]
4. manage batches page (/manage-batches) - view all batches with edit/delete options [GET and POST routes] (similar to manage materials page)
- edit batch page (/edit-batch/<batch_id>) - form to edit batch details, change status, or delete batch [GET and POST routes]
"""

#View all batches
@app.route('/batches')
@requires_auth
def view_batches():
    """
    currently only uses get to show all ready batches, but we will need to add POST routes for marking as shipped.
    
    """

    # ADDED: two independent page params — ready and planned sections paginate separately
    PER_PAGE = 50
    ready_page   = max(1, int(request.args.get('ready_page', 1)))
    planned_page = min(max(1, int(request.args.get('planned_page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # ADDED: sort feature — each section sorts by its date column, newest first by default.
    # Only 'asc'/'desc' are meaningful; the service whitelists anything else to 'desc'.
    ready_sort   = 'asc' if request.args.get('ready_sort') == 'asc' else 'desc'
    planned_sort = 'asc' if request.args.get('planned_sort') == 'asc' else 'desc'

    # CHANGED: get_batches and get_batches_planned now return (df, total) tuples
    data, ready_total         = get_batches(page=ready_page, per_page=PER_PAGE, sort=ready_sort)
    planned_data, planned_total = get_batches_planned(page=planned_page, per_page=PER_PAGE, sort=planned_sort)

    # PRESERVED: Python standard/mix split — unchanged, still runs on whatever slice the query returned
    all_ready    = data.to_dict(orient='records') if not data.empty else []
    ready_standard = [b for b in all_ready if b.get('batch_type') != 'mix']
    ready_mix      = [b for b in all_ready if b.get('batch_type') == 'mix']

    # mix-usage feature: mix_remaining comes from the query in BASE units (grams for mass).
    # Convert it to the batch's product_unit so the Component row shows the same unit as its
    # produced quantity. Guard against missing unit / NULL remaining (non-recipe or non-mix rows).
    for b in ready_mix:
        rem = b.get('mix_remaining')
        unit = b.get('product_unit')
        if rem is not None and unit:
            # Round to 6 dp to match fmt_num display precision — eliminates float residuals
            # (e.g. 1e-15 kg after full deduction) that show "0" but don't trigger <= 0.
            b['mix_remaining'] = round(float(units.from_base(rem, unit)), 6)
    # Sort exhausted mixes (depleted output lot) to the bottom, keeping the existing order otherwise.
    ready_mix.sort(key=lambda b: 1 if (b.get('mix_remaining') is not None and b['mix_remaining'] <= 0) else 0)

    all_planned    = planned_data.to_dict(orient='records') if not planned_data.empty else []
    planned_standard = [b for b in all_planned if b.get('batch_type') != 'mix']
    planned_mix      = [b for b in all_planned if b.get('batch_type') == 'mix']

    # ADDED: compute total pages for each section's pagination nav
    ready_total_pages   = math.ceil(ready_total / PER_PAGE) if ready_total else 1
    planned_total_pages = math.ceil(planned_total / PER_PAGE) if planned_total else 1

    return render_template("batches.html",
        ready_standard=ready_standard,
        ready_mix=ready_mix,
        planned_standard=planned_standard,
        planned_mix=planned_mix,
        ready_page=ready_page,
        ready_total_pages=ready_total_pages,
        planned_page=planned_page,
        planned_total_pages=planned_total_pages,
        # sort feature — current direction per section, so headers can render/toggle the arrow
        ready_sort=ready_sort,
        planned_sort=planned_sort,
        # row-numbering feature — per-tab page offsets so each tab's # continues across its pages.
        ready_offset=(ready_page - 1) * PER_PAGE,
        planned_offset=(planned_page - 1) * PER_PAGE,
        back_link=True, back_link_url="/", back_link_label="Back to Home"
)

# mark as shipped button. 
@app.route('/batches/<int:batch_id>/mark-shipped', methods=['POST']) # this is a button in batches.html for each batch, it sends a POST request to this route with the batch_id to mark that batch as shipped.
@requires_auth
def mark_batch_as_shipped(batch_id):
    """
    click button, mark batch as shipped. this is a POST route
    
    """
    result = mark_as_shipped(batch_id) # call function to update batch status in database.
    if not result:
        return render_template('error.html',
            title="Ship Failed",
            message="Could not mark batch as shipped. The batch may not exist.",
        ), 500
    logging.info(f"Batch marked as shipped: batch_id={batch_id}")
    log_action('batch_shipped', f"batch_id={batch_id}")
    return redirect(url_for('view_batches')) # after marking as shipped, redirect back to batches page to see updated status.


@app.route('/batches/<int:batch_id>/materials')
@requires_auth
def batch_materials(batch_id):
    """
    note: also called by get_shipped_batch_materials

    shows in details when clicking on detials of batch, 
    when calling function, it returns: material name, quantity, unit, batch material lot id, lot number, and batch id,
    but for batch details, user only sees: material name, quantity used, unit, and lot number. 
    
    the batch material lot id and batch id are just for reference and not shown to user.

    CHANGED: planned-deduction-mode feature — the response used to be a bare JSON array. It is now
    an object, because a deferred Planned batch has no batch_materials rows and the drawer must
    still show its chosen lots AND say they are only reserved:
        {"materials": [...], "planned": bool, "fifo": bool}
    planned = these quantities are reservations, not deductions.
    fifo    = deferred batch with no stored picks; the oldest lots get used at the planned date.
    All five JS callers go through renderBatchMaterials() in static/js/batch_materials.js (loaded
    globally in base.html), so this shape is unpacked in exactly one place: batches.html,
    manage_batches.html, edit_batch.html, shipped_batches.html, shipment_detail.html.
    The shipment-side two were missed when the shape changed and silently rendered "Failed to load
    materials" until they were migrated too — keep new callers on the shared renderer.
    """
    rows = get_batch_materials(batch_id)
    planned = False

    # A deferred Planned batch hasn't deducted anything yet, so batch_materials is empty. Fall back
    # to the lot picks stored on the batch itself — otherwise the drawer reads "No materials
    # recorded" for a batch whose lots the user explicitly chose.
    if not rows:
        rows = get_planned_lot_selections(batch_id)
        planned = bool(rows)

    # ADDED: cost-of-material feature — expose cost_per_unit (r[6]) so batches.html can
    #        display it in the materials expansion panel per line item.
    # CHANGED: units-conversion-layer feature — quantity_used is stored in the base unit
    #          (grams for mass), so the expand panel was showing raw gram values. Convert it
    #          back to the material's display unit via disp() (identity for count units) so it
    #          shows e.g. "1" kg instead of "1000".
    materials = [
        {'material_name': r[0], 'quantity_used': disp(r[1], r[2]), 'unit': r[2], 'batch_material_lot_id': r[3], 'lot_number': r[4], 'material_id': r[5], 'cost_per_unit': r[6],
         # planned rows carry an 8th element flagging a lot that has since expired/been used up
         'lot_status': (r[7] if len(r) > 7 else 'active')}
        for r in rows
    ]

    # Distinguish "deferred, no picks → FIFO at promotion" from "nothing here at all".
    fifo = False
    if not materials:
        batch = get_batch_by_id(batch_id)
        fifo = bool(batch) and batch[4] == 'Planned' and batch[11] == 'deferred'

    return jsonify({'materials': materials, 'planned': planned, 'fifo': fifo})



# Excel export route for batches
@app.route('/export/batches-excel')
@requires_auth
def export_batches_excel():
    """
    Export batches to a multi-sheet Excel workbook.

    CHANGED (excel-exports feature): was a single sheet of Ready batches. Now ONE workbook with:
      - "Planned": deferred/finished batches awaiting promotion (get_batches_planned).
      - "Ready":   produced batches with remaining quantity (get_batches).

    WHY no "Shipped" sheet: shipped data now lives in the shipments workbook. Because a batch can
    be split across several shipments, a per-batch whole-batch "shipped" row is no longer a clean
    value — the shipments export models it correctly off shipment_batches instead.
    """
    # Both return (df, total); page=None → full table, no pagination.
    planned, _ = get_batches_planned(page=None)
    ready, _ = get_batches(page=None)

    # If both are empty there's nothing meaningful to export.
    if planned.empty and ready.empty:
        return "No data to export", 400  # No batches to export

    # One workbook, two tabs (Planned first, then Ready).
    filepath = export_multi_sheet_to_excel({'Planned': planned, 'Ready': ready}, 'batches')

    # Trigger browser download
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


# NEW: View shipped batches
@app.route('/shipped-batches')
@requires_auth
def view_shipped_batches():
    """
    gets all shipped batches and displays them in a table
    """

    # ADDED: pagination for shipped batches
    PER_PAGE = 50
    page = min(max(1, int(request.args.get('page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # ADDED: sort feature — by shipment date, newest first by default (service whitelists direction)
    sort = 'asc' if request.args.get('sort') == 'asc' else 'desc'

    # CHANGED: get_batches_shipped now returns (df, total) tuple
    df, total = get_batches_shipped(page=page, per_page=PER_PAGE, sort=sort)

    batches = df.to_dict(orient='records') if not df.empty else []
    columns = list(df.columns) if not df.empty else []
    # ADDED: total_pages for pagination controls in the template
    total_pages = math.ceil(total / PER_PAGE) if total else 1

    return render_template("shipped_batches.html",
        batches=batches, columns=columns, count=len(batches),
        page=page,
        total_pages=total_pages,
        total=total,
        # sort feature — current direction so the "Shipped" header can render/toggle the arrow
        sort=sort,
        back_link=True, back_link_url="/", back_link_label="Back to Home"
)


# REMOVED (excel-exports feature): /export/shipped-batches-excel. Shipped data is now exported
#   from the shipments workbook (/export/shipments-excel), which models batch-splits-across-
#   shipments correctly via shipment_batches. A whole-batch "shipped" export is redundant and
#   misleading now that a batch can be partially shipped on multiple shipments. The /shipped-batches
#   VIEW page and get_batches_shipped query remain — only this export route was removed.


# NEW (excel-exports feature): Excel export route for shipments.
@app.route('/export/shipments-excel')
@requires_auth
def export_shipments_excel():
    """
    Export shipments to a multi-sheet Excel workbook:
      - "Shipments":      one row per shipment, with batch_count and total_units (summary).
      - "Shipment Detail": one row per batch-split (shipment_batches), with the SPLIT quantity.

    This replaces the old shipped-batches export. Because batches split across shipments,
    shipment_batches is the source of truth for "what actually shipped".
    """
    summary = get_shipments_summary()
    details = get_shipment_details()

    # Nothing to export if there are no shipments at all.
    if summary.empty and details.empty:
        return "No data to export", 400

    # One workbook, two tabs (summary first, then line-level detail).
    filepath = export_multi_sheet_to_excel(
        {'Shipments': summary, 'Shipment Detail': details}, 'shipments')

    # Trigger browser download
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


@app.route('/create-batch', methods=['GET', 'POST'])
@requires_auth
def create_batch():
    """
    Adds batches to database based on form submission.
    uses GET to show the form, and POST to process the form submission.
    """

    if request.method == 'POST':

        # test parsing
        product_name = request.form.get('product_name')
        product_name = product_name.strip() if product_name else None
        notes = request.form.get('notes')
        notes = notes.strip() if notes else None
        expiration_date = request.form.get('expiration_date', '').strip() or None
        planned_completion_date = request.form.get('planned_completion_date', '').strip() or None
        # Text validation
        if not product_name:
            return render_template('error.html',
                title="Invalid Input",
                message="Product name cannot be blank.",
                back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
            ), 400

        # CHANGED: batch-type-on-recipe feature — the batch type is no longer chosen on this form.
        #          It's an intrinsic property of the recipe, so resolve it from the recipe here.
        batch_type = get_recipe_batch_type(product_name)
        if batch_type not in ('mix', 'finished'):
            return render_template('error.html',
                title="Invalid Input",
                message="No recipe found for this product, or it has no batch type set. Create the recipe first.",
                back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
            ), 400

        batch_number = request.form.get('batch_number', '').strip()
        if not batch_number:
            return render_template('error.html',
                title="Invalid Input",
                message="Batch number cannot be blank.",
                back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
            ), 400

        #  numeric parsing
        try:
            quantity = float(request.form.get('quantity'))
        except ValueError:
            return render_template('error.html',
                title="Invalid Input",
                message="Quantity must be a valid number.",
                back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
            ), 400

        # numeric validation
        if quantity <= 0:
            return render_template('error.html',
                title="Invalid Input",
                message="Quantity must be greater than zero.",
                back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
            ), 400

        if expiration_date:
            try:
                datetime.strptime(expiration_date, '%Y-%m-%d')
            except ValueError:
                return render_template('error.html',
                    title="Invalid Input",
                    message="Invalid expiration date format. Use YYYY-MM-DD.",
                    back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
                ), 400

        # validate planned_completion_date
        
        if planned_completion_date:
            try:
                pcd = datetime.strptime(planned_completion_date, '%Y-%m-%d') #valid date
                # CHANGED: server-timezone-vs-business-timezone fix — was datetime.now().date(),
                # the server's own clock (UTC on Render). Botaniks runs Pacific; comparing against
                # UTC rejected a Pacific "today" as being in the past for several hours every
                # evening. business_today() is the calendar day Botaniks is actually in.
                if pcd.date() < business_today(): # planned date can be today or later, just not the past
                    return render_template('error.html',
                        title="Invalid Input",
                        message="Planned completion date cannot be in the past.",
                        back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
                    ), 400
            except ValueError:
                return render_template('error.html',
                    title="Invalid Input",
                    message="Invalid planned completion date format. Use YYYY-MM-DD.",
                    back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
                ), 400
        
        #LOT SELECTION PARSING:
        lot_selections_raw = request.form.get('lot_selection', '').strip()# get the raw string from the form
        lot_selections = None
        if lot_selections_raw:
            try:
                raw = json.loads(lot_selections_raw) # parse the JSON string into a Python object (list of dicts)
                lot_selections = {int(k): v for k, v in raw.items()} # convert keys to int for easier handling later
            except (ValueError, TypeError):
                return render_template(
                    'error.html',
                    title="Invalid Input",
                    message="Invalid lot selection format.",
                    back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
                ), 400




        #INPUT VALIDATION DONE===============================


        # ADDED: planned-deduction-mode feature — the create form's toggle. Deferral is only
        # meaningful for a Planned finished/mix batch; anything else deducts at creation, so we
        # normalize to 'immediate' rather than trusting the posted value. 'immediate' is the default,
        # including when the field is absent (JS off, or the field was hidden because no date was set).
        deduction_mode = 'immediate'
        if (request.form.get('deduction_mode') == 'deferred'
                and planned_completion_date
                and batch_type in ('finished', 'mix')):
            deduction_mode = 'deferred'

        # see if its okay that batch creation makes stock go negative.
        # CHANGED: planned-deduction-mode feature — was derived from batch_type + planned date alone.
        # A planned+immediate batch deducts now, so it must pass the same negative-stock gate below
        # as a Ready batch.
        defer_deduction = (batch_type in ('finished', 'mix') and bool(planned_completion_date) and deduction_mode == 'deferred')
        # A planned batch dated TODAY is immediately due — it tries (and fails) to promote on the
        # next batches view if stock is short, leaving a silently-stuck Planned batch with no alert.
        # So validate stock up front for same-day plans, giving the same "inadequate stock" alert as
        # an immediate batch. Future-dated plans are intentionally exempt: they exist to reserve
        # production against stock that hasn't arrived yet. (pcd was parsed above when the date is set;
        # bool(...) short-circuits so pcd is never read when planned_completion_date is blank.)
        # CHANGED: server-timezone-vs-business-timezone fix — was datetime.now().date().
        planned_today = bool(planned_completion_date) and pcd.date() == business_today()
        # Gated off: the override can never be true while ALLOW_NEGATIVE_STOCK_OVERRIDE is False,
        # so add_to_batches always runs with allow_negative=False (its per-lot check is the backstop).
        confirm_negative = ALLOW_NEGATIVE_STOCK_OVERRIDE and request.form.get('confirm_negative') == '1'

        if (not defer_deduction or planned_today) and not confirm_negative:
            negative_materials = check_negative_stock(product_name, quantity)
            if negative_materials:
                if ALLOW_NEGATIVE_STOCK_OVERRIDE:
                    # Override enabled — show the confirmable warning with a "Proceed Anyway" path.
                    return render_template('confirm_negative_stock.html',
                        negative_materials=negative_materials,
                        form_data={
                            'product_name': product_name,
                            'quantity': quantity,
                            'notes': notes or '',
                            'batch_number': batch_number,
                            'expiration_date': expiration_date or '',
                            'planned_completion_date': planned_completion_date or '',
                            # planned-deduction-mode feature — must round-trip the re-POST or the
                            # user's choice silently reverts to 'immediate'.
                            'deduction_mode': deduction_mode,
                            # batch-type-on-recipe feature — type is re-resolved from the recipe on the
                            # re-POST, so it no longer needs to round-trip through this confirm form.
                            'lot_selections': lot_selections_raw, # raw JSON string, must be string because
                            #confirm_negative_stock.html needs to pass back an GTML form hidden input (whcih only holds strings).
                        }
                    )
                # Override disabled (current) — hard block: show the shortfall and stop. No proceed.
                return render_template('insufficient_stock.html',
                    negative_materials=negative_materials,
                    product_name=product_name), 400

        # call function
        try:
            result = add_to_batches(product_name, quantity, notes=notes, batch_number=batch_number, deduct_resources=True, expiration_date=expiration_date, planned_completion_date=planned_completion_date, batch_type=batch_type, allow_negative=confirm_negative, lot_selections=lot_selections, deduction_mode=deduction_mode)
            if result:
                logging.info(f"Batch created: product='{product_name}', quantity={quantity}, batch_number={batch_number}, batch_type={batch_type}")
                log_action('batch_created', f"product={product_name}, quantity={quantity}, batch_id={result}, batch_number={batch_number}, batch_type={batch_type}, deduction_mode={deduction_mode}")
                return redirect(url_for('view_batches'))
            else:
                return render_template('error.html',
                    title="Couldn't Create Batch",
                    message="The batch couldn't be created. A lot you selected may have just changed "
                            "(used up or expired) since you opened the form. Reload the Create Batch "
                            "page and try again.",
                    back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
                ), 500

        except ValueError as e:
            # CHANGED: was str(e) — raw ValueError messages can leak internal schema/logic details.
            # Log the real error server-side and show a safe generic message to the user.
            logging.warning(f"create_batch ValueError: {e}")
            return render_template('error.html',
                title="Couldn't Create Batch",
                message="Please check your selections and try again. A lot you picked may have "
                        "expired or been used up since you opened the form — reload the Create "
                        "Batch page so it shows current stock, then retry.",
                back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
            ), 400

        except Exception as e:
            logging.error(f"create_batch: {e}", exc_info=True)
            return render_template('error.html',
                title="Unexpected Error",
                message="An unexpected error occurred. Please try again.",
                back_link=True, back_link_url="/create-batch", back_link_label="Go back to Create Batch"
            ), 500


    # CHANGED: get_all_materials now returns (df, total). page=None fetches the full table
    #          so the housemade filter below still sees every material.
    all_materials_df, _ = get_all_materials(page=None)
    if not all_materials_df.empty:
        housemade_materials = all_materials_df[all_materials_df['is_housemade'] == True][['name', 'stock_level', 'unit']].to_dict(orient='records')
    else:
        housemade_materials = []
    return render_template("create_batch.html",
        housemade_materials = housemade_materials,
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
    )

#========================
#MANAGMENT OF BATCHES 
#======================
    
@app.route('/manage-batches') # Page to view all batches with edit/delete options 
@requires_auth
def manage_batches():
    """
    This is a page to view all batches with edit/delete options.
    simple GET, just viewing, but with buttons. 
    """
    # ADDED: pagination for manage-batches page
    PER_PAGE = 50
    page = min(max(1, int(request.args.get('page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # CHANGED: get_all_batches_with_id now returns (df, total) tuple
    df, total = get_all_batches_with_id(page=page, per_page=PER_PAGE)

    batches = df.to_dict(orient='records') if (df is not None and not df.empty) else []
    # ADDED: total_pages for pagination controls in the template
    total_pages = math.ceil(total / PER_PAGE) if total else 1

    return render_template("manage_batches.html",
        batches=batches,
        count=len(batches),
        page=page,
        total_pages=total_pages,
        total=total,
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
)




@app.route('/edit-batch/<int:batch_id>') # dynamic URL for editing a specific batch, identified by batch_id. 
@requires_auth
def edit_batch(batch_id):
    """
    edit batch details, not status, this is from manage batches page,
      where each batch has an edit button that takes you to 
      this page with the batch_id in the URL. this page will show a form
        with current batch details filled in

        GET page route
    """

    batch = get_batch_by_id(batch_id) # get batch details for the batch being edited.
    batch_materials_rows = get_batch_materials(batch_id) # get materials used in this batch, to show in the edit page and allow adjustments.
    batch_materials = [] #get materials df and convert to list of dicts for display in edit batch page.
    for r in batch_materials_rows:
        batch_materials.append({'material_name': r[0], 'quantity_used': r[1], 'lot_id': r[3], 'lot_number': r[4], 'material_id': r[5]}) # convert materials to a format for display in the edit batch page.

    if not batch:
        return render_template('error.html', # if batch not found, show error page.
            title="Batch Not Found",
            message="The batch you requested could not be found.",
            back_link=True, back_link_url="/manage-batches", back_link_label="Back to Manage Batches"
        ), 404

    # REMOVED: planned-deduction-mode feature — a "Batch Materials Not Found" 404 used to fire here
    # whenever get_batch_materials() came back empty. That made every deferred Planned batch
    # un-editable: it legitimately has ZERO batch_materials rows until it promotes. An existing batch
    # is always editable; when it has no deductions we show its planned lot picks instead.
    is_deferred_planned = (batch[4] == 'Planned' and batch[11] == 'deferred')
    planned_lots = []
    if is_deferred_planned:
        planned_lots = [
            {'material_name': r[0], 'quantity_used': r[1], 'unit': r[2], 'lot_id': r[3],
             'lot_number': r[4], 'material_id': r[5], 'lot_status': r[7]}
            for r in get_planned_lot_selections(batch_id)
        ]

    msg = request.args.get('msg', '') # get success message from URL parameters, if any (e.g., after updating batch details)
    err = request.args.get('err', '') # get error message from URL parameters, if any (e.g., if updating batch details failed)

    return render_template("edit_batch.html", # show the edit batch form, with current batch details and any success/error messages.
        batch = batch,
        batch_materials = batch_materials,
        # planned-deduction-mode feature — drives the "Planned lot selection" card, which replaces
        # the deduction-adjust UI for a batch that hasn't deducted anything yet.
        is_deferred_planned = is_deferred_planned,
        planned_lots = planned_lots,
        msg=msg, err=err,
        back_link=True,
        back_link_url="/manage-batches",
        back_link_label="Back to Manage Batches" )



@app.route('/edit-batch/<int:batch_id>/update-details', methods=['POST']) # POST route for processing the edit batch form submission, where batch_id identifies which batch to update.
@requires_auth
def update_batch_details(batch_id):
    """
    processes the form submission from edit batch page to update batch details (not status). 
    this is a POST route that takes the updated details from the form and updates the batch in the database, then redirects back to the edit batch page with a success or error message.
    """
    try:
        product_name = request.form.get('product_name')
        product_name = product_name.strip() if product_name else None
        quantity_str = request.form.get('quantity')
        notes = request.form.get('notes')
        notes = notes.strip() if notes else None
        quantity = float(quantity_str) if quantity_str else None
        expiration_date = request.form.get('expiration_date', '').strip() or None
        planned_completion_date = request.form.get('planned_completion_date', '').strip() or None

    except ValueError:
        return render_template('error.html',
            title="Invalid Input",
            message="Quantity must be a valid number.",
            back_link=True, back_link_url=f"/edit-batch/{batch_id}", back_link_label="Go back to Edit Batch"
        ), 400

    if quantity is not None and quantity <= 0:
        return render_template('error.html',
            title="Invalid Input",
            message="Quantity must be greater than 0.",
            back_link=True, back_link_url=f"/edit-batch/{batch_id}", back_link_label="Go back to Edit Batch"
        ), 400

    if expiration_date:
        try:
            datetime.strptime(expiration_date, '%Y-%m-%d')
        except ValueError:
            return render_template('error.html',
                title="Invalid Input",
                message="Invalid expiration date format. Use YYYY-MM-DD.",
                back_link=True, back_link_url=f"/edit-batch/{batch_id}", back_link_label="Go back to Edit Batch"
            ), 400

    #validate planned completion date
    
    if planned_completion_date:
        try:
            datetime.strptime(planned_completion_date, '%Y-%m-%d')
        except ValueError:
            return render_template('error.html',
                title="Invalid Input",
                message="Invalid planned completion date format. Use YYYY-MM-DD.",
                back_link=True, back_link_url=f"/edit-batch/{batch_id}", back_link_label="Go back to Edit Batch"
            ), 400
        
    new_quantities = {}
    lot_selections = {}
    if request.form.get('adjust_deductions') == '1':
        for key, value in request.form.items():
            if key.startswith('material_'):
                try:
                    material_id = int(key[len('material_'):])
                    new_quantities[material_id] = float(value)
                except (ValueError, TypeError):
                    pass

        # Parse lot selections for materials where quantity is increasing
        import json as _json
        raw_lot_selections = request.form.get('lot_selections_json', '')
        if raw_lot_selections:
            try:
                parsed = _json.loads(raw_lot_selections)
                lot_selections = {int(k): int(v) for k, v in parsed.items()}
            except (ValueError, TypeError, _json.JSONDecodeError):
                pass

        if new_quantities and not check_batch_materials_stock(batch_id, new_quantities, lot_selections or None):
            # CHANGED: was url_for(..., err=...) — moved to flash()
            flash('Could not update — insufficient stock for new material quantities', 'error')
            return redirect(url_for('edit_batch', batch_id=batch_id))

    # ADDED: planned-deduction-mode feature — a deferred Planned batch's stored lot picks are sized
    # for its OLD quantity. promote_planned_batches validates each material's stored per-lot sum
    # against quantity_needed * quantity, so keeping stale picks would strand the batch as Planned
    # with a "don't match required" failure on its completion date. Clear them and make the user
    # re-pick (FIFO covers it if they don't).
    existing = get_batch_by_id(batch_id)
    quantity_changed = (
        existing is not None
        and existing[4] == 'Planned' and existing[11] == 'deferred'
        and quantity is not None and float(existing[2]) != quantity
    )

    result = update_batch(batch_id, product_name=product_name, quantity=quantity, notes=notes, expiration_date=expiration_date, planned_completion_date=planned_completion_date)

    if result:
        logging.info(f"Batch details updated: batch_id={batch_id}, product_name={product_name}, quantity={quantity}")
        log_action('batch_updated', f"batch_id={batch_id}, product_name={product_name}, quantity={quantity}")

        if quantity_changed:
            clear_planned_lot_selections(batch_id)
            log_action('planned_lots_cleared', f"batch_id={batch_id}, reason=quantity_changed")
            flash('Quantity changed — lot selections were cleared. Re-select lots, or the oldest lots (FIFO) will be used at the planned date.', 'warning')

        if new_quantities:
            adj_result = adjust_batch_material(batch_id, new_quantities, lot_selections or None)
            if adj_result:
                log_action('batch_materials_adjusted', f"batch_id={batch_id}, adjustments={new_quantities}")
                # CHANGED: was url_for(..., msg=...) — moved to flash()
                flash('Batch details and material deductions updated successfully', 'success')
                return redirect(url_for('edit_batch', batch_id=batch_id))
            else:
                flash('Batch details saved but failed to adjust material deductions — insufficient stock or material not found', 'error')
                return redirect(url_for('edit_batch', batch_id=batch_id))

        flash('Batch details updated successfully', 'success')
        return redirect(url_for('edit_batch', batch_id=batch_id))
    else:
        flash('Failed to update batch details', 'error')
        return redirect(url_for('edit_batch', batch_id=batch_id))


# ADDED: planned-deduction-mode feature — edit the lots a deferred Planned batch has RESERVED.
# No stock moves here: nothing was deducted, so this only rewrites the planned_lot_selections JSON
# that promote_planned_batches will replay on the completion date. Editing an already-deducted
# batch's lots is a different operation entirely — that's adjust_batch_material, above.
@app.route('/edit-batch/<int:batch_id>/planned-lots', methods=['POST'])
@requires_auth
def update_batch_planned_lots(batch_id):
    """
    Replaces the stored lot picks of a deferred Planned batch.
    Consumes the same `lot_selection` hidden JSON field the create-batch form posts:
    {material_id: [{"lot_id": int, "qty": float_in_base_units}, ...]}
    An empty selection clears the picks — promotion then falls back to FIFO.
    """
    raw = request.form.get('lot_selection', '').strip()
    lot_selections = None
    if raw:
        try:
            parsed = json.loads(raw)
            # Shape-check before it reaches the service: keys are material_ids, values are lists of
            # {lot_id, qty}. A malformed payload must be a 400, never a KeyError in the service.
            lot_selections = {}
            for material_id, lots in parsed.items():
                lot_selections[int(material_id)] = [
                    {'lot_id': int(lot['lot_id']), 'qty': float(lot['qty'])} for lot in lots
                ]
        except (ValueError, TypeError, KeyError, AttributeError):
            return render_template('error.html',
                title="Invalid Input",
                message="Invalid lot selection format.",
                back_link=True, back_link_url=f"/edit-batch/{batch_id}", back_link_label="Go back to Edit Batch"
            ), 400

    try:
        if update_planned_lot_selections(batch_id, lot_selections):
            flash('Planned lot selection updated', 'success')
        else:
            flash('Could not update the planned lot selection', 'error')
    except ValueError as e:
        # Don't surface raw service text — log it, show the one thing the user can act on.
        logging.warning(f"update_batch_planned_lots ValueError: batch_id={batch_id}: {e}")
        flash('Could not update — a lot you picked may have expired or been used up since you '
              'opened the page. Reload and try again.', 'error')

    return redirect(url_for('edit_batch', batch_id=batch_id))


@app.route('/edit-batch/<int:batch_id>/change-status', methods=['POST'])
@requires_auth
def change_batch_status(batch_id):
    """
    Change the status of a batch  
     
    This is a POST route that takes the new status from the form submission in the edit batch page, updates the batch status in the database, and redirects back to the edit batch page with a success or error message.
    """

    new_status = request.form.get('status') # Get the new status from the form submission (e.g., "ready", "shipped", etc.)

    
    if new_status == 'Planned': 
        planned_completion_date = request.form.get('planned_completion_date', '').strip() or None #get planned date of completion
        if not planned_completion_date:
            # CHANGED: was url_for(..., err=...) — moved to flash()
            flash('A planned completion date is required when setting status to Planned', 'error')
            return redirect(url_for('edit_batch', batch_id=batch_id))
        try:
            pcd = datetime.strptime(planned_completion_date, '%Y-%m-%d')
            # CHANGED: server-timezone-vs-business-timezone fix — was datetime.now().date().
            if pcd.date() < business_today():
                flash('Planned completion date cannot be in the past', 'error')
                return redirect(url_for('edit_batch', batch_id=batch_id))
        except ValueError:
            flash('Invalid planned completion date format', 'error')
            return redirect(url_for('edit_batch', batch_id=batch_id))
        update_batch(batch_id, planned_completion_date=planned_completion_date)

    # Planned -> Ready is a real promotion, not a label change: a deferred batch must deduct its
    # lots and record batch_materials (a mix also needs its output lot). Going through
    # update_batch_status here left such a batch Ready with nothing deducted.
    current = get_batch_by_id(batch_id)
    if new_status == 'Ready' and current and current[4] == 'Planned':
        result, failure_reason = promote_batch_now(batch_id)
        if not result:
            logging.warning(f"Manual promotion failed: batch_id={batch_id}, reason={failure_reason}")
            flash('Could not mark this batch Ready — there is not enough usable stock '
                  '(or a selected lot has expired). It stays Planned. Check the lot stock and try again.', 'error')
            if request.form.get('next') == 'batches':
                return redirect(url_for('view_batches'))
            return redirect(url_for('edit_batch', batch_id=batch_id))
    else:
        result = update_batch_status(batch_id, new_status)
    if result:
        logging.info(f"Batch status changed: batch_id={batch_id}, new_status={new_status}")
        log_action('batch_status_changed', f"batch_id={batch_id}, new_status={new_status}")
        # CHANGED: was url_for(..., msg=...) — moved to flash()
        flash(f'Batch status updated to {new_status}', 'success')
        # The Batches page's "Mark Ready" button sends next=batches so staff land back there, not on edit.
        if request.form.get('next') == 'batches':
            return redirect(url_for('view_batches'))
        return redirect(url_for('edit_batch', batch_id=batch_id))
    else:
        flash('Failed to update batch status', 'error')
        if request.form.get('next') == 'batches':
            return redirect(url_for('view_batches'))
        return redirect(url_for('edit_batch', batch_id=batch_id))
    

@app.route('/edit-batch/<int:batch_id>/delete', methods=['POST'])
@requires_auth
def delete_batch_route(batch_id):
    
    materials_to_reallocate = []

    if request.form.get('reallocate_all'):
        # Manage Batches deletes reallocate every material on the batch by default.
        # Build the list straight from batch_materials so no per-material form fields
        # are needed; the service reads the actual quantities back from the DB.
        materials_to_reallocate = get_batch_materials_for_reallocation(batch_id)
    else:
        checked_ids = request.form.getlist('reallocate_material')  #getlist() only returns values for checked boxes, uncheck ones are excluded.

        for mid in checked_ids: # for each material that was check, get id by just taking value of checkbox, quantity by
            #looking for form field with name qty_{material_id}, and material name by looking for form field with name matname_{material_id}.
            materials_to_reallocate.append({
                'material_id': int(mid),
                'quantity_used': float(request.form.get(f'qty_{mid}', 0)),
                'material_name': request.form.get(f'matname_{mid}', '')
            })
    

    



    result = delete_batch(batch_id, materials_to_reallocate, reallocate=bool(materials_to_reallocate))
    if result == "shipped":
        return render_template('error.html',
            title="Delete Failed",
            message="This batch can't be deleted because it's still part of a shipment. Remove it from the shipment first, then delete the batch.",
        ), 409
    if not result:
        return render_template('error.html',
            title="Delete Failed",
            message="Could not delete batch. The batch may not exist.",
        ), 500
    logging.info(f"Batch deleted: batch_id={batch_id}, reallocate={bool(materials_to_reallocate)}")
    log_action('batch_deleted', f"batch_id={batch_id}, reallocate={bool(materials_to_reallocate)}")
    return redirect(url_for('manage_batches'))






#================
# RECIPE PAGES
#=================
@app.route('/recipes')
@requires_auth
def view_recipes():
    """
    View All Recipes Page

    Displays recipes in a grouped format where:
    - Recipe name and notes appear only on the FIRST row of each recipe
    - Subsequent rows for the same recipe show only materials (name blank)
    - This makes it easy to see which materials belong to which recipe

    GET route
    """

    # ADDED: read page number from URL (?page=N); default to page 1
    PER_PAGE = 50
    page = min(max(1, int(request.args.get('page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # recipe-filter feature — narrow the list to one recipe kind. 'component' = mix recipes,
    # 'finished' = everything else, 'all' = no filter. Applied in-memory after grouping below.
    kind = (request.args.get('kind') or 'all').lower()
    if kind not in ('all', 'finished', 'component'):
        kind = 'all'
    # row-numbering feature — page offset so the # continues across pages.
    offset = (page - 1) * PER_PAGE

    # CHANGED: get_all_recipes now returns (df, total). Pagination is at the recipe level
    #          (never splits a recipe across pages). total = total number of distinct recipes.
    df, total = get_all_recipes(page=page, per_page=PER_PAGE)

    if df.empty:
        return render_template("recipes.html",
            recipes=[], recipe_count=0, kind=kind, offset=offset,
            page=1, total_pages=1, total=0,
            back_link=True, back_link_url="/", back_link_label="Back to Home"
    )

    # CHANGED: instead of blanking duplicate rows for a flat table, group the flat
    #          (one-row-per-ingredient) result into one dict per recipe so the template
    #          can render a collapsible accordion row per recipe.
    df_display = df.copy()
    # ADDED: recipes with no valid materials come back with NULL material/quantity/unit
    #        (LEFT JOIN). Blank them so we show empty cells / "No materials", not "nan"/"None".
    df_display = df_display.where(df_display.notna(), '')

    # Rows are ordered by recipe_product_name then material_name (see get_all_recipes),
    # so we can group sequentially. A recipe with no materials has a blank material_name —
    # skip that row when collecting materials, leaving an empty materials list.
    recipes = []
    by_name = {}
    for row in df_display.to_dict(orient='records'):
        name = row['recipe_product_name']
        recipe = by_name.get(name)
        if recipe is None:
            recipe = {
                'name': name,
                'product_unit': row['product_unit'],
                'notes': row['notes'],
                # batch-type-on-recipe feature — for the type badge in the template.
                'batch_type': row['batch_type'] or 'finished',
                'materials': [],
            }
            by_name[name] = recipe
            recipes.append(recipe)
        if row['material_name']:
            # ADDED: units-conversion-layer feature — quantity_needed is stored in grams;
            #        convert back to the line's entry unit for display.
            line_qty = row['quantity']
            line_unit = row['unit']
            if line_qty not in ('', None) and line_unit:
                # Keep the exact Decimal from from_base; fmt_num formats it in the template.
                # (No float() — that round-trip is what re-introduced the display spew.)
                line_qty = units.from_base(line_qty, line_unit)
            recipe['materials'].append({
                'material_name': row['material_name'],
                'quantity': line_qty,
                'unit': line_unit,
            })

    # recipe-filter feature — apply the kind filter to the grouped recipes for this page.
    if kind == 'component':
        recipes = [r for r in recipes if r['batch_type'] == 'mix']
    elif kind == 'finished':
        recipes = [r for r in recipes if r['batch_type'] != 'mix']

    # CHANGED: recipe_count now comes from the total returned by the service (all recipes),
    #          not nunique() on the current page (which would undercount when paginated).
    recipe_count = total

    # ADDED: total_pages for pagination controls in the template
    total_pages = math.ceil(total / PER_PAGE) if total else 1

    return render_template("recipes.html",
        recipes=recipes,
        recipe_count=recipe_count,
        kind=kind,
        offset=offset,
        page=page,
        total_pages=total_pages,
        total=total,
        back_link=True, back_link_url="/", back_link_label="Back to Home"
    )



# NEW: Excel export route for recipes
@app.route('/export/recipes-excel')
@requires_auth
def export_recipes_excel():
    """
    Export all recipes and their materials to Excel file
 

    """
    # CHANGED: get_all_recipes now returns (df, total). page=None skips LIMIT/OFFSET
    #          so the export still contains every recipe row, not just one page.
    df, _ = get_all_recipes(page=None)

    if df.empty:
        return "No data to export", 400  # No recipes to export

    # Create Excel file (e.g., recipes_20260203_143022.xlsx)
    filepath = export_to_excel(df, 'recipes')

    # Trigger browser download
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))






# ADDED: units-conversion-layer feature — shared recipe-line converter used by both the
#        add- and edit-recipe routes. Returns (quantity_in_base_unit, stored_unit):
#          - mass material: convert `quantity` from `line_unit` (default 'g') to grams;
#            stored_unit is the entry unit.
#          - count material: amount is a plain count (identity); stored_unit is the
#            material's own unit label, `line_unit` is ignored.
#        Raises ValueError if the material is unknown or a mass unit is unrecognized.
def recipe_qty_to_base(material_name, quantity, line_unit):
    row = get_unit_and_dimension(material_name)
    if row is None:
        raise ValueError(f"material '{material_name}' is not in inventory; add it first")
    mat_unit, dimension = row[0], row[1]
    if dimension == 'count':
        return float(quantity), mat_unit
    # mass material
    entry_unit = (line_unit or 'g').strip()
    if units.dimension_of(entry_unit) != 'mass':
        raise ValueError(f"'{entry_unit}' is not a recognized weight unit")
    return float(units.to_base(quantity, entry_unit)), entry_unit


# ADDED: units-conversion-layer feature — validate the recipe's product dimension the same way
#        the add-material route validates a material's dimension. Returns an error message
#        string for the user, or None when valid. Mass products must use a recognized weight
#        unit (so the housemade material they produce is convertible); count products are free.
def _validate_product_dimension(product_dimension, product_unit):
    if product_dimension not in ("mass", "count"):
        return "Please choose whether the product is tracked by Weight or Count."
    if product_dimension == "mass" and units.dimension_of(product_unit) != "mass":
        return (f"'{product_unit}' is not a recognized weight unit. "
                f"Use one of: {', '.join(units.MASS_UNITS)}.")
    return None


@app.route('/add-recipe', methods=['GET', 'POST'])
@requires_auth
def add_recipe_route():
    """
    Add Recipe Route - Handles both displaying the form and processing submissions

    FORM STRUCTURE:
    - Product Name: The name of the recipe/product being created
    - Notes: Optional notes about the recipe (e.g., "sweetened", "vegan")
    - Materials: Dynamic list of materials, each with:
        - Material Name: What ingredient is needed
        - Quantity Needed: How much of that material per batch

    DATA FLOW:
    1. User fills out form with product name and adds materials
    2. JavaScript allows adding/removing material rows dynamically
    3. On submit, form data is sent as POST request
    4. Backend parses indexed fields (material_name_0, quantity_0, etc.)
    5. Builds materials list and calls add_recipe() from inventory_app.py
    6. Redirects to recipes page on success

    FORM FIELD NAMING CONVENTION:
    - Material rows use indexed names: material_name_0, quantity_0, material_name_1, etc.
    - This allows parsing an arbitrary number of materials on the backend

   
    COMPLEXITY NOTE:
    Unlike simple forms (like add-material), recipes require MULTIPLE materials.
    We use JavaScript on the frontend to dynamically add/remove material rows,
    then parse them on the backend using indexed form field names (material_name_0, etc.)

    """

    if request.method == 'POST':

        
        product_name = request.form.get('product_name')

        product_name = product_name.strip() if product_name else None # cant be none

        notes = request.form.get('notes')#can be none

        notes = notes.strip() if notes else None

        product_unit = request.form.get('product_unit')  # required — what one unit of the product is

        product_unit = product_unit.strip() if product_unit else None

        # ADDED: units-conversion-layer feature — explicit Weight/Count for the product itself,
        #        mirroring the add-material form. Declares (not guesses) the dimension a
        #        mix/component batch will give the housemade material it produces.
        product_dimension = (request.form.get('product_dimension') or '').strip()

        # ADDED: batch-type-on-recipe feature — the type of batch this recipe produces.
        batch_type = (request.form.get('batch_type') or 'finished').strip()

        if not product_name:
            return render_template('error.html',
                title="Invalid Input",
                message="Product name cannot be blank.",
                back_link=True, back_link_url="/add-recipe", back_link_label="Go back"
                 ), 400

        if batch_type not in ('mix', 'finished'):
            return render_template('error.html',
                title="Invalid Input",
                message="Invalid batch type. Must be Finished or Component.",
                back_link=True, back_link_url="/add-recipe", back_link_label="Go back"
                 ), 400

        if not product_unit:
            return render_template('error.html',
                title="Invalid Input",
                message="Product unit of measurement cannot be blank.",
                back_link=True, back_link_url="/add-recipe", back_link_label="Go back"
                 ), 400

        dim_error = _validate_product_dimension(product_dimension, product_unit)
        if dim_error:
            return render_template('error.html',
                title="Invalid Input",
                message=dim_error,
                back_link=True, back_link_url="/add-recipe", back_link_label="Go back"
                 ), 400
    


        #  Parse dynamic material fields
      
        # Materials are submitted with indexed field names:
        # material_name_0, quantity_0, material_name_1, quantity_1, etc.
        #
        # We iterate through indices until we stop finding fields
        # This allows for any number of materials to be added

        materials = []  # Will hold list of material dictionaries
        index = 0       # Start at first material (index 0)

        # Loop through all submitted material rows
        while True:
            # Try to get material at current index
            material_name = request.form.get(f'material_name_{index}')
            quantity_str = request.form.get(f'quantity_{index}')
            # ADDED: units-conversion-layer feature — per-line entry unit (mass dropdown; absent
            #        for count materials).
            line_unit = request.form.get(f'unit_{index}')

            # If no material name found at this index, we've processed all materials
            if not material_name:
                break

            # proccess the quantiy (convert to float)and material name, and add to materials list if valid
            if material_name.strip() and quantity_str:
                try:
                    # Convert quantity to float for decimal support
                    quantity = float(quantity_str)
                    # ADDED: units-conversion-layer feature — convert the entered amount to the
                    #        material's stored base unit (grams for mass via the line's unit;
                    #        identity for counts). Raises ValueError on an unrecognized unit.
                    qn, stored_unit = recipe_qty_to_base(material_name.strip(), quantity, line_unit)
                except ValueError as e:
                    return render_template('error.html',
                        title="Invalid Input",
                        message=f"Quantity for material '{material_name}': {e}",
                        back_link=True, back_link_url="/add-recipe", back_link_label="Go back and fix it"
                    ), 400

                # Add material to our list in the format expected by add_recipe()
                materials.append({
                    'material_name': material_name.strip(),
                    'quantity_needed': qn,
                    'unit': stored_unit,
                })

            # Move to next index
            index += 1

        

        #Validate required data
        # Check that we have both a product name and at least one material
        if not product_name or not materials:
            return render_template('error.html',
                title="Missing Required Fields",
                message="Please provide a product name and at least one material.",
                back_link=True, back_link_url="/add-recipe", back_link_label="Go back and try again"
            ), 400

        


        #  save recipe
        
        try:
            
            result = add_recipe(product_name, materials, notes, product_unit=product_unit, product_dimension=product_dimension, batch_type=batch_type)
            # materials is the list of dictionaries we build from form data. 
            if result:
                #success
                logging.info(f"Recipe added: '{product_name}' with {len(materials)} materials")
                log_action('recipe_added', f"product_name={product_name}, materials={len(materials)}")
                return redirect(url_for('view_recipes'))
            else:
                
                return render_template('error.html',
                    title="Error Adding Recipe",
                    message="Failed to add recipe. Please check that all materials exist in inventory.",
                    back_link=True, back_link_url="/add-recipe", back_link_label="Go back and try again"
                ), 500

        except Exception as e:
            logging.error(f"add_recipe_route: {e}", exc_info=True)
            return render_template('error.html',
                title="Error Adding Recipe",
                message="An unexpected error occurred. Please try again.",
                back_link=True, back_link_url="/add-recipe", back_link_label="Go back and try again"
            ), 500

   
   
   #GET handeling. 

    return render_template("add_recipe.html",
    back_link=True,
    back_link_url="/",
    back_link_label="Back to Home"
)



#==============
#RECIPE MANAGEMENT PAGES (edit/delete recipes)
#==============

@app.route('/manage-recipes') # Page to view all recipes with edit/delete options
@requires_auth
def manage_recipes():
    """
    This is a page to view all recipes with edit/delete options.
    simple GET, just viewing, but with buttons. 
    """
    # ADDED: pagination for manage-recipes page
    PER_PAGE = 50
    page = min(max(1, int(request.args.get('page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # CHANGED: get_all_recipes_with_id now returns (df, total) tuple
    df, total = get_all_recipes_with_id(page=page, per_page=PER_PAGE)

    recipes = df.to_dict(orient='records') if (df is not None and not df.empty) else []
    # ADDED: total_pages for pagination controls in template
    total_pages = math.ceil(total / PER_PAGE) if total else 1

    return render_template("manage_recipes.html",
        recipes=recipes,
        count=len(recipes),
        page=page,
        total_pages=total_pages,
        total=total,
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
)

@app.route('/edit-recipe/<int:recipe_id>') # dynamic URL for editing a specific recipe, identified by recipe_id.
@requires_auth
def edit_recipe(recipe_id):
    """
    view edit recipe page, with form to edit recipe details and materials. 
    this is from manage recipes page, 
    where each recipe has an edit button that takes you to this page with the recipe_id in the URL. 
    this page will show a form with current recipe details filled in, and allow editing of both the recipe details 
    (name, notes) and the materials (add/remove materials, change quantities).
    
    GET page route
    """

    df = get_recipe_by_id(recipe_id) #get recipe details for the recipe being edited

    if df is None or df.empty:
        return render_template('error.html',
            title="Recipe Not Found",
            message="The recipe you requested could not be found.",
            back_link=True, back_link_url="/manage-recipes", back_link_label="Back to Manage Recipes"
        ), 404

    # Extract recipe-level info from first row (same for all rows)
    recipe_id_val = df['recipe_id'].iloc[0]
    product_name = df['product_name'].iloc[0]
    notes = df['notes'].iloc[0]
    product_unit = df['product_unit'].iloc[0]
    # ADDED: units-conversion-layer feature — declared Weight/Count for the product. Legacy
    #        recipes have NULL here; infer from the unit so the form pre-selects sensibly.
    product_dimension = df['product_dimension'].iloc[0]
    if not product_dimension:
        product_dimension = units.dimension_of(product_unit) if product_unit else 'mass'
    # ADDED: batch-type-on-recipe feature — pre-select the type on the edit form. Legacy recipes
    #        with NULL default to 'finished'.
    batch_type = df['batch_type'].iloc[0] or 'finished'

    # Extract materials list — one dict per row.
    # ADDED: a recipe with no materials comes back from the LEFT JOIN as a single all-NULL
    #        placeholder row. Drop rows without a material name so the form doesn't render a
    #        "None"/"nan" input; the user can add materials with the "+ Add Material" button.
    materials_df = df[df['material_name'].notna()]
    materials = materials_df[['material_name', 'quantity_needed', 'unit']].to_dict(orient='records') # html friendly format.

    # ADDED: units-conversion-layer feature — quantity_needed is stored in the material's base
    #        unit (grams for mass). Convert it back to the unit the line was entered in (`unit`)
    #        so the edit form shows the original number; default to grams if no unit recorded.
    for m in materials:
        entry_unit = m.get('unit') or 'g'
        m['unit'] = entry_unit
        if m.get('quantity_needed') is not None:
            # Keep the exact Decimal (no float() round-trip); the edit form runs it through
            # fmt_num so a long division tail shows the clean original value.
            m['quantity_needed'] = units.from_base(m['quantity_needed'], entry_unit)

    msg = request.args.get('msg', '')
    err = request.args.get('err', '')

    return render_template("edit_recipe.html",
        recipe_id=recipe_id_val,
        product_name=product_name,
        notes=notes,
        product_unit=product_unit,
        product_dimension=product_dimension,
        batch_type=batch_type,
        materials=materials,
        msg=msg,
        err=err,
        back_link=True,
        back_link_url="/manage-recipes",
        back_link_label="Back to Manage Recipes"
    )



    
@app.route('/edit-recipe/<int:recipe_id>/update', methods=['POST']) # POST route for processing the edit recipe form submission, where recipe_id identifies which recipe to update.
@requires_auth
def update_recipe_route(recipe_id):
    """
    updates the recipe details and materials based on the form submission
    from the edit recipe page.

    POST route for processing edit recipe submission. 
    """
    try:
        
        notes = request.form.get('notes') 

        notes = notes.strip() if notes else None

        product_name = request.form.get('product_name')

        product_name = product_name.strip() if product_name else None

        product_unit = request.form.get('product_unit')  # required — what one unit of the product is

        product_unit = product_unit.strip() if product_unit else None

        # ADDED: units-conversion-layer feature — explicit Weight/Count for the product itself.
        product_dimension = (request.form.get('product_dimension') or '').strip()

        # ADDED: batch-type-on-recipe feature — the type of batch this recipe produces.
        batch_type = (request.form.get('batch_type') or 'finished').strip()
        if batch_type not in ('mix', 'finished'):
            return render_template('error.html',
                title="Invalid Input",
                message="Invalid batch type. Must be Finished or Component.",
                back_link=True, back_link_url=f"/edit-recipe/{recipe_id}", back_link_label="Go back"
            ), 400

        materials = [] # materials come in as indexed fields, so we have to parse same as recipe
        index = 0
        while True:
            material_name = request.form.get(f'material_name_{index}')
            quantity_str = request.form.get(f'quantity_{index}')
            line_unit = request.form.get(f'unit_{index}')  # ADDED: units-conversion-layer feature
            if not material_name: #loop until out of material name
                break
            if material_name.strip() and quantity_str: # if we have a material name and quantity, add to materials list
                try:
                    # ADDED: units-conversion-layer feature — convert each line to the material's
                    #        base unit (grams for mass via the line unit; identity for counts).
                    qn, stored_unit = recipe_qty_to_base(material_name.strip(), float(quantity_str), line_unit)
                    materials.append({ #as a dictionary.
                        'material_name': material_name.strip(),
                        'quantity_needed': qn,
                        'unit': stored_unit,
                    })
                except ValueError as e:
                    return render_template('error.html',
                        title="Invalid Input",
                        message=f"Quantity for '{material_name}': {e}",
                        back_link=True, back_link_url=f"/edit-recipe/{recipe_id}", back_link_label="Go back"
                    ), 400
            index += 1

    except ValueError:
        return render_template('error.html',
            title="Invalid Input",
            message="Quantity must be a valid number",
            back_link=True, back_link_url=f"/edit-recipe/{recipe_id}", back_link_label="Go back to Edit Recipe"
        ), 400
    
    if not product_name:
        return render_template('error.html',
            title="Invalid Input",
            message="Product name cannot be blank.",
            back_link=True, back_link_url=f"/edit-recipe/{recipe_id}", back_link_label="Go back"
        ), 400

    if not product_unit:
        return render_template('error.html',
            title="Invalid Input",
            message="Product unit of measurement cannot be blank.",
            back_link=True, back_link_url=f"/edit-recipe/{recipe_id}", back_link_label="Go back"
        ), 400

    dim_error = _validate_product_dimension(product_dimension, product_unit)
    if dim_error:
        return render_template('error.html',
            title="Invalid Input",
            message=dim_error,
            back_link=True, back_link_url=f"/edit-recipe/{recipe_id}", back_link_label="Go back"
        ), 400

    if not materials:
        return render_template('error.html',
            title="Invalid Input",
            message="Recipe must have at least one material.",
            back_link=True, back_link_url=f"/edit-recipe/{recipe_id}", back_link_label="Go back"
        ), 400
    
    result = update_recipe(recipe_id, product_name=product_name, notes=notes, materials=materials, product_unit=product_unit, product_dimension=product_dimension, batch_type=batch_type)
    

    if result:
        logging.info(f"Recipe updated: recipe_id={recipe_id}, product_name={product_name}")
        log_action('recipe_updated', f"recipe_id={recipe_id}, product_name={product_name}")
        # CHANGED: was url_for(..., msg=...) — moved to flash()
        flash('Recipe details updated successfully', 'success')
        return redirect(url_for('edit_recipe', recipe_id=recipe_id))
    else:
        flash('Failed to update recipe details', 'error')
        return redirect(url_for('edit_recipe', recipe_id=recipe_id))



@app.route('/edit-recipe/<int:recipe_id>/delete', methods=['POST'])
@requires_auth
def delete_recipe_route(recipe_id):
    """
    simply deletes the recipe from database,
    POST route
    """
    
    result = delete_recipe_by_id(recipe_id)
    if not result:
        return render_template('error.html',
            title="Delete Failed",
            message="Could not delete recipe. Please try again.",
        ), 500
    logging.info(f"Recipe deleted: recipe_id={recipe_id}")
    log_action('recipe_deleted', f"recipe_id={recipe_id}")
    return redirect(url_for('manage_recipes'))





#==================
#LOT PAGES
#==================

@app.route('/manage-lots') # Page to view all lots with edit/delete options (view only)
@requires_auth
def manage_lots():
    """
    This is a page to view all lots with edit/delete options.
    simple GET, just viewing, but with buttons. 
    """
    # ADDED: pagination for manage-lots page
    PER_PAGE = 50
    page = min(max(1, int(request.args.get('page', 1))), MAX_PAGE)  # CHANGED: capped at MAX_PAGE to prevent large-OFFSET DoS

    # CHANGED: get_all_lots now returns (df, total) tuple
    df, total = get_all_lots(page=page, per_page=PER_PAGE)

    lots = df.to_dict(orient='records') if (df is not None and not df.empty) else []
    # ADDED: total_pages for pagination controls in template
    total_pages = math.ceil(total / PER_PAGE) if total else 1

    return render_template("manage_lots.html",
        lots=lots,
        count=len(lots),
        page=page,
        total_pages=total_pages,
        total=total,
        back_link=True,
        back_link_url="/",
        back_link_label="Back to Home"
)





@app.route('/edit-lot/<int:lot_id>') # dynamic URL for editing a specific lot, identified by lot_id. (view only)
@requires_auth
def edit_lot(lot_id):
    """
    view lot details page, with form to edit lot details. 
    this is from manage lots page, 
    where each lot has an edit button that takes you to this page with the lot_id in the URL. 
    this page will show lot details, but no editing allowed for now since lot management is more complex and we want to avoid accidental stock changes. 

    GET page route
    """

    df = get_lot_by_id(lot_id) #get lot details for the lot being viewed

    
    
    if df is None or df.empty:
        return render_template('error.html',
            title="Lot Not Found",
            message="The lot you requested could not be found.",
            back_link=True, back_link_url="/manage-lots", back_link_label="Back to Manage Lots"
        ), 404
    
    lot = df.to_dict(orient='records') if (df is not None and not df.empty) else [] # convert to dictionary for HTML display, like usual.

    # ADDED: units-conversion-layer feature — the lot stores quantity in the base unit (grams
    #        for mass) and cost per base unit; convert both back to the material's display unit
    #        so the edit form shows the values the user originally entered.
    if lot:
        lot_unit = lot[0].get('unit')
        if lot[0].get('quantity') is not None:
            lot[0]['quantity'] = float(units.from_base(lot[0]['quantity'], lot_unit))
        if lot[0].get('cost_per_unit') is not None:
            # Cost is stored as $/base-unit and converts the OPPOSITE way to quantity:
            # display $/unit -> stored $/g via from_base (divide) on the write path, so
            # stored -> display must MULTIPLY by the factor, i.e. to_base. (Using from_base
            # here was the bug that rendered e.g. $50/lb as ~$0.0002/lb.)
            lot[0]['cost_per_unit'] = float(units.to_base(lot[0]['cost_per_unit'], lot_unit))

    lot_batches = get_batches_for_lot(lot_id)

    return render_template("edit_lot.html",
        lot=lot,
        lot_batches=lot_batches,
        back_link=True,
        back_link_url="/manage-lots",
        back_link_label="Back to Manage Lots"
    )


@app.route('/edit-lot/<int:lot_id>/update', methods=['POST']) # dynamic URL for editing a specific lot, identified by lot_id. (view only)
@requires_auth
def update_lot_route(lot_id):
    """

    calls update_lot function. 
    POST route for processing edit lot submission.
    takes user requests: lot number, quantity (if not homemade mat), expiration date, location, supplier, cost per unit, 
    status. 

    """
    
    try:




        #see if quantity field needs to be removed
        lot_df = get_lot_by_id(lot_id)
        if lot_df is None or lot_df.empty:
            return render_template('error.html',
                title="Lot Not Found",
                message="The lot you are trying to update could not be found.",
                back_link=True, back_link_url=f"/edit-lot/{lot_id}", back_link_label="Go back to Edit Lot"
            ), 404
        
        


        lot_number_raw = request.form.get('lot_number')
        lot_number = lot_number_raw.strip() if lot_number_raw else None

        quantity_str = request.form.get('quantity', '').strip()
        quantity = float(quantity_str) if quantity_str else None

        expiration_date_raw = request.form.get('expiration_date')
        expiration_date = expiration_date_raw.strip() if expiration_date_raw else None

        location_raw = request.form.get('location')
        location = location_raw.strip() if location_raw else None

        supplier_raw = request.form.get('supplier')
        supplier = supplier_raw.strip() if supplier_raw else None


        cost_per_unit_str = request.form.get('cost_per_unit', '').strip()
        cost_per_unit = float(cost_per_unit_str) if cost_per_unit_str else None

        
        if lot_df['is_housemade'].iloc[0]: # if this is a housemade material,
            quantity = None

        #validate float inputs
        if quantity is not None and quantity <= 0:
            return render_template('error.html',
                title="Invalid Input",
                message="Quantity must be greater than zero.",
                back_link=True, back_link_url=f"/edit-lot/{lot_id}", back_link_label="Go back to Edit Lot"
            ), 400
        
        if cost_per_unit is not None and cost_per_unit < 0:
            return render_template('error.html',
                title="Invalid Input",
                message="Cost per unit cannot be negative.",
                back_link=True, back_link_url=f"/edit-lot/{lot_id}", back_link_label="Go back to Edit Lot"
            ), 400
        
        #validate date input
        if expiration_date:
            try:
                datetime.strptime(expiration_date, '%Y-%m-%d')
            except ValueError:
                return render_template('error.html',
                    title="Invalid Input",
                    message="Invalid expiration date format. Use YYYY-MM-DD.",
                    back_link=True, back_link_url=f"/edit-lot/{lot_id}", back_link_label="Go back to Edit Lot"
                ), 400
            
        # ADDED: units-conversion-layer feature — quantity and cost are entered in the
        #        material's display unit; store them in the base unit (grams for mass).
        #        quantity -> to_base; cost-per-display-unit -> cost-per-gram via from_base.
        lot_unit = lot_df['unit'].iloc[0]
        if quantity is not None:
            quantity = float(units.to_base(quantity, lot_unit))
        if cost_per_unit is not None:
            cost_per_unit = float(units.from_base(cost_per_unit, lot_unit))

        result = update_lot(lot_id, lot_number=lot_number, quantity=quantity, expiration_date=expiration_date, location=location, cost_per_unit=cost_per_unit, supplier=supplier)

        
        


        if result:
            logging.info(f"Lot updated: lot_id={lot_id}, lot_number={lot_number}, quantity={quantity}")
            log_action('lot_updated', f"lot_id={lot_id}, lot_number={lot_number}, quantity={quantity}")
            # CHANGED: was url_for(..., msg=...) — moved to flash()
            flash('Lot details updated successfully', 'success')
            return redirect(url_for('edit_lot', lot_id=lot_id))
        else:
            flash('Failed to update lot details', 'error')
            return redirect(url_for('edit_lot', lot_id=lot_id))


    
    except ValueError as e:
            logging.error(f"update_lot_route: {e}", exc_info=True)
            return render_template('error.html',
                title="Invalid Input",
                message="Please ensure all fields are filled out correctly.",
                back_link=True, back_link_url=f"/edit-lot/{lot_id}", back_link_label="Go back to Edit Lot"
            ), 400
        

        
    
    




@app.route('/edit-lot/<int:lot_id>/delete', methods=['POST'])
@requires_auth
def delete_lot_route(lot_id):
    """
    POST route to permanently delete a lot.
    """
    lot_df = get_lot_by_id(lot_id)
    if lot_df is None or lot_df.empty:
        return render_template('error.html',
            title="Lot Not Found",
            message="The lot you are trying to delete could not be found.",
            back_link=True, back_link_url="/manage-lots", back_link_label="Back to Manage Lots"
        ), 404

    lot_number = lot_df['lot_number'].iloc[0]

    # Block deletion of a lot still used by any batch — preserves batch→lot traceability
    # and avoids the SQLite/Postgres divergence (SQLite would silently orphan the reference,
    # Postgres would reject it with a generic FK error).
    lot_batches = get_batches_for_lot(lot_id)
    if lot_batches:
        count = len(lot_batches)
        flash(f"Cannot delete lot — it is used by {count} batch(es). "
              f"Remove or reallocate those batches first.", 'error')
        return redirect(url_for('edit_lot', lot_id=lot_id))

    result = delete_lot(lot_id)

    if result:
        log_action('lot_deleted', f"lot_id={lot_id}, lot_number={lot_number}")
        # CHANGED: was url_for(..., msg=...) — moved to flash()
        flash('Lot deleted successfully', 'success')
        return redirect(url_for('manage_lots'))
    else:
        flash('Failed to delete lot', 'error')
        return redirect(url_for('edit_lot', lot_id=lot_id))


# ========================
# SHIPMENTS ROUTES
# ========================

@app.route('/shipments')
@requires_auth
def shipments_list():
    page = request.args.get('page', 1, type=int)
    per_page = 50
    df, total = get_all_shipments(page=page, per_page=per_page)
    total_pages = math.ceil(total / per_page) if total else 1
    shipments = df.to_dict('records') if not df.empty else []
    return render_template('shipments.html',
        shipments=shipments,
        # row-numbering feature — page offset so the # column continues across pages.
        offset=(page - 1) * per_page,
        page=page,
        total_pages=total_pages,
        total=total or 0,
    )


@app.route('/shipments/new')
@requires_auth
def new_shipment_page():
    df, _ = get_batches(page=None)
    batches = df.to_dict('records') if not df.empty else []
    return render_template('create_shipment.html', batches=batches, today=business_today())


@app.route('/shipments/<int:shipment_id>')
@requires_auth
def shipment_detail(shipment_id):
    data = get_shipment_by_id(shipment_id)
    if not data:
        return render_template('error.html',
            title="Shipment Not Found",
            message=f"Shipment {shipment_id} does not exist.",
            back_link=True, back_link_url="/shipments", back_link_label="Back to Shipments"
        ), 404
    return render_template('shipment_detail.html',
        shipment=data['shipment'],
        batches=data['batches'],
    )


@app.route('/shipments/create', methods=['POST'])
@requires_auth
def create_shipment_route():
    raw_ids = request.form.getlist('batch_ids')
    if not raw_ids:
        return render_template('error.html',
            title="No Batches Selected",
            message="Please select at least one batch to create a shipment.",
            back_link=True, back_link_url="/batches", back_link_label="Back to Batches"
        ), 400

    # Each checked batch carries a qty_<batch_id> field — the amount of that batch to ship.
    try:
        lines = {}
        for bid in raw_ids:
            batch_id = int(bid)
            qty_raw = request.form.get(f'qty_{bid}', '').strip()
            if not qty_raw:
                raise ValueError(f"Enter a quantity for every selected batch (batch {batch_id} was blank).")
            qty = float(qty_raw)
            if qty <= 0:
                raise ValueError(f"Quantity for batch {batch_id} must be greater than zero.")
            lines[batch_id] = qty
    except ValueError as e:
        return render_template('error.html',
            title="Invalid Input",
            message=str(e),
            back_link=True, back_link_url="/shipments/new", back_link_label="Back to New Shipment"
        ), 400

    destination = request.form.get('destination', '').strip() or None
    notes = request.form.get('notes', '').strip() or None
    category = request.form.get('category', '').strip() or None
    date_shipped = request.form.get('date_shipped', '').strip()
    if not date_shipped:
        return render_template('error.html',
            title="Invalid Input",
            message="Date shipped cannot be blank.",
            back_link=True, back_link_url="/shipments/new", back_link_label="Back to New Shipment"
        ), 400
    try:
        datetime.strptime(date_shipped, '%Y-%m-%d')
    except ValueError:
        return render_template('error.html',
            title="Invalid Input",
            message="Date shipped must be a valid date.",
            back_link=True, back_link_url="/shipments/new", back_link_label="Back to New Shipment"
        ), 400

    try:
        shipment_id = create_shipment(lines, destination=destination, notes=notes,
                                       category=category, date_shipped=date_shipped)
    except ValueError as e:
        return render_template('error.html',
            title="Cannot Create Shipment",
            message=str(e),
            back_link=True, back_link_url="/batches", back_link_label="Back to Batches"
        ), 400

    if shipment_id is None:
        return render_template('error.html',
            title="Error",
            message="Failed to create shipment. Check logs for details.",
            back_link=True, back_link_url="/batches", back_link_label="Back to Batches"
        ), 500

    return redirect(url_for('shipment_detail', shipment_id=shipment_id))


@app.route('/manage-shipments')
@requires_auth
def manage_shipments():
    df, _ = get_all_shipments(page=None)
    shipments = df.to_dict('records') if not df.empty else []
    return render_template('manage_shipments.html', shipments=shipments)


@app.route('/edit-shipment/<int:shipment_id>')
@requires_auth
def edit_shipment(shipment_id):
    data = get_shipment_by_id(shipment_id)
    if not data:
        return render_template('error.html',
            title="Shipment Not Found",
            message=f"Shipment {shipment_id} does not exist.",
            back_link=True, back_link_url="/manage-shipments", back_link_label="Back to Manage Shipments"
        ), 404

    # Build the line-editor rows: every batch currently on this shipment, plus any other batch with
    # remaining quantity that could be added. max_qty is a UI hint — the service is the real guard.
    candidates = {}
    for b in data['batches']:
        candidates[b['batch_id']] = {
            'batch_id': b['batch_id'],
            'batch_number': b['batch_number'],
            'product_name': b['product_name'],
            'current_qty': b['quantity'],
            'max_qty': b['batch_quantity'],
        }
# make dic of shipment batch details. 
    # This will be used to populate the line editor in the edit_shipment template.

    pdf, _ = get_batches(page=None) # Get all batches for the line editor
    for b in (pdf.to_dict('records') if not pdf.empty else []):
        bid = b['batch_id']
        if bid in candidates:
            # picker remaining already subtracts this shipment's allocation → add it back for the cap
            candidates[bid]['max_qty'] = (candidates[bid]['current_qty'] or 0) + (b['remaining'] or 0)
        elif (b['remaining'] or 0) > 0:
            # Only offer batches that still have unshipped quantity. Fully-shipped batches
            # (remaining 0/None) that aren't already on this shipment are not addable.
            candidates[bid] = {
                'batch_id': bid,
                'batch_number': b['batch_number'],
                'product_name': b['product_name'],
                'current_qty': 0,
                'max_qty': b['remaining'],
            }

    # Split shipment_number back into its YYMMDD date + counter to prefill the edit form.
    # Old pre-migration numbers (e.g. "SHP-2026-07-15-001") don't fit this shape — fall back to
    # the shipment's stored date and counter 1; saving then adopts the new format.
    shipment_number = data['shipment']['shipment_number'] or ''
    if shipment_number.isdigit() and len(shipment_number) > 6:
        number_date = f"20{shipment_number[0:2]}-{shipment_number[2:4]}-{shipment_number[4:6]}"
        number_counter = shipment_number[6:]
    else:
        number_date = data['shipment']['date_shipped'] or ''
        number_counter = '1'

    return render_template('edit_shipment.html',
        shipment=data['shipment'],
        candidates=list(candidates.values()),
        number_date=number_date,
        number_counter=number_counter,
    )


@app.route('/edit-shipment/<int:shipment_id>/update', methods=['POST'])
@requires_auth
def update_shipment_route(shipment_id):
    destination = request.form.get('destination', '').strip() or None
    notes = request.form.get('notes', '').strip() or None
    category = request.form.get('category', '').strip() or None
    date_shipped = request.form.get('date_shipped', '').strip()
    counter_str = request.form.get('counter', '').strip()

    if not date_shipped:
        return render_template('error.html',
            title="Invalid Input",
            message="Date shipped cannot be blank.",
            back_link=True, back_link_url=f"/edit-shipment/{shipment_id}", back_link_label="Back to Edit Shipment"
        ), 400
    try:
        prefix = datetime.strptime(date_shipped, '%Y-%m-%d').strftime('%y%m%d')
    except ValueError:
        return render_template('error.html',
            title="Invalid Input",
            message="Date shipped must be a valid date.",
            back_link=True, back_link_url=f"/edit-shipment/{shipment_id}", back_link_label="Back to Edit Shipment"
        ), 400
    if not counter_str.isdigit() or int(counter_str) <= 0:
        return render_template('error.html',
            title="Invalid Input",
            message="Sequence # must be a positive whole number.",
            back_link=True, back_link_url=f"/edit-shipment/{shipment_id}", back_link_label="Back to Edit Shipment"
        ), 400
    shipment_number = f"{prefix}{int(counter_str)}"

    # Collect the batch lines from qty_<batch_id> fields; blanks/zeros drop the line.
    try:
        lines = {}
        for key, val in request.form.items():
            if not key.startswith('qty_'):
                continue
            val = val.strip()
            if not val:
                continue
            qty = float(val)
            if qty > 0:
                lines[int(key[4:])] = qty
    except ValueError:
        return render_template('error.html',
            title="Invalid Input",
            message="One or more quantities were not valid numbers.",
            back_link=True, back_link_url=f"/edit-shipment/{shipment_id}", back_link_label="Back to Edit Shipment"
        ), 400

    try:
        update_shipment(shipment_id, destination=destination, notes=notes, category=category,
                         date_shipped=date_shipped, shipment_number=shipment_number, lines=lines)
    except ValueError as e:
        return render_template('error.html',
            title="Cannot Update Shipment",
            message=str(e),
            back_link=True, back_link_url=f"/edit-shipment/{shipment_id}", back_link_label="Back to Edit Shipment"
        ), 400
    flash('Shipment updated successfully', 'success')
    return redirect(url_for('manage_shipments'))


@app.route('/shipments/<int:shipment_id>/delete', methods=['POST'])
@requires_auth
def delete_shipment_route(shipment_id):
    try:
        delete_shipment(shipment_id)
    except ValueError as e:
        return render_template('error.html',
            title="Shipment Not Found",
            message=str(e),
            back_link=True, back_link_url="/manage-shipments", back_link_label="Back to Manage Shipments"
        ), 404
    flash('Shipment deleted — batch quantities returned to available stock', 'success')
    return redirect(url_for('manage_shipments'))


@app.route('/export/shipment-manifest/<int:shipment_id>')
@requires_auth
def export_shipment_manifest(shipment_id):
    import pandas as pd
    data = get_shipment_by_id(shipment_id)
    if not data:
        return "Shipment not found", 404
    if not data['batches']:
        return "No batches in this shipment to export", 400
    # One workbook, two tabs: batch-level manifest + lot-level materials traceability.
    # (export_multi_sheet_to_excel skips the materials sheet if it comes back empty.)
    batches_df = pd.DataFrame(data['batches'])
    materials_df = get_shipment_materials(shipment_id)
    filepath = export_multi_sheet_to_excel(
        {'Batches': batches_df, 'Materials & Lots': materials_df}, f"shipment-{shipment_id}")
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


# ========================
# START THE APP
# ========================

"""
THE FLASK REQUEST/RESPONSE CYCLE
=================================
When you run app.run(), Flask starts a web server that listens for requests.
Here's what happens when a user visits your site:

1. USER ACTION:
   User types http://localhost:8000/inventory in browser

2. HTTP REQUEST:
   Browser sends: GET /inventory HTTP/1.1
   (HTTP verb + path + protocol version)

3. FLASK ROUTING:
   Flask looks through all @app.route() decorators
   Finds: @app.route('/inventory')
   Calls: view_inventory()

4. FUNCTION EXECUTION:
   - Checks authentication (if @requires_auth)
   - Runs your Python code
   - Queries database
   - Generates HTML

5. HTTP RESPONSE:
   Flask sends back:
   - Status code (200 = success, 404 = not found, 500 = error)
   - Headers (content type, cookies, etc.)
   - Body (your HTML)

6. BROWSER RENDERING:
   Browser receives HTML and displays it

This cycle repeats for every request (page load, form submission, etc.)
"""

if __name__ == '__main__':

    
    """
    START THE WEB SERVER
    ====================
    This block only runs when you execute this file directly:
      python app.py

    It doesn't run when you import this file as a module.

    CONFIGURATION EXPLAINED:
    """

    # Get port number from environment variable (for cloud hosting)
    # Railway, Heroku, etc. set PORT environment variable
    # If not set (local development), use 8000
    port = int(os.environ.get('PORT', 8000))

    # Start the Flask development server
    app.run(
        host='0.0.0.0',  # Listen on all network interfaces
                         # '0.0.0.0' = accessible from any IP address
                         # '127.0.0.1' = only accessible from this computer
                         # For cloud hosting, must be '0.0.0.0'

        port=port,       # Port number (default 8000)
                         # Access via: http://localhost:8000

        debug=False      # Production mode
                         # debug=True: Shows detailed errors, auto-reloads on code changes
                         # debug=False: Hides error details (safer for production)
    )

    """
    AFTER app.run():
    ===============
    Your terminal will show:
    * Running on http://0.0.0.0:8000
    * Do not use the development server in a production environment

    The server is now listening for requests!
    - Visit http://localhost:8000 in your browser
    - Press Ctrl+C to stop the server
    - Each request will print to the terminal (useful for debugging)
    """
