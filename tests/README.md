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
| `test_threshold_matrix` | Every (mode, headroom, redline, cap) combination gets the same verdict from all three validators — the `/api/config` save handler, the hand-edit file validator, and the engine — and valid states gate Cleanup/Simulate the right way |
| `test_redline_only` | Redline-only mode (`REDLINE_ONLY_MODE` + a Redline floor): validation rules, the always-on Simulate/plan gate, the standing preview queue |
| `test_redline_fastpath` | The shared incremental queue-delete path (`_redline_fast_path`), used by BOTH a Redline emergency and a manual Cleanup: with a current plan it deletes down the marked queue in plan order — re-verifying monitored roots and protections fresh — and falls back to a full scan on any doubt. Also: a manual-style call (`do_radarr=True`) forgets each deleted movie in Radarr from the TMDB id + section the queue stores, while a Redline emergency skips Radarr; and `write_plan_to_queue` persists that identity |
| `test_optional_value_memory` | Disabled optional fields keep their last entered value across saves/restarts; unticking Headroom stores 0 and is valid as long as a Redline floor and/or a Library Size Cap is armed to drive cleanup; a blank enabled field is rejected rather than read as a silent 0 |
| `test_delete_delay` | Deletion-delay config validation (whole days), queue composition, plan currency (raw-stamp + candidate-config comparison), and the save reconciliation: a threshold-changing save that satisfies every limit unschedules clocks but keeps the queue |
| `test_time_zone` | `TIME_ZONE` drives the process clock — daily-run midnight, deletion-delay aging, log timestamps — with `auto` meaning the container clock |
| `test_deleted_log` | deleted.log parser across lines with and without the optional rationale fields; the why surfaces in history lines |
| `test_radarr_cleanup` | Radarr forgets a movie the moment its copy in Radarr's section is deleted (duplicates elsewhere don't block it); a copy known to be in a different section never touches Radarr, and only unknown-section rows fall back to the Radarr-owns-folder match |
| `test_media_server_integration` | The Plex / Jellyfin / Radarr integrations over their REAL HTTP request functions, against an in-process localhost mock (`tests/mocks/mock_services.py`): a protected Plex collection and a Jellyfin favorite resolve to on-disk movie paths, a section-match deletion actually issues the Radarr DELETE, and an unreachable Plex aborts a deleting run — the URL/auth/parse layer the monkeypatched tests skip |
| `test_source_merge` | The Plex + Jellyfin source merge (`get_all_movies`): the same file on both servers collapses to one candidate with summed plays, oldest added date, most-recent last-played, unioned protection, and distinct-users = the higher of the two (never the sum); single-server passthrough; and a same-filename path divergence is flagged as an unreconciled twin and skipped, not double-counted or wrongly deleted |
| `test_reverify_marked` | The incremental marked-queue re-verify (no full scan): on fresh watch data a marked movie a recent watch lifted out of the delete set is un-marked and the next eligible (re-checked fresh) is backfilled in its place, covering count held; the one-way safety belt (`max(last-scan, fetched)`) means a failed/partial fetch can only spare a movie, never doom it; and the redline delete gate (`_confirmed_unwatched`) only lets an emergency delete a movie every id of which read back fresh and unwatched — a watched, partially-verifiable, or id-less movie is spared |
| `test_debug_mode` | Debug mode (a no-delete diagnostic state) and Debug Cleanup: config validation makes Debug mode and Automatic Cleanup mutually exclusive; the engine guarantees `debug_cleanup` never deletes (`delete_candidate` dry-runs, and the redline fast path — which unlinks with no per-file gate — bails in any debug mode); `/api/run` requires Debug mode for `debug_cleanup`, refuses a real Cleanup while Debug mode is on, and runs `debug_cleanup` past the 15% safety cap via the Simulate gate |
| `test_startup_invalidation` | Every startup drops cache.json (the library snapshot/metadata AND the marked & eligible queue under its `pending` key) plus any legacy pending_deletions.json — files can change while the app is down — while keeping deletion history and the last run's log. With no cached plan `simulate_required` is True and the Debug Cleanup button is ghosted, so a fresh Simulate is required after a restart |
| `test_debug_live_tick` | Debug Cleanup is "Cleanup minus deletion": it mirrors a real cleanup tick from the standing marked queue in cache — no full scan, and it DELETES NOTHING, but it DOES apply and persist the same marked-queue upkeep a Cleanup does (drop gone/newly-protected marks, re-score) before only PREVIEWING the deletions. `_debug_cleanup_delete_preview` is fully read-only — walks the queue in plan order with the SAME protection + fresh-watch re-verify as the real redline fast path and reports the covering prefix (sparing since-watched/protected, reporting when the queue can't cover), never unlinking or writing deleted.log. `_debug_cleanup_from_queue` runs the real (persisting) upkeep end to end: a case proves it drops a vanished mark from the queue on disk while still deleting no real file and writing no deleted.log |
| `test_reverify_tick` | The tick wiring end to end: `_revalidate_pending_marks` (the 15-min Summary maintenance) re-SIZES the marked set to the current headroom/cap deficit — a watched marked movie drops out and the next in line is marked in its place, a bigger deficit marks more, the fresh plays persist to the snapshot (built_at preserved) — SKIPS the re-select in redline-only mode (no delay clocks there), and unschedules everything at a zero deficit. Real pending file + cache snapshot, stubbed fetch/protections |
| `test_tautulli_refresh` | The library scan forces a Tautulli media-info refresh (`refresh=true` on each section's first page) so a recently-added, already-watched movie missing from Tautulli's stale cache is still returned with its real play history — the bug where such a movie became a 0-play Jellyfin-only row and was marked for deletion. Models Tautulli's stateful cache; fails if the refresh is ever dropped |
| `test_jellyfin_fetch` | `get_all_movies_from_jellyfin()` over a canned `_jellyfin_request`: Jellyfin's per-user/bits-per-second/ISO-8601 shapes normalize to Tautulli-shaped rows (bitrate→kbps, resolution, provider ids, DateCreated→epoch); plays SUM across users while last-played takes the most recent and distinct-watchers count Played-with-zero-plays; BoxSet protection applies by movie id, IMDb id, and TMDb id; a missing protected BoxSet fails closed |
| `test_engine_helpers` | Engine internals no scenario test drives: Tautulli intra-source dedup (highest plays / newest last-played / Radarr section preserved), the config coercion helpers (numbers incl. non-finite rejection, string lists, extensions, booleans, library-path normalization), `compute_config_hash` metadata-source sensitivity, and the IMDb pipeline (`_bounded_gunzip` decompression-bomb caps, `_load_imdb_ratings_from_disk` header/row validation, `imdb_dataset_needed`) |
| `test_app_coverage` | App-layer safety the scenarios skip: `/api/run`'s missing/garbled-mode → Simulate safety default (never a real deletion) plus the unknown-mode 400 and run-active 409 guards; run-log section/error extraction (COMPLETED-WITH-ERRORS report served whole, content sections stop before it, early ABORT → synthetic RUN FAILED); and the hand-editable pending-queue's hostile-input guards (garbage size / out-of-range epoch read as safe defaults, never a 500) |
| `test_run_context_log` | Every run (Simulate, Cleanup, Debug Cleanup) opens its log with the shared **RUN CONTEXT** header — the configured space mode, each target's armed value + breach deficit, filesystem/library state, and which breached target set the free-space goal — checked across headroom, redline, cap, combo, and redline-only |
| `test_candidate_sources` | The candidate stage (`build_candidates`) under all three server configurations — Plex-only, Jellyfin-only, both — driving the real filter/protection/merge branches that no other test reaches: a protected Plex collection and a protected Jellyfin BoxSet each exclude their movie, a Jellyfin favorite is excluded only with favorites-protection on, the same file on both servers collapses to ONE candidate with summed plays, and a cross-server provider-id conflict (or an unmerged twin) is skipped, never deleted |
| `test_live_button_state` | The Cleanup button ghosts when space limits are satisfied while Simulate always stays available (it maintains the standing queue); fail open on unknowns; real problems keep their tooltips |
| `test_protection_failclosed` | A configured protected collection matching nothing aborts deleting runs, warns-and-continues in the quiet Summary, and proceeds normally on a real match |
| `test_safety_autopause` | A cleanup tick with unsafe thresholds pauses Automatic Cleanup with the reason; safe ticks still run |
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
- `e2e_debugghost.mjs` — ticking Debug mode on the Config page ghosts the Scheduler
  Mode → Automatic Cleanup option immediately (card styling + a reason naming Debug
  mode, not just a bare `disabled`), and unticking restores the exact pre-debug state
- `e2e_prune_confirm.mjs` — the Config page's save-time "this will prune — save again
  to confirm" guard fires for Headroom and Redline the same way it does for the
  Library Size Cap: setting either to a value the disk is already past prompts the
  second-save confirm in Paused mode too (not only when arming Automatic Cleanup), while an
  unchanged breached threshold on an unrelated save does not nag
- `e2e_debuglive_btn.mjs` — on a Debug-mode dashboard the Cleanup button is the yellow
  Debug Cleanup, which uses the Simulate gate: it stays enabled through a status
  poll that reports a real Cleanup blocked by the safety percentage (the poll used to
  re-block it), yet still disables on a real hard config error AND ghosts with a "run
  Simulate first" reason when no current plan exists (it replays the queue a Simulate
  builds). Also covers the running
  visuals: an active `debug_cleanup` run shows the header badge and run pill reading
  "Debugging" in yellow (not the red "Running" of a real Cleanup) and keeps the button
  ghosted for the duration. Runs against its own Debug-mode app instance (`DEBUG_MODE=true`,
  isolated `OUTPUT_DIR`).

## Environment knobs

| Var | Purpose |
|---|---|
| `PLAYWRIGHT_MODULE` | import specifier/path for playwright (default `playwright`) |
| `PW_CHROMIUM` | explicit chromium binary for playwright |
| `MR_E2E_PORT` | base app port for the opt-in tiers (default 5057; +1/+2 also used) |
| `MR_E2E_SECOND_RUN` | `0` skips `e2e_fullrun`'s second (cache-reuse) Simulate |
