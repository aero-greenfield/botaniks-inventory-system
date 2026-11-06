# Physical count follow-up: instructions for Claude

**Read this whole file before touching any data.** Work happens on the `physical-count-followup` branch; `main` stays untouched until the user approves merging. It was written on 2026-11-05, at the end of the
edit-path safety review. The user will come back with physical shelf counts. Your job is to compare those
counts to the system, propose corrections, and apply only the corrections the user approves.

## Background (what happened)
- Bugs let an edited batch quantity drift away from stock. A recipe rename also split Foxtail stock across two
  materials, and there were status and shipment mismatches.
- The fixes were merged to `main` and deployed on 2026-11-05 (merge `dd2cce0`). Production also received:
  - `ALTER TABLE recipes ADD COLUMN housemade_material_id` (empty; it fills itself in the next time each mix
    recipe is made).
  - Batch 206 status corrected from Partially Shipped to Shipped (8 of 8 shipped, logged in `audit_log`).
- An earlier repair script (`scripts/repair_quantity_edit_desync.py`, run on 2026-11-04) corrected batch
  deductions *mathematically*. Staff also made 155 manual lot edits between June and Nov 2026, possibly
  correcting some of the same items by hand. **That's why we're counting:** only the shelf shows whether an
  item was double-corrected.

## User preferences (follow these)
- **Read-only first.** Show the user any query or write *before* running it against production. Production
  writes need explicit approval, and should be guarded (exact `WHERE` matching expected old values, one
  transaction, committed only if the rowcount matches), with a matching `audit_log` row. Daily DB backups exist.
- The user is cautious about overwriting **user-entered** data. Change only what the shelf proves wrong.
- **Never** add `Co-Authored-By: Claude` (or any Claude attribution) to commits or PRs.
- The user isn't deeply technical about databases. Explain in plain terms and give a recommendation.

## Connecting to production
- `DATABASE_URL` is **commented out** in `.env` (the local app runs on SQLite). The scripts here read that
  commented line directly. **Never print the URL.**
- `scripts/audit_readonly.py` is a read-only health check (Postgres read-only session, rolled back). Run it
  with `.venv/Scripts/python.exe scripts/audit_readonly.py` before and after any corrections.
- Python: use `.venv/Scripts/python.exe`. On Windows, set `PYTHONIOENCODING=utf-8` (some material names
  contain characters like `⁄`).

## The count sheet (system values as of 2026-11-05, in display units)
| # | Material (id) | Lot # (lot_id) | Location | System qty |
|---|---|---|---|---|
| 1 | Matcha - C (30) | FDRL15BE (34) | Brommer | 241.04 kg |
| 2 | Matcha - C (30) | FDRG09BE (33) | Fern | 0.16 kg (probably empty) |
| 3 | Matcha - C (30) | FDRL15BE (288) | Fern | 119.62 kg |
| 4 | Matcha - C (30) | FDRA19BF (248) | Brommer | 630 kg |
| 5 | Matcha - B (29) | FDYL02BE (37) | Brommer | 88.736 kg |
| 6 | Matcha - B (29) | FDYL02BE (287) | Fern | 28.72 kg |
| 7 | Sugar (33) | 26181HU1 (160) | Fern | 0.01 lb (probably empty) |
| 8 | Sugar (33) | 26202HU1 (197) | Fern | 8 lb |
| 9 | Sugar (33) | 26260HU1 (246) | Fern | 936 lb |
| 10 | Tulsi Turmeric Ginger (20) | 3 (67) | — | 11.2 lb |
| 11 | Tulsi Turmeric Ginger (20) | 26232 (233) | Brommer | 180 lb |
| 12 | Tulsi Turmeric Ginger (20) | 26247 (232) | Fern | 0.498 lb (probably empty) |
| 13 | Tulsi Turmeric Ginger (20) | 26049 (234) | Brommer | 141.902 lb |
| 14 | Tulsi Turmeric Ginger (20) | 26049 (237) | Fern | 300.01 lb |
| 15 | Turmeric C/S (21) | 25L349-3460 (68) | — | 34.8 lb |
| 16 | Turmeric C/S (21) | 26D120-3460 (238) | Fern | 110.4 lb |
| 17 | Botanik Matcha (71, house-made) | MIX-BATCH-BHT260804C2 (178) | — | 2 kg |
| 18 | Foxtail Cafe Style Matcha (42, house-made, old blend) | MIX-BATCH-FTC260923C1 (225) | — | 4 kg |
| 19 | Foxtail Cafe Style Matcha V2 (114, house-made) | no active lots | — | 0 kg |
| 20 | Bag 6 3/4" x 3 1/2" x 11 1/4" (34) | 103328400 (207) | Fern | 1487 bags |
| 21 | Bag 9 x 13 1/2 x 4 3/4" (36) | unknown11 (126) | Fern | 1224 bags |
| 22 | Box 8 x 4 x 12" (38) | 31868A (226) | Fern | 1780 boxes |

Storage units: mass lots are stored in **grams** (`raw_material_lots.quantity`), and count lots as raw
counts. Convert with `services.units` (`to_base`, `from_base`).

