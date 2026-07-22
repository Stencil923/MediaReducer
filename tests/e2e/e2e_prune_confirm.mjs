// The Config page's save-time "this will prune — save again to confirm" guard.
// A Library Size Cap below the current library has always warned (even in Paused
// mode). This test locks in that Headroom and Redline do the SAME: setting either
// to a value the disk is already past prompts the same second-save confirm, in
// Paused mode too — not only when arming Automatic Cleanup. Drives the pure _immediatePruneWarning
// in the real page realm (real _diskStats), so it's deterministic.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

let navErr;
for (let i = 0; i < 5; i++) {
  try {
    await p.goto('' + BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForFunction(() => typeof _immediatePruneWarning === 'function', { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /config:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

const R = await p.evaluate(() => {
  const total = Number(_diskStats?.total_gb) || 1000;
  const free = Number(_diskStats?.free_gb) || 500;
  const big = total + free + 1000;   // guaranteed to breach either free-space threshold
  const base = { RUN_MODE: 'paused', HEADROOM_GB: 0, REDLINE_GB: null,
                 MAX_LIBRARY_GB: null, DELETE_DELAY_DAYS: 0 };
  const savedRun = _savedConfig, savedLib = _lastKnownLibraryGb, savedMarks = _markedWaitingAges;
  _markedWaitingAges = null;
  const call = (saved, cfg, nowCleanup) => { _savedConfig = saved; return _immediatePruneWarning(cfg, !!nowCleanup); };
  try {
    return {
      // Headroom CHANGED into a breach, still Paused → warns like the cap does.
      headPaused: call({ ...base }, { ...base, HEADROOM_GB: big }, false),
      // Redline CHANGED into a breach, still Paused → warns.
      redPaused:  call({ ...base }, { ...base, REDLINE_GB: big }, false),
      // Unchanged breached Redline on an unrelated Paused save → must NOT nag.
      redUnchanged: call({ ...base, REDLINE_GB: big }, { ...base, REDLINE_GB: big }, false),
      // Arming Live over an already-breached (unchanged) Headroom → warns (imminent).
      headArmCleanup: (() => { _lastKnownLibraryGb = null;
        return call({ ...base, HEADROOM_GB: big, RUN_MODE: 'paused' },
                    { ...base, HEADROOM_GB: big, RUN_MODE: 'headroom' }, true); })(),
    };
  } finally {
    _savedConfig = savedRun; _lastKnownLibraryGb = savedLib; _markedWaitingAges = savedMarks;
  }
});

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };

check('Headroom changed into a breach warns in Paused mode (next Cleanup will prune)',
  /Headroom target/.test(R.headPaused) && /next Cleanup will prune/.test(R.headPaused) && /Save again to confirm/.test(R.headPaused));
check('Redline changed into a breach warns in Paused mode (next Cleanup will free)',
  /Redline floor/.test(R.redPaused) && /next Cleanup will free/.test(R.redPaused) && /Save again to confirm/.test(R.redPaused));
check('an unchanged breached threshold on an unrelated save does NOT nag',
  R.redUnchanged === '');
check('arming Automatic Cleanup over an already-breached Headroom still warns (imminent wording)',
  /Headroom target/.test(R.headArmCleanup) && /within ~15 minutes/.test(R.headArmCleanup));
check('no JS errors', errs.length === 0);
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0, 3)));
console.log('R:', JSON.stringify(R));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
