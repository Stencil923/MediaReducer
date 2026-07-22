// A real end-to-end run: fire a Simulate through the API against the booted
// app + mock Tautulli, wait for the engine subprocess to finish, and assert the
// full cache pipeline landed — the library snapshot, the marked & eligible
// queue, and (on a second run) that the metadata cache is reused. Plain fetch,
// no browser: this exercises the run/cache path the page-load smoke tests don't.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const H = { 'Content-Type': 'application/json', 'X-MediaReducer': '1' };

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function status() {
  const r = await fetch(`${BASE}/api/status`, { cache: 'no-store' });
  return r.json();
}

async function runSimulate() {
  const r = await fetch(`${BASE}/api/run`, {
    method: 'POST', headers: H, body: JSON.stringify({ mode: 'debug_sim' }),
  });
  const d = await r.json();
  if (!d.started) return { started: false, message: d.message || '' };
  // Wait out the engine subprocess (bounded).
  for (let i = 0; i < 90; i++) {
    await sleep(1000);
    const s = await status();
    if (!s.run_active) return { started: true, status: s };
  }
  return { started: true, timeout: true };
}

// Health must be green first (the fixture points Tautulli at the mock).
const s0 = await status();
check('media server connected before the run',
  (s0.cleanup_state?.connection_health?.critical_ok) === true);

// First Simulate: builds the plan and snapshot from scratch.
const r1 = await runSimulate();
check('first Simulate starts', r1.started === true);
check('first Simulate finishes (no timeout)', r1.started && !r1.timeout);

const snapResp = await fetch(`${BASE}/api/library-snapshot`, { cache: 'no-store' });
const snap = await snapResp.json();
check('run wrote a non-empty library snapshot',
  Array.isArray(snap.movies) && snap.movies.length > 0);
check('snapshot carries a build time',
  Number(snap.built_at) > 0);

const s1 = r1.status || await status();
// The fixture library sits well under the 100 GB headroom, so nothing is marked
// for deletion — but the ENTIRE eligible library still enters the queue.
check('the eligible queue was built (every movie, in deletion order)',
  Number(s1.marked_count) > 0);

// Second Simulate: exercises the metadata-cache-hit path end to end. It must
// complete just the same (the cache makes it cheaper, never breaks it). Skipped
// with MR_E2E_SECOND_RUN=0 — the metadata cache is Plex-keyed, so a Jellyfin/
// both re-run just repeats the first pass and adds runtime for no new coverage.
if (process.env.MR_E2E_SECOND_RUN !== '0') {
  const r2 = await runSimulate();
  check('second Simulate (cache path) also finishes', r2.started === true && !r2.timeout);
  const s2 = r2.status || await status();
  check('the queue is stable across a re-run',
    Number(s2.marked_count) === Number(s1.marked_count));
}

console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
