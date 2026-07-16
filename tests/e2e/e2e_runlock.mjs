const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

// Proxy /api/status so we can flip run_active without a real run.
let fakeRunActive = false;
await p.route('**/api/status**', async route => {
  const resp = await route.fetch();
  const d = await resp.json();
  d.run_active = fakeRunActive;
  await route.fulfill({ response: resp, json: d });
});

await p.goto('' + BASE + '/explorer', { waitUntil: 'networkidle' });
await p.waitForTimeout(800);

const snap = () => p.evaluate(() => ({
  ghost: document.getElementById('filter-score-card')?.classList.contains('section-run-ghost'),
  noteHidden: document.getElementById('exp-run-lock-note')?.hidden,
  balDisabled: document.getElementById('c-bal')?.disabled,
  graceDisabled: document.getElementById('c-grace')?.disabled,
  cutoffDisabled: document.getElementById('c-cutoff')?.disabled,
  cutoffOn: document.getElementById('c-cutoff-on')?.checked,
  saveDisabled: document.getElementById('btn-cfg-save')?.disabled,
  refreshDisabled: document.getElementById('btn-sample-refresh')?.disabled,
}));

const before = await snap();

// Flip to run-active and wait for the 4s status poll to deliver it.
fakeRunActive = true;
await p.waitForFunction(() => document.getElementById('filter-score-card')?.classList.contains('section-run-ghost'), { timeout: 12000 });
const locked = await snap();

// Make the form dirty attempt: move dial while locked (disabled input ignores events)
const balBefore = await p.$eval('#c-bal', el => el.value);

// Flip back and wait for unlock.
fakeRunActive = false;
await p.waitForFunction(() => !document.getElementById('filter-score-card')?.classList.contains('section-run-ghost'), { timeout: 12000 });
const after = await snap();

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };
// Save is disabled on a CLEAN form by design — only inputs/ghost/note matter here.
check('starts unlocked (no ghost, note hidden, inputs enabled)',
  !before.ghost && before.noteHidden && !before.balDisabled && !before.graceDisabled && !before.refreshDisabled);
check('locks on run: ghost + note shown', locked.ghost && !locked.noteHidden);
check('locks inputs (bal, grace)', locked.balDisabled && locked.graceDisabled);
check('locks Save and Refresh', locked.saveDisabled && locked.refreshDisabled);
check('unlocks after run: ghost off, note hidden', !after.ghost && after.noteHidden);
check('inputs re-enabled', !after.balDisabled && !after.graceDisabled);
check('cutoff input follows its toggle after unlock', after.cutoffDisabled === !after.cutoffOn);
check('no JS errors', errs.length === 0);
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0,3)));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
