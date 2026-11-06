"""READ-ONLY production data audit (created 2026-11-05 during the edit-path safety review).

Usage (from the repo root):  .venv/Scripts/python.exe scripts/audit_readonly.py
Opens a READ-ONLY session (Postgres rejects any write), runs SELECTs only, rolls back.
Reads the DATABASE_URL line from .env even when it is commented out; never prints it.
Healthy result: every check is 0 rows EXCEPT the known items listed in
docs/physical-count-followup.md (section "Known audit results").
"""
import sys, re
import psycopg2

REPO = sys.argv[1] if len(sys.argv) > 1 else "."
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, REPO)
from services import units  # pure-logic module, no DB/Flask

url = None
for line in open(f"{REPO}/.env", encoding="utf-8"):
    m = re.match(r"\s*#?\s*DATABASE_URL\s*=\s*(\S+)", line)
    if m:
        url = m.group(1).strip().strip('"').strip("'")
        break
if not url:
    sys.exit("no DATABASE_URL line found")

conn = psycopg2.connect(url)
conn.set_session(readonly=True, autocommit=False)
cur = conn.cursor()

def q(sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()

def show(title, sql, args=(), limit=15):
    rows = q(sql, args)
    print(f"\n== {title}: {len(rows)} row(s)")
    for r in rows[:limit]:
        print("   ", r)
    if len(rows) > limit:
        print(f"    ... {len(rows) - limit} more")
    return rows

print("read-only session:", q("SHOW transaction_read_only")[0][0])

# 0. schema: has the INTEGER -> NUMERIC migration run?
show("[schema] batches.quantity column type",
     "SELECT data_type FROM information_schema.columns WHERE table_name='batches' AND column_name='quantity'")

# A1. produced/shipped deferred batches that never deducted; mixes with no output lot
show("[A1] non-Planned deferred finished/mix batches with NO batch_materials", """
    SELECT b.batch_id, b.batch_number, b.product_name, b.batch_type, b.status, b.quantity
    FROM batches b
    WHERE b.status <> 'Planned' AND b.batch_type IN ('finished','mix') AND b.deduction_mode = 'deferred'
      AND NOT EXISTS (SELECT 1 FROM batch_materials bm WHERE bm.batch_id = b.batch_id)
    ORDER BY b.batch_id""")
show("[A1] non-Planned mix batches with NO output lot (mix_lot_id NULL)", """
    SELECT batch_id, batch_number, product_name, status, quantity
    FROM batches WHERE batch_type = 'mix' AND status <> 'Planned' AND mix_lot_id IS NULL ORDER BY batch_id""")

# A2. duplicate / orphaned MIX output lots; deferred batches stuck Planned with deductions
show("[A2] MIX-BATCH lot numbers that appear more than once", """
    SELECT lot_number, COUNT(*), SUM(quantity) FROM raw_material_lots
    WHERE lot_number LIKE 'MIX-BATCH-%%' GROUP BY lot_number HAVING COUNT(*) > 1 ORDER BY lot_number""")
show("[A2] MIX-BATCH lots with stock that no batch points to (mix_lot_id)", """
    SELECT l.lot_id, l.lot_number, l.quantity, l.status FROM raw_material_lots l
    WHERE l.lot_number LIKE 'MIX-BATCH-%%' AND l.quantity > 0.001
      AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.mix_lot_id = l.lot_id) ORDER BY l.lot_id""")
show("[A2] Planned batches that already have batch_materials", """
    SELECT b.batch_id, b.batch_number, b.product_name, b.deduction_mode, b.promotion_failure_reason
    FROM batches b WHERE b.status = 'Planned'
      AND EXISTS (SELECT 1 FROM batch_materials bm WHERE bm.batch_id = b.batch_id)
      AND b.deduction_mode = 'deferred' ORDER BY b.batch_id""")

# A3. multi-lot override signature: same material drawn from >1 lot with identical quantity_used on every row
show("[A3] batch/material pairs over >1 lot with identical quantity_used (override-bug signature)", """
    SELECT batch_id, material_id, COUNT(*), MIN(quantity_used), MAX(quantity_used)
    FROM batch_materials GROUP BY batch_id, material_id
    HAVING COUNT(*) > 1 AND MIN(quantity_used) = MAX(quantity_used) ORDER BY batch_id""")
show("[A3] (context) all batch/material pairs drawn from >1 lot", """
    SELECT batch_id, material_id, COUNT(*) FROM batch_materials
    GROUP BY batch_id, material_id HAVING COUNT(*) > 1 ORDER BY batch_id""", limit=5)

# A4. house-made materials vs mix recipes (rename split)
show("[A4] house-made materials whose name matches NO recipe (orphaned by rename?)", """
    SELECT rm.material_id, rm.name, rm.unit,
           (SELECT COALESCE(SUM(quantity),0) FROM raw_material_lots l WHERE l.material_id = rm.material_id AND l.status='active') AS active_stock,
           (SELECT COUNT(*) FROM recipe_materials x WHERE x.material_id = rm.material_id) AS used_in_recipes
    FROM raw_materials rm WHERE rm.is_housemade
      AND NOT EXISTS (SELECT 1 FROM recipes r WHERE LOWER(r.product_name) = LOWER(rm.name)) ORDER BY rm.material_id""")
show("[A4] mix recipes with NO house-made material of the same name", """
    SELECT r.recipe_id, r.product_name, r.product_unit FROM recipes r WHERE r.batch_type = 'mix'
      AND NOT EXISTS (SELECT 1 FROM raw_materials rm WHERE rm.is_housemade AND LOWER(rm.name) = LOWER(r.product_name))""")

# A5. recipe unit vs house-made material unit
show("[A5] mix recipe product_unit differs from its house-made material unit", """
    SELECT r.recipe_id, r.product_name, r.product_unit, rm.material_id, rm.unit
    FROM recipes r JOIN raw_materials rm ON rm.is_housemade AND LOWER(rm.name) = LOWER(r.product_name)
    WHERE LOWER(COALESCE(r.product_unit,'')) <> LOWER(COALESCE(rm.unit,''))""")

# A6. dimension vs unit disagreement
mats = q("SELECT material_id, name, unit, dimension FROM raw_materials")
bad = [m for m in mats if m[3] and units.dimension_of(m[2] or '') != m[3]]
print(f"\n== [A6] materials whose dimension disagrees with their unit: {len(bad)} row(s)")
for m in bad[:15]:
    print("   ", m, "-> unit implies", units.dimension_of(m[2] or ''))

# mix desync (what this branch fixes): produced (remaining + consumed) vs batch quantity in base units
rows = q("""
    SELECT b.batch_id, b.batch_number, b.product_name, b.quantity, rm.unit, rm.dimension, l.quantity,
           (SELECT COALESCE(SUM(quantity_used),0) FROM batch_materials bm WHERE bm.lot_id = l.lot_id)
    FROM batches b JOIN raw_material_lots l ON l.lot_id = b.mix_lot_id
    JOIN raw_materials rm ON rm.material_id = l.material_id WHERE b.batch_type = 'mix'""")
desync = []
for bid, bnum, name, qty, unit, dim, remaining, consumed in rows:
    unit = unit or 'units'
    dim = dim or units.dimension_of(unit)
    expected = float(units.to_base(qty, unit)) if dim == 'mass' else float(qty)
    produced = float(remaining) + float(consumed)
    if abs(produced - expected) > 0.01:
        desync.append((bid, bnum, name, float(qty), unit, round(expected, 3), round(produced, 3)))
print(f"\n== [branch fix #2] mix batches whose output lot != batch quantity: {len(desync)} of {len(rows)}")
for d in desync[:20]:
    print("    (batch_id, number, product, qty, unit, expected_base, lot_produced_base)", d)

# B7 / B8. shipments vs batch quantity and status
show("[B7] batches with more shipped than their quantity (negative remaining)", """
    SELECT b.batch_id, b.batch_number, b.product_name, b.quantity, SUM(sb.quantity) AS shipped, b.status
    FROM batches b JOIN shipment_batches sb ON sb.batch_id = b.batch_id
    GROUP BY b.batch_id, b.batch_number, b.product_name, b.quantity, b.status
    HAVING SUM(sb.quantity) > b.quantity + 0.0001 ORDER BY b.batch_id""")
show("[B7/B8] batch status disagrees with its shipment allocations", """
    SELECT batch_id, batch_number, product_name, status, quantity, shipped, derived FROM (
      SELECT b.batch_id, b.batch_number, b.product_name, b.status, b.quantity,
             COALESCE((SELECT SUM(quantity) FROM shipment_batches sb WHERE sb.batch_id = b.batch_id),0) AS shipped,
             CASE WHEN COALESCE((SELECT SUM(quantity) FROM shipment_batches sb WHERE sb.batch_id = b.batch_id),0) <= 0 THEN 'Ready'
                  WHEN COALESCE((SELECT SUM(quantity) FROM shipment_batches sb WHERE sb.batch_id = b.batch_id),0) < b.quantity THEN 'Partially Shipped'
                  ELSE 'Shipped' END AS derived
      FROM batches b WHERE b.status <> 'Planned') t
    WHERE status <> derived ORDER BY batch_id""")
show("[B8] Planned batches that are on a shipment", """
    SELECT DISTINCT b.batch_id, b.batch_number, b.product_name FROM batches b
    JOIN shipment_batches sb ON sb.batch_id = b.batch_id WHERE b.status = 'Planned'""")

# B9. dangling references from deleted materials / lots
show("[B9] recipe_materials pointing at a missing material", """
    SELECT x.recipe_id, x.material_name, x.material_id FROM recipe_materials x
    WHERE x.material_id IS NULL OR NOT EXISTS (SELECT 1 FROM raw_materials rm WHERE rm.material_id = x.material_id)""")
show("[B9] batch_materials pointing at a missing material or lot", """
    SELECT bm.batch_id, bm.material_id, bm.lot_id FROM batch_materials bm
    WHERE NOT EXISTS (SELECT 1 FROM raw_materials rm WHERE rm.material_id = bm.material_id)
       OR (bm.lot_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM raw_material_lots l WHERE l.lot_id = bm.lot_id))""")
show("[stock] lots with negative quantity", """
    SELECT lot_id, lot_number, material_id, quantity, status FROM raw_material_lots WHERE quantity < -0.001""")

# B10/B11. batches whose name matches no recipe
show("[B10/B11] batches whose product_name matches NO recipe (by type/status)", """
    SELECT b.batch_type, b.status, COUNT(*), MIN(b.product_name), MAX(b.product_name) FROM batches b
    WHERE NOT EXISTS (SELECT 1 FROM recipes r WHERE LOWER(r.product_name) = LOWER(b.product_name))
    GROUP BY b.batch_type, b.status ORDER BY 3 DESC""")
show("[B11] DEFERRED Planned batches with no matching recipe (will fail promotion)", """
    SELECT b.batch_id, b.batch_number, b.product_name, b.planned_completion_date FROM batches b
    WHERE b.status = 'Planned' AND b.deduction_mode = 'deferred'
      AND NOT EXISTS (SELECT 1 FROM recipes r WHERE LOWER(r.product_name) = LOWER(b.product_name))""")

# B12. stuck Planned batches
show("[B12] Planned batches with a promotion failure recorded", """
    SELECT batch_id, batch_number, product_name, planned_completion_date, promotion_failure_reason
    FROM batches WHERE status = 'Planned' AND promotion_failure_reason IS NOT NULL ORDER BY batch_id""")

conn.rollback()
conn.close()
print("\ndone (rolled back, nothing written)")
