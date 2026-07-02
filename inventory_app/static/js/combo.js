/* ── Shared searchable-dropdown (combo) component ─────────────────────────
   One reusable type-to-search dropdown used across Sendy for pick-list /
   free-text fields (mapping's Category, Brand, Color, Packaging, Unit type,
   Condition; cashbook's Category, ผู้ใช้; ...). A combo is:
     <div class="combo" data-opts="<key>" [data-allow-new="1"]>
       <input class="combo-input ...">          visible text box (type to filter)
       <input type="hidden" class="combo-value" ...>   canonical value
       <input type="hidden" class="combo-other" ...>   (allow-new only) free text
       <div class="ac-drop"></div>
     </div>
   Behaviour: focus → full list; type → substring filter; pick → fills value.
   allow-new: an unmatched typed value is kept (→ combo-other when present,
   else → combo-value, i.e. value==label plain-text fields). pick-only: an
   unmatched value resolves to empty.

   Sync provider only — reads the page-defined global `COMBO_OPTS` object
   (declared either as `const COMBO_OPTS = {...}` or `window.COMBO_OPTS =
   {...}` in the page's own <script>; both resolve as a bare global here).
   No async/fetch, no keyboard-nav, no submit-guard — see conversions/
   pair_form.html for that (separate, unrelated combo implementation). */

// Resolve the combo's hidden value(s) from the current input text.
function comboCommit(div) {
  const opts  = COMBO_OPTS[div.dataset.opts] || [];
  const input = div.querySelector('.combo-input');
  const valEl = div.querySelector('.combo-value');
  const othEl = div.querySelector('.combo-other');
  const allowNew = div.dataset.allowNew === '1';
  const text = (input.value || '').trim();
  const opt = opts.find(o => o.label === text) ||
              opts.find(o => String(o.v) === text);   // also match by raw value
  if (opt) {
    input.value = opt.label;
    if (valEl) valEl.value = opt.v;
    if (othEl) othEl.value = '';
  } else if (allowNew) {
    if (othEl) { if (valEl) valEl.value = ''; othEl.value = text; }
    else if (valEl) valEl.value = text;          // free-text combo (unit/condition)
  } else {                                        // pick-only, no match → empty
    if (valEl) valEl.value = '';
    if (othEl) othEl.value = '';
  }
  // notify dependents (e.g. sm-unit → unit-conversion row visibility)
  if (valEl) valEl.dispatchEvent(new Event('change', { bubbles: true }));
}

// Sync a combo's VISIBLE input from its hidden value/other (used on load and
// after the modal prefills the hidden fields programmatically).
function comboSyncLabel(div) {
  const opts  = COMBO_OPTS[div.dataset.opts] || [];
  const input = div.querySelector('.combo-input');
  const valEl = div.querySelector('.combo-value');
  const othEl = div.querySelector('.combo-other');
  if (valEl && valEl.value !== '') {
    const opt = opts.find(o => String(o.v) === String(valEl.value));
    input.value = opt ? opt.label : valEl.value;
  } else if (othEl && othEl.value) {
    input.value = othEl.value;
  } else if (input.value) {
    comboCommit(div);   // input pre-filled (e.g. staged category text) → resolve
  }
}

function comboRender(div, q) {
  const opts = COMBO_OPTS[div.dataset.opts] || [];
  const drop = div.querySelector('.ac-drop');
  const ql = (q || '').toLowerCase();
  const hits = (ql ? opts.filter(o => o.label.toLowerCase().includes(ql)) : opts).slice(0, 80);
  drop.innerHTML = hits.length
    ? hits.map(o =>
        `<div class="ac-item" data-v="${String(o.v).replace(/"/g,'&quot;')}">` +
        `<span class="flex-fill">${o.label}</span>` +
        (o.hint ? `<span class="ac-sku">${o.hint}</span>` : '') + `</div>`).join('')
    : '<div class="ac-item text-subtle">ไม่พบ — ' +
      (div.dataset.allowNew === '1' ? 'พิมพ์เพื่อใช้ค่าใหม่ได้' : 'เลือกจากรายการ') + '</div>';
}
function comboOpen(div) {
  const input = div.querySelector('.combo-input');
  const drop  = div.querySelector('.ac-drop');
  const rect = input.getBoundingClientRect();
  drop.style.left = rect.left + 'px';
  drop.style.top = (rect.bottom + 2) + 'px';
  drop.style.width = Math.max(rect.width, 220) + 'px';
  drop.classList.add('open');
  input.classList.add('combo-open');
}
function comboClose(div) {
  div.querySelector('.ac-drop').classList.remove('open');
  div.querySelector('.combo-input').classList.remove('combo-open');
}

function initCombo(div) {
  if (div._comboInit) return;
  div._comboInit = true;
  const input = div.querySelector('.combo-input');
  const drop  = div.querySelector('.ac-drop');
  comboSyncLabel(div);
  input.addEventListener('focus', () => { comboRender(div, ''); comboOpen(div); });
  input.addEventListener('input', () => { comboRender(div, input.value); comboOpen(div); });
  input.addEventListener('blur', () => { setTimeout(() => { comboClose(div); comboCommit(div); }, 180); });
  drop.addEventListener('mousedown', e => {
    const item = e.target.closest('.ac-item[data-v]');
    if (!item) return;
    e.preventDefault();
    const opt = (COMBO_OPTS[div.dataset.opts] || []).find(o => String(o.v) === item.dataset.v);
    input.value = opt ? opt.label : item.dataset.v;
    comboCommit(div);
    comboClose(div);
  });
}
function initAllCombos(root) {
  (root || document).querySelectorAll('.combo').forEach(initCombo);
}
// Repaint visible labels from the hidden values (after programmatic prefill).
function syncCombos(root) {
  (root || document).querySelectorAll('.combo').forEach(comboSyncLabel);
}
// Switch a combo to a different COMBO_OPTS group at runtime (e.g. cashbook's
// direction select swapping the category combo between categories_income /
// categories_expense). Re-renders the open dropdown in place so a filter the
// user already typed still applies to the new group.
function comboSetGroup(div, optsKey) {
  div.dataset.opts = optsKey;
  if (div.querySelector('.ac-drop').classList.contains('open')) {
    comboRender(div, div.querySelector('.combo-input').value);
  }
}
document.addEventListener('DOMContentLoaded', () => initAllCombos(document));
