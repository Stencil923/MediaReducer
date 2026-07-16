// Engine <-> Score Explorer scoring parity: replays the grid produced by
// gen_py_scores.py through the page's ACTUAL JS (extracted from the
// template) and fails on any drift > 0.01 points.
// Usage: node parity_check.cjs <dir-with-scoring.json-and-py_scores.json>
const { readFileSync } = require('fs');
const path = require('path');

const dataDir = process.argv[2] || '.';
const repoRoot = path.join(__dirname, '..', '..');
const html = readFileSync(path.join(repoRoot, 'templates', 'deletion_score_explorer.html'), 'utf8');
const SCORING = JSON.parse(readFileSync(path.join(dataDir, 'scoring.json'), 'utf8'));
const py = JSON.parse(readFileSync(path.join(dataDir, 'py_scores.json'), 'utf8'));

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
let CFG = { BAL: 0, STALE: 36, CUTOFF: null };
function gcfg() { return CFG; }
function grab(re, name) { const m = html.match(re); if (!m) throw new Error('missing ' + name); return m[0]; }
eval(grab(/function balanceWeights\(bal\)\{[\s\S]*?\n\}/, 'balanceWeights'));
eval(grab(/function voteConfidence\(votes\)\{[\s\S]*?\n\}/, 'voteConfidence'));
eval(grab(/function retentionBreakdown\(m,cfg\)\{[\s\S]*?\n  return\{breakdown:b,retention\};\n\}/, 'retentionBreakdown'));

let worst = 0, fails = 0;
for (const bal of [0, 50]) {
  CFG = { BAL: bal, STALE: 36, CUTOFF: null };
  for (const age of [900, 1500, 2000]) {
    for (const u of [0, 1, 2, 4, 6]) {
      const played = u > 0;
      const m = { playCount: played ? 1 : 0, lastPlayedDays: played ? age : null,
                  addedDays: age, users: u, rating: 6.5, votes: 50000 };
      const { breakdown: b, retention } = retentionBreakdown(m, CFG);
      const p = py[String(bal)][`${age}d/${u}u`];
      const d = Math.max(Math.abs(retention - p.score),
                         Math.abs(b.recency - p.recency),
                         Math.abs((b.shelf || 0) - p.shelf));
      worst = Math.max(worst, d);
      if (d > 0.01) {
        fails++;
        console.log(`MISMATCH bal=${bal} ${age}d/${u}u js=${retention.toFixed(3)} py=${p.score}`);
      }
    }
  }
}
console.log(`max diff: ${worst.toFixed(5)} | mismatches: ${fails}`);
console.log('RESULT:', fails === 0 ? 'PASS' : 'FAIL');
process.exit(fails === 0 ? 0 : 1);
