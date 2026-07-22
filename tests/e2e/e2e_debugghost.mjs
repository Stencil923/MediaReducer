// Bug regression: on the Config page, ticking Debug mode must ghost the
// Scheduler Mode → Live option IMMEDIATELY, with a visible reason — using
// the same .run-mode-disabled card styling + desc-headroom reason text the
// server-side gates use. (The bug: Live ghosted only after a click, via a raw
// `disabled` toggle with no card styling and no reason.) Toggling the checkbox
// exercises the exact sync() → _updateRunModeAvailability() path that also runs
// on page load, so this covers the on-load ghost too.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

await p.goto('' + BASE + '/config', { waitUntil: 'networkidle' });
await p.waitForTimeout(600);

const snap = () => p.evaluate(() => {
  const card = document.getElementById('mode-card-headroom');
  const live = document.getElementById('mode-headroom');
  const desc = document.getElementById('desc-headroom');
  return {
    ghosted: !!card?.classList.contains('run-mode-disabled'),
    cleanupDisabled: !!live?.disabled,
    reason: (desc?.textContent || '').trim(),
    enabledText: desc?.dataset.enabledText || '',
  };
});

const before = await snap();

// Tick Debug mode (same code path as loading a page whose saved config already
// has Debug mode on).
await p.evaluate(() => {
  const dbg = document.getElementById('DEBUG_MODE');
  dbg.checked = true;
  dbg.dispatchEvent(new Event('change', { bubbles: true }));
});
await p.waitForTimeout(150);
const on = await snap();

// Untick — Live must return to its normal (server-gated) availability.
await p.evaluate(() => {
  const dbg = document.getElementById('DEBUG_MODE');
  dbg.checked = false;
  dbg.dispatchEvent(new Event('change', { bubbles: true }));
});
await p.waitForTimeout(150);
const off = await snap();

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };

check('with Debug mode on, Live is ghosted (card styled + input disabled), not just after a click',
  on.ghosted && on.cleanupDisabled);
check('the ghost shows a reason naming Debug mode',
  /debug mode/i.test(on.reason) && on.reason !== on.enabledText);
// Turning Debug off must restore the exact pre-debug state — whatever it was.
// (This fixture already ghosts Live for a real headroom-safety reason, so we
// compare to `before` rather than assume Live becomes enabled.)
check('turning Debug mode off restores the pre-debug Live state (no debug reason left over)',
  off.ghosted === before.ghosted && off.cleanupDisabled === before.cleanupDisabled
  && off.reason === before.reason && !/debug mode/i.test(off.reason));
check('no JS errors', errs.length === 0);
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0, 3)));
console.log('before:', JSON.stringify(before));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