## How to process the counts
1. **Re-pull the current system quantities for these 22 lots (read-only).** Production keeps moving after
   2026-11-05: new batches deduct and new lots arrive. Compare each count to the system value **as of the
   day/time of the count**, not to the table above.
   - Ask the user when they counted.
   - Check `batch_materials` rows created after that time for these lots (join `batches.date_completed` /
     `audit_log`). Adjust the comparison so activity after the count isn't mistaken for a discrepancy.
2. Build a comparison table with columns: lot, system at count time, counted, difference, % difference.
3. **Use these thresholds when recommending:**
   - Differences within normal handling noise (about ≤0.5% or ≤0.1 kg/lb, whichever is larger; packaging
     ±5 units) → recommend **no change**. Tell the user it matches.
   - Larger differences → recommend correcting the lot to the counted value. Explain that this doesn't
     overwrite history: past batch records stay as they are, and only the lot's on-hand amount changes.
   - Lot shows a tiny leftover (rows 2, 7, 12) and is physically empty → set it to 0. `exhaust_lot_if_depleted`
     logic / status 'inactive' applies.
   - A lot that **can't be found** → ask before zeroing it. It may be stored somewhere else.
4. **Get the user's approval for the full list, then apply it:**
   - **Purchased lots:** the user can apply corrections in the app (Edit Lot page; enter the value in the
     display unit, the route converts to base units). Or, with approval, use a guarded SQL update in base units.
   - **House-made lots (rows 17 and 18, and any MIX-BATCH-* lot):** the Edit Lot page **blocks** quantity
     edits for house-made lots by design. Corrections need a guarded SQL update with approval, for example
     `UPDATE raw_material_lots SET quantity=%s, status=CASE WHEN %s>0.001 THEN 'active' ELSE 'inactive' END WHERE lot_id=%s AND quantity=%s`.
     Then insert an `audit_log` row (`action='lot_count_corrected'`, details with the lot, old and new
     values, and the count date).
5. Afterwards, re-run `scripts/audit_readonly.py` and confirm nothing new appears (see the known results below).

## Open items that the count settles
- **Batch 322** (Foxtail Cafe Style Matcha V2, FTC261020C1): the batch says 151 kg, but its output lot 265
  recorded only 140 kg (all consumed).
  - **If about 11 kg of V2 is found** (row 19): 151 was real. Propose recording that V2 stock as a lot on
    material 114. Also note that batch 322's inputs were only deducted for 140, so about 7.9% more of its
    inputs (Matcha B/C, sugar) was actually used. The Matcha counts above will already reflect that, so
    correct via the counts, not by re-deducting.
  - **If no V2 is found:** 140 was real. Propose setting batch 322's quantity to 140, record-only (guarded
    `UPDATE batches`, no stock change, because its lot is fully consumed).
- **Lot 225** (old Foxtail blend, 4 kg): if it isn't on the shelf, propose zeroing it.

## Open items that DON'T depend on the count
- **Foxtail decision (the user is asking their boss).** Finished recipes 43 (Retail Cafe), 53 (unique 22kg)
  and 54 (unique 18kg) still consume material 42 (the old blend). Recipe 26 consumes 114 (V2). If the boss
  says to switch to V2, point those recipes' `recipe_materials` from material 42 to 114 (recipe edit page,
  or a guarded SQL update). Also ask whether recipe 13 ("Foxtail Cafe Style Matcha V1", no batches, no
  material) should be deleted. **Don't rename recipe 13**; the user chose to keep the V1 name.
- **Batch 287** (FTC260930C1): the record says 236 kg, but 240 kg was produced and consumed. The
  recommendation was to leave it and add a note to the batch (record-only).
- **Vendi Bean (material 113)** has `is_housemade = false` but holds only MIX-BATCH lots (all 0 qty).
  Optional: set `is_housemade = true`. Harmless either way, because the new code handles it.

## Known audit results (expected and NOT problems, as of 2026-11-05)
- 19 duplicate `MIX-BATCH-*` lot numbers: leftovers from batches deleted and re-created, plus mix batches
  sharing a `batch_number`. All have 0 quantity.
- 31 batch/material pairs drawn from more than one lot. This is normal.
- A4: material 42 has no matching recipe name, and 7 mix recipes have no material yet (never produced, or V1).
  This is the open Foxtail decision.
- 2 mix batches out of sync with their output lot: 287 and 322 (above).
- 43 old "Foxtail Cafe Style Matcha" mix batches whose name matches no recipe. This is history the user
  chose to keep.

Anything **new** beyond this list is worth flagging to the user.

## Remaining code work (not urgent, for later sessions, one at a time with tests)
- MEDIUM: add `batches.recipe_id` so renamed recipes don't orphan old batches (units missing on some pages,
  deferred planned batches fail promotion). Keep `product_name` as history.
- MEDIUM: block switching a material between Weight and Count once it has stock or recipe usage.
- MEDIUM: refuse to delete a material that's used in recipes or batches (offer archive instead).
- LOW: make product_name read-only on the edit-batch page.
- LOW: clear reserved lots on deferred Planned batches when their recipe's ingredients change (warn the user).
- LOW: the create-batch error page shows a generic message when a mix name collides with a purchased
  material. Show the real reason (`_resolve_housemade_material` in services/batches.py).
