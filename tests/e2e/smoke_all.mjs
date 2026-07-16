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
await b.close();
console.log('RESULT:', pass ? 'PASS' : 'FAIL');
process.exit(pass ? 0 : 1);
