"""Repair the 8 production batches whose quantity was edited without reconciling stock.

Background
----------
update_batch() used to be a bare `UPDATE batches` and the only raw-material reconciliation path
was gated behind an unchecked "Also adjust raw material deductions" checkbox. Editing a batch's
quantity therefore left BOTH its input deductions and (for a mix batch) its house-made output
lot sized for the ORIGINAL quantity. Found by replaying audit_log: 27 batches had a quantity
edit, 13 survive, 8 are desynced.

That is fixed in update_batch now, but the existing rows still carry the damage. This script
replays each edit through the FIXED code path — `update_batch(..., materials_basis_quantity=)`
names the quantity the stored deductions were really sized for, so the proportional scaling is
computed against reality rather than against the already-corrupted column. Every correction is
therefore a normal, audited edit: no hand-written UPDATE statements.

Deliberately NOT touched
------------------------
Batches 83 / 84 / 85 (FTCC7513 / FTCC7512 / FTCC7510) are a DIFFERENT bug: `batches.quantity`
was declared INTEGER, so Postgres rounded 18.75 -> 19 at insert while the output lot kept the
exact value. There the LOT is correct and the BATCH ROW is the corrupted side, so reconciling
them would push the rounded number into good lots and destroy 250-500 g of real stock each.
They are repaired by the NUMERIC migration plus a direct batch-row correction — see the notes
in services/setup.py.

Usage
-----
    python scripts/repair_quantity_edit_desync.py            # dry run (default, read-only)
    python scripts/repair_quantity_edit_desync.py --apply    # write

DATABASE_URL in .env points at live Supabase and there is no local SQLite copy, so --apply
writes to production. Take a snapshot first.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Material names contain characters the Windows console codepage can't encode (e.g. the
# fraction slash in 'Bag - White - 6 3/4" x ...'), which would otherwise abort the report
# mid-run with a UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv()

from database import get_db_connection          # noqa: E402
from services import units                      # noqa: E402
from services.audit import log_action           # noqa: E402
from services.batches import update_batch, BatchReconcileError  # noqa: E402

# (batch_id, batch_number, quantity the stored deductions were actually sized for)
# "basis" is the batch_created quantity from audit_log; the batch row now holds a later edit.
TARGETS = [
    (245, "FTC260902C1", 240.0),
    (287, "FTC260930C1", 240.0),
    (295, "FTC260921C1", 127.0),
    (322, "FTC261020C1", 140.0),
    (206, "BHT260804C2", 10.0),
    (331, "FTC261021C1", 45.0),
    (332, "FTC261022C1", 45.0),
    (334, "FTC261020C1", 35.0),
]


def snapshot(db, cursor, batch_id):
    """Current state of a batch: its quantity, its input rows, and its mix output lot."""
    db.execute(cursor, """
        SELECT batch_number, product_name, batch_type, quantity, mix_lot_id
        FROM batches WHERE batch_id = %s
    """, (batch_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    number, product, btype, quantity, mix_lot_id = row

    db.execute(cursor, """
        SELECT bm.batch_material_id, bm.material_id, rm.name, rm.unit, bm.lot_id, bm.quantity_used
        FROM batch_materials bm JOIN raw_materials rm ON rm.material_id = bm.material_id
        WHERE bm.batch_id = %s ORDER BY bm.batch_material_id
    """, (batch_id,))
    inputs = [(bm, mid, name, unit, lot, float(used)) for bm, mid, name, unit, lot, used in cursor.fetchall()]

    lot = None
    if mix_lot_id:
        db.execute(cursor, """
            SELECT l.quantity, l.status, rm.unit,
                   (SELECT COALESCE(SUM(quantity_used), 0) FROM batch_materials WHERE lot_id = l.lot_id)
            FROM raw_material_lots l JOIN raw_materials rm ON rm.material_id = l.material_id
            WHERE l.lot_id = %s
        """, (mix_lot_id,))
        r = cursor.fetchone()
        if r:
            lot = dict(lot_id=mix_lot_id, remaining=float(r[0]), status=r[1], unit=r[2], consumed=float(r[3]))

    return dict(number=number, product=product, btype=btype,
                quantity=float(quantity), mix_lot_id=mix_lot_id, inputs=inputs, lot=lot)


def describe(label, state, basis=None):
    print(f"  {label}: quantity={state['quantity']:g}" + (f"  (deductions sized for {basis:g})" if basis else ""))
    for _bm, _mid, name, unit, lot_id, used in state["inputs"]:
        print(f"      input  {name[:34]:<34} lot {str(lot_id):>5}  {used:>12.4f} g"
              f"  = {float(units.from_base(used, unit)):>10.4g} {unit}")
    if state["lot"]:
        l = state["lot"]
        produced = l["remaining"] + l["consumed"]
        print(f"      output lot {l['lot_id']}: built_for {produced:.1f} g "
              f"({float(units.from_base(produced, l['unit'])):g} {l['unit']}), "
              f"consumed {l['consumed']:.1f} g, remaining {l['remaining']:.1f} g [{l['status']}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    args = ap.parse_args()

    db = get_db_connection()
    cursor = db.cursor()

    print("=" * 96)
    print(("APPLYING" if args.apply else "DRY RUN — no writes") + f"   target: {os.getenv('DATABASE_URL', 'SQLite')[:48]}")
    print("=" * 96)

    planned = []
    for batch_id, expected_number, basis in TARGETS:
        before = snapshot(db, cursor, batch_id)
        print(f"\nbatch {batch_id} {expected_number} ({before['btype'] if before else '?'})")
        if before is None:
            print("  !! not found — skipping")
            continue
        if before["number"] != expected_number:
            print(f"  !! batch_number is {before['number']!r}, expected {expected_number!r} — SKIPPING")
            continue

        ratio = before["quantity"] / basis
        describe("BEFORE", before, basis)
        print(f"  -> replay: scale every input row by {before['quantity']:g}/{basis:g} = {ratio:.6f}")
        for _bm, _mid, name, unit, lot_id, used in before["inputs"]:
            delta = used * ratio - used
            verb = "deduct" if delta > 0 else "return"
            print(f"      {verb} {abs(delta):>12.4f} g  "
                  f"({abs(float(units.from_base(delta, unit))):>8.4g} {unit})  "
                  f"{name[:30]:<30} lot {lot_id}")
        if before["lot"]:
            l = before["lot"]
            new_produced = float(units.to_base(before["quantity"], l["unit"]))
            print(f"      output lot {l['lot_id']}: remaining {l['remaining']:.1f} g -> "
                  f"{max(0.0, new_produced - l['consumed']):.1f} g")
        planned.append((batch_id, before["quantity"], basis))

    # ---- feasibility pre-flight -----------------------------------------------------------
    # A repair that RAISES a quantity must draw the extra material from the very lot the batch
    # originally used — and several of those lots have since been fully consumed by other
    # batches. _scale_batch_material_rows refuses rather than driving a lot negative, so those
    # batches will be REPORTED AS REFUSED instead of silently repaired. Surface it up front.
    print("\n" + "=" * 96)
    print("FEASIBILITY PRE-FLIGHT (increases only — returning stock is always possible)")
    print("=" * 96)
    blocked = {}
    for batch_id, current_quantity, basis in planned:
        state = snapshot(db, cursor, batch_id)
        ratio = current_quantity / basis
        for _bm, _mid, name, unit, lot_id, used in state["inputs"]:
            delta = used * ratio - used
            if delta <= 0 or not lot_id:
                continue
            db.execute(cursor, "SELECT quantity, status FROM raw_material_lots WHERE lot_id = %s", (lot_id,))
            r = cursor.fetchone()
            have = float(r[0]) if r else 0.0
            if have + 1e-3 < delta:
                blocked.setdefault(batch_id, []).append(
                    f"lot {lot_id} ({name[:28]}) needs {delta:.4f} g, has {have:.4f} g [{r[1] if r else '?'}]")
    if blocked:
        for batch_id, reasons in blocked.items():
            print(f"  batch {batch_id}: CANNOT be repaired from its original lots")
            for reason in reasons:
                print(f"      - {reason}")
        print(f"\n  {len(planned) - len(blocked)} of {len(planned)} batches are repairable now; "
              f"{len(blocked)} are blocked on exhausted source lots.")
    else:
        print("  all planned repairs are feasible from their original lots.")

    if not args.apply:
        print("\n" + "=" * 96)
        print(f"DRY RUN complete — {len(planned) - len(blocked)} of {len(planned)} batches would be "
              f"repaired, {len(blocked)} refused. Nothing was written.")
        print("Re-run with --apply to write (take a Supabase snapshot first).")
        db.close()
        return

    print("\n" + "=" * 96)
    ok, failed = 0, []
    for batch_id, current_quantity, basis in planned:
        try:
            result = update_batch(batch_id, quantity=current_quantity, materials_basis_quantity=basis)
            if result:
                ok += 1
                log_action("batch_quantity_desync_repaired",
                           f"batch_id={batch_id}, quantity={current_quantity}, deductions_were_sized_for={basis}")
                print(f"  batch {batch_id}: repaired")
            else:
                failed.append((batch_id, "update_batch returned None"))
                print(f"  batch {batch_id}: FAILED (returned None)")
        except BatchReconcileError as e:
            failed.append((batch_id, str(e)))
            print(f"  batch {batch_id}: REFUSED — {e}")

    print("\n" + "=" * 96)
    print(f"repaired {ok} / {len(planned)}")
    for batch_id, why in failed:
        print(f"  still broken: batch {batch_id} — {why}")

    print("\nAfter state:")
    for batch_id, _q, basis in planned:
        after = snapshot(db, cursor, batch_id)
        print(f"\nbatch {batch_id} {after['number']}")
        describe("AFTER", after)

    db.close()


if __name__ == "__main__":
    main()
