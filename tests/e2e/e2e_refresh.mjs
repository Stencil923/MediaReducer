const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
const posts = [];  // {url, t}
p.on('pageerror', e => errs.push('PAGEERR: ' + e.message));
p.on('request', r => {
  if (r.method() === 'POST') {
    const u = r.url();
    if (u.includes('/api/score-config') || u.includes('/api/score-sample/refresh')) {
      posts.push({ url: u.replace(/^https?:\/\/[^/]+/, ''), t: Date.now() });
    }
  }
});

await p.goto('' + BASE + '/explorer', { waitUntil: 'networkidle' });
await p.waitForTimeout(800);

const balBefore = await p.$eval('#c-bal', el => el.value);

// Move the dial all the way to watch-history (0) and fire the input handler.
await p.$eval('#c-bal', el => {
  el.value = '0';
  el.dispatchEvent(new Event('input', { bubbles: true }));
});
await p.waitForTimeout(300);
const saveDirty = await p.$eval('#btn-cfg-save', el => !el.disabled);

// Click Refresh — should save the dial first, then rebuild.
await p.click('#btn-sample-refresh');

// Wait for the flow to settle (refresh button re-enabled).
for (let i = 0; i < 60; i++) {
  const busy = await p.$eval('#btn-sample-refresh', el => el.disabled || el.classList.contains('btn-busy'));
  if (!busy && i > 2) break;
  await p.waitForTimeout(300);
}
await p.waitForTimeout(500);

const cfg = await p.evaluate(() => fetch('/api/config', { cache: 'no-store' }).then(r => r.json()));

const scIdx = posts.findIndex(x => x.url.includes('/api/score-config'));
const rfIdx = posts.findIndex(x => x.url.includes('/api/score-sample/refresh') && !x.url.includes('status'));

console.log('dial before:', balBefore, '| save became dirty:', saveDirty);
console.log('POST order:', JSON.stringify(posts.map(x => x.url)));
console.log('saved balance after:', cfg.SCORE_BALANCE);
console.log('JS errors:', JSON.stringify(errs));

const orderedOk = scIdx !== -1 && rfIdx !== -1 && posts[scIdx].t <= posts[rfIdx].t;
const pass = saveDirty && orderedOk && Number(cfg.SCORE_BALANCE) === 0 && errs.length === 0;
console.log('CHECK save-before-refresh:', orderedOk);
console.log('CHECK saved balance == 0:', Number(cfg.SCORE_BALANCE) === 0);
console.log('RESULT:', pass ? 'PASS' : 'FAIL');
await b.close();
process.exit(pass ? 0 : 1);
