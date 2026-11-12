// ADDED: planned-deduction-mode feature — extracted verbatim from the inline script in
// create_batch.html so edit_batch.html can reuse it to edit the RESERVED lots of a deferred
// Planned batch. Two callers, one implementation.
//
// UNITS: recipe amounts and lot quantities are STORED in the base unit (grams for mass), but the
// UI displays and accepts input in the unit the recipe line was entered in (display_unit, e.g. lb).
// factor = base units per 1 display unit. All math here stays in display units so coverage reads
// as "176 vs 176"; collectLotSelections multiplies back by factor so the backend always sees grams.

/**
 * Builds one card per recipe material, each with one or more (lot, quantity) rows.
 *
 * @param cardsEl       container element; its children are replaced
 * @param materials     [{material_id, material_name, quantity_needed, display_unit, base_per_display}]
 * @param availableLots {material_id: [{lot_id, lot_number, quantity}]}  quantities in BASE units
 * @param batchQty      number of units being produced
 * @param prefill       optional {material_id: [{lot_id, qty}]} in BASE units — existing picks to restore
 */
function renderLotPicker(cardsEl, materials, availableLots, batchQty, prefill) {
    cardsEl.innerHTML = '';
    prefill = prefill || {};

    materials.forEach(function (mat) {
        var factor = mat.base_per_display || 1;
        var required = mat.quantity_needed * batchQty / factor;
        var unit = mat.display_unit ? (' ' + mat.display_unit) : '';
        var lots = availableLots[mat.material_id] || [];
        // map lot_id -> available quantity (in display units), for auto-fill
        var lotAvail = {};
        lots.forEach(function (l) { lotAvail[l.lot_id] = l.quantity / factor; });

        var card = document.createElement('div');
        card.style.cssText = 'border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:10px;';
        card.dataset.materialId = mat.material_id;
        card.dataset.required = required;  // used by the submit-time coverage check (display units)
        card.dataset.factor = factor;      // used to convert entered display-unit qty -> base on submit

        var optHtml = '<option value="">-- select lot --</option>';
        // CHANGED: lot-picker-location — show which warehouse the lot is in, e.g.
        //          "Lot: 26260HU1 | Location: Fern | 936.00 lb available" ("—" when no location was recorded).
        lots.forEach(function (l) {
            optHtml += '<option value="' + l.lot_id + '">Lot: ' + l.lot_number + ' | Location: ' + (l.location || '—') + ' | ' +
                (l.quantity / factor).toFixed(2) + unit + ' available</option>';
        });

        card.innerHTML =
            '<div style="font-weight:600;font-size:13px;margin-bottom:6px;">' + mat.material_name +
            ' <span style="font-weight:400;color:var(--text-muted);">— need ' + required.toFixed(2) + unit + '</span>' +
            ' <span class="lot-remaining" style="font-weight:400;font-size:12px;margin-left:6px;"></span></div>' +
            '<div class="lot-rows"></div>' +
            '<button type="button" class="add-lot-btn" style="margin-top:6px;font-size:12px;padding:4px 10px;border:1px dashed var(--border);border-radius:5px;background:transparent;cursor:pointer;">+ Add lot</button>' +
            '<div class="lot-error" style="color:#dc2626;font-size:12px;margin-top:4px;display:none;"></div>';
        var remainingEl = card.querySelector('.lot-remaining');

        // sum of quantities entered in all rows except (optionally) one to exclude
        function sumEntered(excludeInput) {
            var total = 0;
            card.querySelectorAll('.lot-rows > div input[type="number"]').forEach(function (inp) {
                if (inp === excludeInput) return;
                var v = parseFloat(inp.value);
                if (v > 0) total += v;
            });
            return total;
        }

        function updateRemaining() {
            var remaining = required - sumEntered(null);
            if (Math.abs(remaining) <= 1e-6) {
                remainingEl.textContent = '✓ covered';
                remainingEl.style.color = '#16a34a';
            } else if (remaining > 0) {
                remainingEl.textContent = 'remaining: ' + remaining.toFixed(2) + unit;
                remainingEl.style.color = 'var(--text-muted)';
            } else {
                remainingEl.textContent = 'over by ' + Math.abs(remaining).toFixed(2) + unit;
                remainingEl.style.color = '#dc2626';
            }
        }

        function addRow(preLotId, preQtyDisplay) {
            var row = document.createElement('div');
            row.style.cssText = 'display:flex;gap:8px;align-items:center;margin-bottom:4px;';
            row.innerHTML =
                '<select style="flex:1;padding:5px 8px;border:1px solid var(--border);border-radius:5px;font-size:13px;">' + optHtml + '</select>' +
                '<input type="number" step="any" min="0.01" placeholder="qty" style="width:90px;padding:5px 8px;border:1px solid var(--border);border-radius:5px;font-size:13px;">' +
                '<button type="button" style="padding:4px 8px;border:1px solid var(--border);border-radius:5px;background:transparent;cursor:pointer;color:#dc2626;">×</button>';
            var sel = row.querySelector('select');
            var qtyInput = row.querySelector('input[type="number"]');

            // Auto-fill the remaining needed amount when a lot is picked,
            // capped at that lot's available quantity. Only overwrite empty
            // or previously auto-filled inputs so hand-typed values are kept.
            sel.addEventListener('change', function () {
                var lotId = parseInt(sel.value);
                if (!lotId) { updateRemaining(); return; }
                // Prevent the same lot being picked twice within this material card —
                // double-counting a lot would inflate the covered total.
                var dup = false;
                card.querySelectorAll('.lot-rows > div select').forEach(function (otherSel) {
                    if (otherSel !== sel && parseInt(otherSel.value) === lotId) dup = true;
                });
                if (dup) {
                    var dupErr = card.querySelector('.lot-error');
                    dupErr.textContent = 'That lot is already selected for this material — pick a different lot.';
                    dupErr.style.display = 'block';
                    sel.value = '';
                    qtyInput.value = '';
                    qtyInput.dataset.autofilled = '';
                    updateRemaining();
                    return;
                }
                card.querySelector('.lot-error').style.display = 'none';
                if (qtyInput.value !== '' && qtyInput.dataset.autofilled !== '1') {
                    updateRemaining();
                    return;
                }
                var remaining = required - sumEntered(qtyInput);
                var avail = lotAvail[lotId] != null ? lotAvail[lotId] : remaining;
                var fill = Math.min(remaining, avail);
                if (fill < 0) fill = 0;
                qtyInput.value = fill > 0 ? fill : '';
                qtyInput.dataset.autofilled = '1';
                updateRemaining();
            });

            // A hand edit clears the auto-filled flag so we won't clobber it later.
            qtyInput.addEventListener('input', function () {
                qtyInput.dataset.autofilled = '';
                updateRemaining();
            });

            row.querySelector('button').addEventListener('click', function () { row.remove(); updateRemaining(); });
            card.querySelector('.lot-rows').appendChild(row);

            // Restoring a saved pick: set the values directly, never through the change handler,
            // whose auto-fill would overwrite the quantity the user actually reserved. A lot that
            // has since gone inactive isn't in optHtml, so add it back rather than dropping the row
            // silently — the user needs to see the pick that will fail at promotion.
            if (preLotId) {
                if (!sel.querySelector('option[value="' + preLotId + '"]')) {
                    var opt = document.createElement('option');
                    opt.value = preLotId;
                    opt.textContent = 'Lot ' + preLotId + ' (unavailable)';
                    sel.appendChild(opt);
                }
                sel.value = String(preLotId);
                qtyInput.value = preQtyDisplay;
            }
        }

        var saved = prefill[mat.material_id];
        if (saved && saved.length) {
            saved.forEach(function (p) { addRow(p.lot_id, Math.round((p.qty / factor) * 10000) / 10000); });
        } else {
            addRow();
        }
        card.querySelector('.add-lot-btn').addEventListener('click', function () { addRow(); });
        cardsEl.appendChild(card);
        updateRemaining();
    });
}

