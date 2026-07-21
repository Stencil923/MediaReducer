# MediaReducer tests

```bash
tests/run_tests.sh                # unit + scoring-parity (hermetic, no network, no browser)
tests/run_tests.sh --integration  # + the full run pipeline over real HTTP under every
                                  # server profile (Plex / Jellyfin / both). No browser.
tests/run_tests.sh --e2e          # everything --integration does + the browser page tests
```

Three tiers, cheapest first. `--integration` boots a real app + mock servers
and drives the scan→score→queue pipeline over `fetch` — no browser, so it needs
only python + node and runs anywhere. `--e2e` adds the two chromium page tests
on top (`smoke_all`, `e2e_runlock`), which are the only tests that need
playwright.

## What runs

**Unit** (`tests/unit/`, plain Python scripts — each prints `PASS`/`FAIL`
lines and exits non-zero on failure):

| Test | Guards |
|---|---|
| `test_library_snapshot` | The library snapshot survives engine cache clears and interrupted runs; completed scans replace it; the app reads it back |
| `test_config_cache` | `load_config()`'s mtime-keyed memo is invisible: every call returns an isolated copy, a file write invalidates it, `_CONFIG_FILE_ISSUES` still surfaces, and the missing-file onboarding state is stable |
| `test_mark_score_refresh` | Re-Simulating under a new balance keeps each mark's age (`marked_at`) but refreshes its displayed score/title/size |
| `test_threshold_matrix` | Every (mode, headroom, redline, cap) combination gets the same verdict from all three validators — the `/api/config` save handler, the hand-edit file validator, and the engine — and valid states gate Live/Simulate the right way |
| `test_redline_only` | Redline-only mode (`REDLINE_ONLY_MODE` + a Redline floor): validation rules, the always-on Simulate/plan gate, the standing preview queue |
| `test_redline_fastpath` | A Redline emergency with a current plan deletes down the marked queue in plan order — re-verifying monitored roots and protections fresh — and falls back to a full scan on any doubt |
| `test_optional_value_memory` | Disabled optional fields keep their last entered value across saves/restarts; unticking Headroom stores 0 and requires a Redline floor (redline-only mode); zero rejected where disabling is the off switch |
| `test_delete_delay` | Deletion-delay config validation (whole days), queue composition, plan currency (raw-stamp + candidate-config comparison), and the save reconciliation: a threshold-changing save that satisfies every limit unschedules clocks but keeps the queue |
| `test_time_zone` | `TIME_ZONE` drives the process clock — daily-run midnight, deletion-delay aging, log timestamps — with `auto` meaning the container clock |
| `test_deleted_log` | deleted.log parser across lines with and without the optional rationale fields; the why surfaces in history lines |
| `test_radarr_cleanup` | Radarr forgets a movie the moment its copy in Radarr's section is deleted (duplicates elsewhere don't block it); a copy known to be in a different section never touches Radarr, and only unknown-section rows fall back to the Radarr-owns-folder match |
| `test_media_server_integration` | The Plex / Jellyfin / Radarr integrations over their REAL HTTP request functions, against an in-process localhost mock (`tests/mocks/mock_services.py`): a protected Plex collection and a Jellyfin favorite resolve to on-disk movie paths, a section-match deletion actually issues the Radarr DELETE, and an unreachable Plex aborts a deleting run — the URL/auth/parse layer the monkeypatched tests skip |
| `test_source_merge` | The Plex + Jellyfin source merge (`get_all_movies`): the same file on both servers collapses to one candidate with summed plays, oldest added date, most-recent last-played, unioned protection, and distinct-users = the higher of the two (never the sum); single-server passthrough; and a same-filename path divergence is flagged as an unreconciled twin and skipped, not double-counted or wrongly deleted |
| `test_jellyfin_fetch` | `get_all_movies_from_jellyfin()` over a canned `_jellyfin_request`: Jellyfin's per-user/bits-per-second/ISO-8601 shapes normalize to Tautulli-shaped rows (bitrate→kbps, resolution, provider ids, DateCreated→epoch); plays SUM across users while last-played takes the most recent and distinct-watchers count Played-with-zero-plays; BoxSet protection applies by movie id, IMDb id, and TMDb id; a missing protected BoxSet fails closed |
| `test_engine_helpers` | Engine internals no scenario test drives: Tautulli intra-source dedup (highest plays / newest last-played / Radarr section preserved), the config coercion helpers (numbers incl. non-finite rejection, string lists, extensions, booleans, library-path normalization), `compute_config_hash` metadata-source sensitivity, and the IMDb pipeline (`_bounded_gunzip` decompression-bomb caps, `_load_imdb_ratings_from_disk` header/row validation, `imdb_dataset_needed`) |
| `test_app_coverage` | App-layer safety the scenarios skip: `/api/run`'s missing/garbled-mode → Simulate safety default (never a live deletion) plus the unknown-mode 400 and run-active 409 guards; run-log section/error extraction (COMPLETED-WITH-ERRORS report served whole, content sections stop before it, early ABORT → synthetic RUN FAILED); and the hand-editable pending-queue's hostile-input guards (garbage size / out-of-range epoch read as safe defaults, never a 500) |
| `test_candidate_sources` | The candidate stage (`build_candidates`) under all three server configurations — Plex-only, Jellyfin-only, both — driving the real filter/protection/merge branches that no other test reaches: a protected Plex collection and a protected Jellyfin BoxSet each exclude their movie, a Jellyfin favorite is excluded only with favorites-protection on, the same file on both servers collapses to ONE candidate with summed plays, and a cross-server provider-id conflict (or an unmerged twin) is skipped, never deleted |
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

**Integration + E2E** (`tests/e2e/`): both tiers boot the mock Tautulli and
mock Jellyfin (`tests/mocks/`), which serve the SAME disposable library tree,
config, and ratings TSV built by `tests/fixtures/make_fixtures.py` — nothing
outside the temp dir is touched, and the fixture's IMDb URL points at a dead
port so any accidental network fetch fails loudly.

Integration (`--integration`, plain `fetch`, no browser — needs only `node`):

- `e2e_fullrun.mjs` — a real Simulate runs to completion and writes the library
  snapshot + eligible queue. Run against a fresh app instance under **each
  server profile** — `e2e_fullrun_plex`, `e2e_fullrun_jellyfin`, and
  `e2e_fullrun_both` (`make_fixtures.py <dir> plex|jellyfin|both`) — so the
  whole scan→score→queue pipeline is exercised with Plex/Tautulli only, Jellyfin
  only, and both servers merging. Plex also does a second Simulate to prove the
  metadata-cache-reuse path (`MR_E2E_SECOND_RUN=0` skips it for the others,
  whose cache is Plex-keyed).

Browser (`--e2e` only, needs playwright + chromium — the sole browser tests):

- `smoke_all.mjs` — all three pages load with zero JS errors
- `e2e_runlock.mjs` — Filtering & Scoring locks/unlocks with run state

## Environment knobs

| Var | Purpose |
|---|---|
| `PLAYWRIGHT_MODULE` | import specifier/path for playwright (default `playwright`) |
| `PW_CHROMIUM` | explicit chromium binary for playwright |
| `MR_E2E_PORT` | base app port for the opt-in tiers (default 5057; +1/+2 also used) |
| `MR_E2E_SECOND_RUN` | `0` skips `e2e_fullrun`'s second (cache-reuse) Simulate |
