// Bug regression (dashboard): in Debug mode the Live button becomes the yellow
// "Debug Cleanup", which uses the SIMULATE gate — it deletes nothing, so it must
// ignore the live/safety thresholds. The subtle bug: the server rendered the
// button correctly, but the /api/status poll re-applied the button state from the
// LIVE fields (_applyCleanupState), so a few seconds after load the poll re-blocked
// Debug Cleanup on a safety-percentage target and slapped the safety tooltip on
// it. Here we stub /api/status to report simulate-ok + live-blocked-by-safety and
// prove the button stays live through the poll; then flip simulate to blocked and
// prove it DOES disable (so it really tracks the simulate gate, not "always on").
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

// Mutable stub: start with a real Live block by the safety percentage, which a
// real Cleanup would honor but Debug Cleanup must not.
let stub = {
  simulate_disabled: false, simulate_tooltip: '',
  cleanup_disabled: true, cleanup_tooltip: 'Redline floor is over the safety percentage.',
  // The Debug Cleanup button binds to debug_disabled/debug_tooltip: it ignores
  // the live/safety block but ghosts until a Simulate has built the queue.
  debug_disabled: false, debug_tooltip: '',
  safety_blocked: true,
  run_active: false, run_debug_cleanup: false,
};
await p.route('**/api/status**', async route => {
  // Guard the whole handler: a poll in flight when the page navigates/closes
  // makes fetch()/fulfill() reject, which would otherwise crash the test.
  try {
    const resp = await route.fetch();
    const d = await resp.json();
    d.cleanup_state = d.cleanup_state || {};
    d.cleanup_state.summary_disabled = false;
    d.cleanup_state.simulate_disabled = stub.simulate_disabled;
    d.cleanup_state.simulate_tooltip = stub.simulate_tooltip;
    d.cleanup_state.cleanup_disabled = stub.cleanup_disabled;
    d.cleanup_state.cleanup_tooltip = stub.cleanup_tooltip;
    d.cleanup_state.debug_disabled = stub.debug_disabled;
    d.cleanup_state.debug_tooltip = stub.debug_tooltip;
    d.cleanup_state.space_thresholds = Object.assign({}, d.cleanup_state.space_thresholds,
      { safety_blocked: stub.safety_blocked });
    d.run_active = stub.run_active;
    d.run_cleanup = false;
    d.run_debug_cleanup = stub.run_debug_cleanup;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(d) });
  } catch (_) {
    try { await route.continue(); } catch (_) { /* page gone — ignore */ }
  }
});
// Feed a debug_cleanup "running" progress once the stub marks a run active, so the
// run pill (renderProgress) updates like it would during a real Debug Cleanup.
await p.route('**/api/run/progress**', async route => {
  try {
    if (!stub.run_active) { await route.continue(); return; }
    const prog = { schema: 1, status: 'running', phase: 'scanning', mode: 'debug_cleanup',
                   scanned: 10, total: 100, eligible: 0, protected: 0, skipped: 0,
                   deleted: 0, bytes_freed: 0, target_bytes: 0, trigger: '',
                   message: 'Scanning…', started_at: 1 };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(prog) });
  } catch (_) {
    try { await route.continue(); } catch (_) { /* page gone — ignore */ }
  }
});