/**
 * Reads the picker back out.
 *
 * @param cardsEl  the same container passed to renderLotPicker
 * @param opts     {optional: bool} — when true (a DEFERRED planned batch), a material may be left
 *                 unpicked or under-covered and is simply omitted; promotion fills it FIFO. When
 *                 false, every material must be fully covered or the caller must block submit.
 * @returns {selections, valid, anyIncomplete}  selections values are in BASE units.
 */
function collectLotSelections(cardsEl, opts) {
    var optional = !!(opts && opts.optional);
    var selections = {};
    var valid = true;
    var anyIncomplete = false;

    cardsEl.querySelectorAll(':scope > div').forEach(function (card) {
        var matId = card.dataset.materialId;
        var required = parseFloat(card.dataset.required) || 0;  // display units
        var factor = parseFloat(card.dataset.factor) || 1;       // base units per display unit
        var errEl = card.querySelector('.lot-error');
        var entries = [];
        var seen = {};
        var dup = false;
        var total = 0;  // display units, for the coverage check

        card.querySelectorAll('.lot-rows > div').forEach(function (row) {
            var lotId = parseInt(row.querySelector('select').value);
            var qty = parseFloat(row.querySelector('input[type="number"]').value);
            if (lotId && qty > 0) {
                if (seen[lotId]) { dup = true; }
                seen[lotId] = true;
                total += qty;
                // Backend deducts in the base unit (grams), so convert the display-unit qty back.
                entries.push({lot_id: lotId, qty: qty * factor});
            }
        });

        // Duplicate lots always make the selection invalid, optional or not.
        if (dup) {
            errEl.textContent = 'A lot is selected more than once for this material — remove the duplicate.';
            errEl.style.display = 'block';
            valid = false;
            return;
        }

        if (optional) {
            // Only send selections when this material is fully, validly covered; otherwise leave it
            // out so promotion fills it FIFO. Never block.
            errEl.style.display = 'none';
            if (entries.length && total + 1e-6 >= required) {
                selections[matId] = entries;
            } else {
                anyIncomplete = true;
            }
            return;
        }

        // Immediate batch: require at least one lot and full coverage.
        if (!entries.length) {
            errEl.textContent = 'Select at least one lot.';
            errEl.style.display = 'block';
            valid = false;
        } else if (total + 1e-6 < required) {
            errEl.textContent = 'Selected lots cover only ' + total.toFixed(2) + ' of ' + required.toFixed(2) +
                ' needed — cover the full amount before creating the batch.';
            errEl.style.display = 'block';
            valid = false;
        } else {
            errEl.style.display = 'none';
            selections[matId] = entries;
        }
    });

    return {selections: selections, valid: valid, anyIncomplete: anyIncomplete};
}
