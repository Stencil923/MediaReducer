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
| `test_library_snapshot` | The library snapshot survives engine cache clears and interrupted runs; completed scans replace it; the app reads it back |
| `test_mark_score_refresh` | Re-Simulating under a new balance keeps each mark's age (`marked_at`) but refreshes its displayed score/title/size |
| `test_threshold_matrix` | Every (mode, headroom, redline, cap) combination gets the same verdict from all three validators — the `/api/config` save handler, the hand-edit file validator, and the engine — and valid states gate Live/Simulate the right way |
| `test_redline_only` | Redline-only mode (`REDLINE_ONLY_MODE` + a Redline floor): validation rules, the always-on Simulate/plan gate, the standing preview queue |
| `test_redline_fastpath` | A Redline emergency with a current plan deletes down the marked queue in plan order — re-verifying monitored roots and protections fresh — and falls back to a full scan on any doubt |
| `test_optional_value_memory` | Disabled optional fields keep their last entered value across saves/restarts; unticking Headroom stores 0 and requires a Redline floor (redline-only mode); zero rejected where disabling is the off switch |
| `test_delete_delay` | Deletion-delay config validation (whole days), queue composition, plan currency (raw-stamp + candidate-config comparison), and the save reconciliation: a threshold-changing save that satisfies every limit unschedules clocks but keeps the queue |
| `test_time_zone` | `TIME_ZONE` drives the process clock — daily-run midnight, deletion-delay aging, log timestamps — with `auto` meaning the container clock |
| `test_deleted_log` | deleted.log parser across lines with and without the optional rationale fields; the why surfaces in history lines |
| `test_radarr_cleanup` | Radarr forgets a movie the moment its copy in Radarr's section is deleted (duplicates elsewhere don't block it); a copy known to be in a different section never touches Radarr, and only unknown-section rows fall back to the Radarr-owns-folder match |
| `test_live_button_state` | Live ghosts when space limits are satisfied while Simulate always stays available (it maintains the standing queue); fail open on unknowns; real problems keep their tooltips |
| `test_protection_failclosed` | A configured protected collection matching nothing aborts deleting runs, warns-and-continues in the quiet Summary, and proceeds normally on a real match |
| `test_safety_autopause` | A Live tick with unsafe thresholds pauses Live with the reason; safe ticks still run |
| `test_graceful_shutdown` | SIGTERM to the app forwards the stop to the engine child and waits for it to exit before the app does |
| `test_progress_phases` | Each progress step fills 0→100 exactly once; Plex+Jellyfin path resolution reports under the indeterminate "library" step |
| `test_debug_report` | The sanitized debug report carries the decision-state sections and never leaks movie names, paths, or IPs |

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

## Environment knobs

| Var | Purpose |
|---|---|
| `PLAYWRIGHT_MODULE` | import specifier/path for playwright (default `playwright`) |
| `PW_CHROMIUM` | explicit chromium binary for playwright |
| `MR_E2E_PORT` | app port for e2e (default 5057) |
