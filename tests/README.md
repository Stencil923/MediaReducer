# MediaReducer tests

```bash
tests/run_tests.sh          # unit + scoring-parity (hermetic, no network, no browser)
tests/run_tests.sh --e2e    # + browser end-to-end against a real app instance
```

## What runs

**Unit** (`tests/unit/`, plain Python scripts — each prints `PASS`/`FAIL`
lines and exits non-zero on failure):

| Test | Guards |
|---|---|
| `test_sample_merge` | Dual-source (Plex+Jellyfin) sample merge is order-independent, including the twin-never-scanned case |
| `test_sample_annotate` | IMDb dataset gating: annotate from an on-disk file at any balance, download only when scoring needs it |
| `test_score_config_trigger` | Sample rebuild fires exactly on IMDb-needed crossings of `/api/score-config` saves |
| `test_refresh_flow` | Refresh commits the dial first; no download at 100% watch history; builds dedupe |
| `test_delete_delay` | Deletion-delay config validation (whole days, blank = 0) and marked-for-deletion queue composition |
| `test_deleted_log` | deleted.log parser across every line generation; rationale fields surface in history lines |
| `test_live_button_state` | Simulate/Live ghost when space limits are satisfied; fail open on unknowns; real problems keep their tooltips |
| `test_optional_value_memory` | Disabled optional fields keep their last entered value across saves/restarts; blank Headroom saves as 0; zero rejected where disabling is the off switch |
| `test_protection_failclosed` | A configured protected collection matching nothing aborts deleting runs; sample builds warn |
| `test_safety_autopause` | A Live tick with unsafe thresholds pauses Live with the reason; safe ticks still run |
| `test_scheduler_pause` | Sample builds freeze the background clock; ticks defer; clock restarts after |

**Parity** (`tests/parity/`): `gen_py_scores.py` scores a balance × age ×
distinct-users grid through the real engine; `parity_check.cjs` replays the
same grid through the Score Explorer's actual JS (extracted from the
template) and fails on drift > 0.01 points. This is the guard for the
"engine and preview must never disagree" invariant.

**E2E** (`tests/e2e/`, needs `node` + playwright + chromium): boots the mock
Tautulli (`tests/mocks/`) and a real app instance against a disposable
library tree, config, and ratings TSV built by `tests/fixtures/make_fixtures.py`
— nothing outside the temp dir is touched, and the fixture's IMDb URL points
at a dead port so any accidental network fetch fails loudly.

- `smoke_all.mjs` — all three pages load with zero JS errors
- `e2e_runlock.mjs` — Filtering & Scoring locks/unlocks with run state
- `e2e_annotate.mjs nofile|file` — unrated-sample banners and the on-disk
  dataset auto-annotate flow
- `e2e_refresh.mjs` — Refresh saves a moved dial before rebuilding

## Environment knobs

| Var | Purpose |
|---|---|
| `PLAYWRIGHT_MODULE` | import specifier/path for playwright (default `playwright`) |
| `PW_CHROMIUM` | explicit chromium binary for playwright |
| `MR_E2E_PORT` | app port for e2e (default 5057) |
