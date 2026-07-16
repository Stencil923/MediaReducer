const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);

const phase = process.argv[2]; // 'nofile' | 'file'
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [], posts = [];
p.on('pageerror', e => errs.push('PAGEERR: ' + e.message));
p.on('request', r => {
  if (r.method() === 'POST') posts.push(r.url().replace(/^https?:\/\/[^/]+/, ''));
});

await p.goto('' + BASE + '/explorer', { waitUntil: 'networkidle' });
await p.waitForTimeout(1000);

// Drag the balance dial to the IMDb side WITHOUT saving.
await p.$eval('#c-bal', el => {
  el.value = '50';
  el.dispatchEvent(new Event('input', { bubbles: true }));
});
await p.waitForTimeout(600);

const note1 = await p.$eval('#sample-imdb-note', el => ({ shown: el.style.display !== 'none', text: el.textContent }));

if (phase === 'file') {
  // Auto-reload should be running — wait for it to settle.
  for (let i = 0; i < 40; i++) {
    const busy = await p.evaluate(() => typeof refreshBusy !== 'undefined' && refreshBusy);
    if (!busy && i > 2) break;
    await p.waitForTimeout(400);
  }
  await p.waitForTimeout(600);
}

const state = await p.evaluate(() => ({
  ratedRows: raw.filter(m => Number.isFinite(m.rating) && m.rating > 0).length,
  totalRows: raw.length,
  noteShown: document.getElementById('sample-imdb-note').style.display !== 'none',
  noteText: document.getElementById('sample-imdb-note').textContent,
}));
const cfg = await p.evaluate(() => fetch('/api/config', { cache: 'no-store' }).then(r => r.json()));

const scPosts = posts.filter(u => u.includes('/api/score-config')).length;
const rfPosts = posts.filter(u => u.includes('/api/score-sample/refresh') && !u.includes('status')).length;

console.log('phase:', phase);
console.log('note right after drag:', JSON.stringify(note1));
console.log('final: rated', state.ratedRows + '/' + state.totalRows, '| note shown:', state.noteShown);
console.log('final note text:', JSON.stringify(state.noteText.slice(0, 90)));
console.log('POSTs — score-config:', scPosts, '| sample-refresh:', rfPosts);
console.log('saved balance still:', cfg.SCORE_BALANCE);
console.log('JS errors:', JSON.stringify(errs));

let pass;
if (phase === 'nofile') {
  pass = note1.shown && /never been downloaded/.test(note1.text)
    && scPosts === 0 && rfPosts === 0
    && state.ratedRows === 0
    && Number(cfg.SCORE_BALANCE) === 0 && errs.length === 0;
} else {
  pass = note1.shown && /found on the server/.test(note1.text)
    && scPosts === 0 && rfPosts === 1
    && state.ratedRows > 0 && !state.noteShown
    && Number(cfg.SCORE_BALANCE) === 0 && errs.length === 0;
}
console.log('RESULT:', pass ? 'PASS' : 'FAIL');
await b.close();
process.exit(pass ? 0 : 1);
