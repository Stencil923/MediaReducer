const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
let pass = true;
for (const path of ['/', '/config', '/explorer']) {
  const p = await b.newPage();
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  try {
    const resp = await p.goto(BASE + path, { waitUntil: 'networkidle', timeout: 20000 });
    await p.waitForTimeout(1200);
    const status = resp ? resp.status() : 0;
    const bodyLen = (await p.evaluate(() => document.body.innerText.length));
    const nan = await p.evaluate(() => document.body.innerText.includes('NaN'));
    const ok = status === 200 && errs.length === 0 && bodyLen > 100 && !nan;
    console.log(`${ok ? 'PASS' : 'FAIL'} ${path} status=${status} jsErrors=${errs.length} bodyLen=${bodyLen} NaN=${nan}`);
    if (errs.length) console.log('   errors:', JSON.stringify(errs.slice(0, 3)));
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL ${path}: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── Phone-width sanity: no page may scroll horizontally at common smartphone
// widths, and the dashboard's Cleanup Targets word-values (e.g. "Disabled")
// must not break mid-word into a second line. 320 = smallest common (SE-class),
// 360 = most common Android, 390 = iPhone 12-15.
for (const w of [320, 360, 390]) {
  const ctx = await b.newContext({ viewport: { width: w, height: 780 } });
  for (const path of ['/', '/config', '/explorer']) {
    const p = await ctx.newPage();
    try {
      await p.goto(BASE + path, { waitUntil: 'load', timeout: 20000 });
      await p.waitForTimeout(700);
      const overflow = await p.evaluate(() =>
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth);
      const ok = overflow <= 1;
      console.log(`${ok ? 'PASS' : 'FAIL'} ${path}@${w} horizontal-overflow=${overflow}px`);
      pass = pass && ok;
      if (path === '/') {
        // Each Cleanup Targets value must sit on ONE line (a mid-word break
        // like "Disable d" doubles the element's height past one line-box).
        const wrapped = await p.evaluate(() => {
          const bad = [];
          for (const id of ['target-row-headroom', 'target-row-redline']) {
            const v = document.querySelector('#' + id + ' .value');
            if (!v) continue;
            const lh = parseFloat(getComputedStyle(v).lineHeight) || 24;
            if (v.clientHeight > lh * 1.6) bad.push(id + ':' + v.textContent.trim());
          }
          return bad;
        });
        const vok = wrapped.length === 0;
        console.log(`${vok ? 'PASS' : 'FAIL'} cleanup-target values single-line@${w}${vok ? '' : ' — ' + wrapped.join(', ')}`);
        pass = pass && vok;
      }
    } catch (e) {
      console.log(`FAIL ${path}@${w}: ${e.message}`);
      pass = false;
    }
    await p.close();
  }
  await ctx.close();
}
await b.close();
console.log('RESULT:', pass ? 'PASS' : 'FAIL');
process.exit(pass ? 0 : 1);