// The dashboard polls status/progress/logs continuously, so 'networkidle' is
// flaky under load — 'domcontentloaded' is enough (the page renders server-side
// and we wait for a poll below). Retry the navigation: under the full suite the
// app can still be warming up / the box is busy, and the first goto may refuse.
let navErr;
for (let i = 0; i < 5; i++) {
  try {
    await p.goto('' + BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForSelector('#btn-cleanup', { timeout: 15000 });
    navErr = null;
    break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load the dashboard:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

const snap = () => p.evaluate(() => {
  const live = document.getElementById('btn-cleanup');
  const wrap = live?.closest('[data-hover-tip]');
  return {
    label: (live?.textContent || '').trim(),
    warning: !!live?.classList.contains('btn-outline-warning'),
    disabled: !!live?.disabled,
    tip: wrap?.getAttribute('data-hover-tip') || '',
  };
});

// Wait for at least one status poll to run _applyCleanupState (the moment the bug
// used to strike). The button must remain enabled with no safety tooltip.
await p.waitForTimeout(4500);
const overSafety = await snap();

// Now a genuine hard config error (blocks even Simulate): the Debug Cleanup
// button MUST disable — proving it tracks the simulate gate, not "always on".
stub = { simulate_disabled: true, simulate_tooltip: 'Fix Space Thresholds first.',
         cleanup_disabled: true, cleanup_tooltip: 'Fix Space Thresholds first.',
         debug_disabled: true, debug_tooltip: 'Fix Space Thresholds first.', safety_blocked: false };
await p.waitForFunction(() => document.getElementById('btn-cleanup')?.disabled === true, { timeout: 12000 })
  .catch(() => {});
const hardError = await snap();

// No current plan (simulate_required): Debug Cleanup ghosts with a "run
// Simulate first" reason, even though there's no config error and no safety block.
stub = { simulate_disabled: false, simulate_tooltip: '', cleanup_disabled: false, cleanup_tooltip: '',
         debug_disabled: true, debug_tooltip: 'Run Simulate first — Debug Cleanup replays the marked & eligible queue a Simulate builds.',
         safety_blocked: false };
await p.waitForFunction(() =>
  /run simulate first/i.test(document.getElementById('btn-cleanup')?.closest('[data-hover-tip]')?.getAttribute('data-hover-tip') || ''),
  { timeout: 12000 }).catch(() => {});
const noPlan = await snap();

// ── Running visuals: a Debug Cleanup in progress reads "Debugging" in yellow ──
// Clear the hard error (so the button is only ghosted by the RUN, not the config)
// and mark a debug_cleanup run active.
stub = { simulate_disabled: false, simulate_tooltip: '', cleanup_disabled: true,
         cleanup_tooltip: 'Redline floor is over the safety percentage.', safety_blocked: true,
         run_active: true, run_debug_cleanup: true };
await p.waitForFunction(() =>
  (document.querySelector('.site-header .run-badge-label')?.textContent || '').trim() === 'Debugging',
  { timeout: 12000 }).catch(() => {});
const running = await p.evaluate(() => {
  const h = document.querySelector('.site-header');
  const pill = document.getElementById('rp-pill');
  const live = document.getElementById('btn-cleanup');
  return {
    headerLabel: (h?.querySelector('.run-badge-label')?.textContent || '').trim(),
    headerDebug: !!h?.classList.contains('is-debug-run'),
    headerCleanup: !!h?.classList.contains('is-cleanup-run'),
    pillLabel: (pill?.textContent || '').trim(),
    pillWarn: !!pill?.classList.contains('is-warn'),
    pillCleanup: !!pill?.classList.contains('is-live'),
    cleanupDisabled: !!live?.disabled,
  };
});

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };

check('the Live button is the yellow Debug Cleanup', overSafety.warning && /debug cleanup/i.test(overSafety.label));
check('Debug Cleanup stays ENABLED through a status poll that blocks Live on the safety percentage',
  !overSafety.disabled);
check('Debug Cleanup shows no safety-percentage tooltip', !/safety percentage/i.test(overSafety.tip));
check('a real hard config error (blocks Simulate) DOES disable Debug Cleanup',
  hardError.disabled && /fix space thresholds/i.test(hardError.tip));
check('Debug Cleanup ghosts with a "run Simulate first" reason when no current plan exists',
  noPlan.disabled && /run simulate first/i.test(noPlan.tip));
check('an active Debug Cleanup shows the header badge "Debugging" in yellow (not red Live)',
  running.headerLabel === 'Debugging' && running.headerDebug && !running.headerCleanup);
check('an active Debug Cleanup shows the run pill "Debugging" in yellow (not red Running)',
  running.pillLabel === 'Debugging' && running.pillWarn && !running.pillCleanup);
check('the Debug Cleanup button stays ghosted while the run is in progress',
  running.cleanupDisabled);
check('no JS errors', errs.length === 0);
console.log('running:', JSON.stringify(running));
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0, 3)));
console.log('overSafety:', JSON.stringify(overSafety), 'hardError:', JSON.stringify(hardError));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
