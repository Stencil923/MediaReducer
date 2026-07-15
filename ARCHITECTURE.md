# Architecture

A contributor's map of how MediaReducer fits together. For install/usage see
[README.md](README.md); this doc is about the code.

## The big picture

MediaReducer is two Python processes plus a set of Jinja templates:

```
 browser ──HTTP──▶  app.py  (Flask web server, container PID 1 under tini)
                      │
                      ├─ renders templates/  (dashboard, config, explorer)
                      ├─ reads/writes  /config/config.json
                      ├─ APScheduler tick every 15 min ──┐
                      └─ subprocess.Popen ───────────────┴──▶  engine.py
                                                                 │ scans Plex/Tautulli,
                                                                 │ Jellyfin, Radarr;
                                                                 │ scores; marks/deletes
                                                                 ▼
                                                          movie files on /library
```

- **`app.py`** — the Flask server. Serves the three pages, exposes the JSON API
  the UI polls, launches `engine.py` as a subprocess for every run, runs the
  scheduler that fires automatic Live deletion, gates Live behind
  plan-currency, and builds the sanitized debug report. Run state
  (`_run_active`, `_run_process`, …) is in-memory and resets on restart.
- **`engine.py`** — the deletion engine. A standalone script: it loads the same
  `config.json`, fetches the library from the media APIs, scores every movie,
  and either simulates or performs deletions to satisfy the space limits. It
  never imports `app.py`; they communicate through `config.json`, the state
  files below, and the subprocess exit code.
- **`templates/`** — `base.html` (shared layout, CSS design system, JS helpers)
  plus `dashboard.html`, `config.html`, `deletion_score_explorer.html`.
- **`entrypoint.py`** — container entry: optional PUID/PGID drop, then
  `os.execvp` into `app.py` (so the app is the signalled process; `init: true`
  in compose runs tini as PID 1 to reap the engine and forward SIGTERM).

## Run modes

The engine's behavior is chosen by `RUN_MODE` (from config) or, more often, the
`MEDIAREDUCER_MODE_OVERRIDE` env var the app sets per launch:

| Mode | Trigger | What it does |
| --- | --- | --- |
| `debug_info` | Summary refresh (dashboard/scheduler upkeep) | Status + library-size vs. limits, then exits. No scan, no delete. Quiet (log discarded, no progress events). |
| `debug_sim` | **Simulate** button | Full dry run: scans, scores, logs the ranked candidate list and what *would* be deleted. Writes the marked-for-deletion plan. |
| `headroom` | **Live** run / scheduler tick | Live mode: enforces `HEADROOM_GB`/`REDLINE_GB`/`MAX_LIBRARY_GB`, actually deletes. |
| `sample_pool` | Score Explorer refresh | Builds the sample of real movies the Explorer previews scoring on. Quiet. |

`MEDIAREDUCER_MANUAL=1` marks a manual Live Run (deletes immediately, no delay,
no daily-window gate — the user has just seen the plan).

## A run, end to end

1. UI POSTs `/api/run` (or the scheduler tick calls `run_script()`).
2. The app checks connection health + plan-currency, then `subprocess.Popen(["python3", "engine.py"])` with the mode in the environment. A daemon thread `wait()`s on it.
3. The engine writes `progress.json` as it goes; the dashboard polls `/api/run/progress` and tails `lastrun.log` via `/api/logs/last`.
4. On exit, the app marks progress terminal (done/stopped/error). Live runs that deleted files archive their log under `logs/`.

Only one run at a time — `_run_lock` + `_run_active` reject overlaps. **Stop**
(and a container SIGTERM, forwarded by `_graceful_shutdown`) sends SIGTERM to the
engine, which finishes the file it's on (unlink → `deleted.log`) before exiting.

## Key models

**Scoring** — `compute_retention_score()` in `engine.py` (the module docstring
has the full formula). Higher score = keep. A balance dial splits weight between
watch/added history and IMDb quality; deletion order is score ascending, with
documented tiebreaks. The Score Explorer's JS mirrors this exactly — the
`tests/parity/` check fails if the two drift.

**Deletion delay** (`DELETE_DELAY_DAYS`) — a daily Live run first *marks*
candidates (into `pending_deletions.json`) and only deletes a mark once it has
aged N calendar days. Marks are display-only; a deletion always re-derives
eligibility from a fresh full scan, so a stale mark can never delete a protected
movie. Redline emergencies and manual Live Runs bypass the delay.

**Plan currency** — Live (arming automatic mode or the manual button) is locked
whenever the saved config changed in a way that affects *what* gets deleted. A
completed Simulate stamps the deletion-affecting keys (`_PLAN_CONFIG_KEYS`, kept
identical in both files) plus the monitored paths into `pending_deletions.json`;
if the current config doesn't match the stamp, `simulate_required` is set and
Live ghosts until a new Simulate. See `_pending_plan_current()` (app) /
`write_plan_to_queue()` (engine).

**Fail-closed protection** — protected collections (Plex/Jellyfin), identity
mismatches between servers, and (when IMDb is in use) movies with no rating are
*skipped*, never deleted. Any API failure aborts a deleting run rather than
guessing.

## State files (all under `/config`, i.e. `OUTPUT_DIR`)

| File | Written by | Purpose |
| --- | --- | --- |
| `config.json` | app | Saved settings (single source of truth for both processes). |
| `cache.json` | engine | Movie metadata cache, schedule state, storage stats, Score Explorer sample. |
| `pending_deletions.json` | engine | Marked-for-deletion queue + the plan-currency stamp. |
| `lastrun.log` | engine | Most recent run log (overwritten each run). |
| `logs/` | engine | Archived logs from runs that deleted something. |
| `deleted.log` | engine | Permanent deletion history. |
| `progress.json` | engine | Live run progress for the dashboard. |
| `title.ratings.tsv` | engine | IMDb ratings dataset (downloaded when needed). |

Writes are atomic (temp file + `replace()`), so a crash or kill mid-write never
leaves a torn file.

## Development

```bash
tests/run_tests.sh          # unit + parity — hermetic, no network, fast
tests/run_tests.sh --e2e    # also the browser tests (needs playwright + chromium;
                            # skips cleanly with a message if playwright is absent)
```

- **Unit tests** (`tests/unit/test_*.py`) are standalone scripts run against a
  temp config; each prints `PASS`/`FAIL` and exits non-zero on failure.
- **Parity** (`tests/parity/`) pins the engine's Python scoring against the
  Explorer's JS mirror.
- **e2e** (`tests/e2e/*.mjs`) drive a real Chromium via Playwright against the
  app booted over a mock Tautulli.

Config knobs and per-run overrides come through env vars — `MEDIAREDUCER_CONFIG`
(config path), `MEDIAREDUCER_MODE_OVERRIDE`, `MEDIAREDUCER_MANUAL`,
`MEDIAREDUCER_LIBRARY`, the `*_APPDATA` auto-detect paths, and
`MEDIAREDUCER_TRUSTED_HOSTS` (reverse-proxy Host allow-list).

Comments encode real invariants (fail-closed skips, the plan/mark contract,
ordering rules) — when changing behavior, update the comment with it; when only
touching comments, keep the code identical (the repo's history uses an
AST-diff check to prove comment-only edits).
