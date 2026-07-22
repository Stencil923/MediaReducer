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
  scheduler that fires automatic Cleanup deletions, gates cleanup behind
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
| `headroom` | **Cleanup** button / scheduler tick | Enforces `HEADROOM_GB`/`REDLINE_GB`/`MAX_LIBRARY_GB` and actually deletes — both the manual Cleanup and the automatic (scheduler) run. |
| `debug_cleanup` | **Debug Cleanup** button (Debug mode) | "Cleanup minus deletion": runs the marked-queue upkeep from cache and PERSISTS it (drops gone/protected marks, refreshes plays/scores in the queue + snapshot) exactly like a cleanup tick, then only PREVIEWS what it would delete. Never unlinks a file or trims the queue for deletions. |

`MEDIAREDUCER_MANUAL=1` marks a manual Cleanup (deletes immediately, no delay,
no daily-window gate — the user has just seen the plan).

## A run, end to end

1. UI POSTs `/api/run` (or the scheduler tick calls `run_script()`).
2. The app checks connection health + plan-currency, then `subprocess.Popen(["python3", "engine.py"])` with the mode in the environment. A daemon thread `wait()`s on it.
3. The engine writes `progress.json` as it goes; the dashboard polls `/api/run/progress` and tails `lastrun.log` via `/api/logs/last`.
4. On exit, the app marks progress terminal (done/stopped/error); the engine itself archives the log under `logs/` when a run deleted files.

Only one run at a time — `_run_lock` + `_run_active` reject overlaps. **Stop**
(and a container SIGTERM, forwarded by `_graceful_shutdown`) sends SIGTERM to the
engine, which finishes the file it's on (unlink → `deleted.log`) before exiting.

## Key models

**Scoring** — `compute_retention_score()` in `engine.py` (the module docstring
has the full formula). Higher score = keep. A balance dial splits weight between
watch/added history and IMDb quality; deletion order is score ascending, with
documented tiebreaks. The Score Explorer's JS mirrors this exactly — the
`tests/parity/` check fails if the two drift.

**Space thresholds** — `HEADROOM_GB` (0 = trigger off) and `MAX_LIBRARY_GB`
share the once-per-day window + `DAILY_RUN_TIME` and the deletion delay; either
alone is a valid setup (cap-only included). `REDLINE_GB` fires immediately on
any 15-minute tick and frees only back to its own floor; while Headroom is
ticked it must sit strictly below the headroom value. `REDLINE_ONLY_MODE` (the
GUI's Headroom checkbox unticked) turns the headroom trigger off; it is valid as
long as a Redline floor and/or a Library Size Cap is armed (headroom value 0).
TRUE redline-only — a Redline floor with NO cap — makes Redline the only trigger,
retires the delay, and has Simulate maintain a standing queue of every eligible
movie in deletion order; a cap armed alongside instead keeps running on the daily
schedule with the delay (`_redline_only_mode()` returns False in that case). With a current plan, a Redline breach takes a
fast path: it deletes straight down the marked queue — re-verifying monitored
roots, protected collections, and Jellyfin favorites fresh — instead of a full
rescan, then a background Simulate rebuilds the preview. **File size
optimization is honored in EVERY delete path** — the fast path (redline
emergency and manual Cleanup), the full scan (daily and manual), and the Debug
Cleanup preview all pick via the same `_pop_next_deletion`, re-applied against
the live remaining target: when what's left to free lands inside a group of
near-tied-score movies, the cheapest cover goes (the smallest-scoring single
file that covers, else the largest tied file), so one big movie can spare
several small near-ties even after a since-watched spare shifts the target.

**Marked & eligible queue** — every full plan (Simulate or a daily/manual
Cleanup) writes the ENTIRE eligible list to cache.json's `pending` key in deletion
order. Only the prefix covering the current space targets is *marked*
(`marked_at` set — the deletion-delay clock); the rest is merely *eligible*
(`marked_at` null), visible order that starts a fresh clock only if it is ever
marked. A Simulate that finds nothing eligible still writes its stamped
(empty) plan — that is a real answer, not a missing one. Satisfied limits stop
the clocks but keep the queue, on both sides: the engine's 15-minute upkeep
(`_revalidate_pending_marks`) and a config save that changes a threshold into
satisfied territory (`_unschedule_pending_marks`, reported as
`pending_unscheduled` — and only a save that actually changed a threshold
touches the clocks, so a stale cached library size can never reset them).

**Summary maintenance (the 15-minute pipeline)** — the quiet Summary keeps the
whole cache accurate between daily full scans, and every cached-queue cleanup run
(a manual Cleanup, a Debug Cleanup) runs the SAME pipeline as its pre-check, then
acts on the result — so even though Headroom/Library-Cap only *delete* at their
daily trigger, the marked set, scores, and disk numbers stay current every 15
minutes. The pipeline, in order:

1. **Refresh filesystem capacity + library size** and persist them (`emit_stats`).
2. **Drop dead marks** — files that are gone, or that joined a protected collection.
3. **Re-size the marked set to the CURRENT headroom/cap deficit**
   (`_daily_deficit_bytes`) from the cached queue, no full scan: mark the
   File-size-optimized covering set (the SAME `_pop_next_deletion` a real delete
   uses), scored on FRESH watch data. A movie a recent watch lifted out of the set
   is dropped and the next in line — re-checked fresh before it's trusted — takes
   its place; the set GROWS or SHRINKS with the deficit (a newly-marked movie starts
   its delay clock now, a dropped one loses its clock). Within limits (or
   redline-only, or no deficit) nothing is scheduled — the clocks stop but the queue
   stays as the standing eligible order.
4. The fresh watch data is **persisted in ONE atomic write**
   (`save_pending(..., snapshot_watch_updates=…)`): each re-checked movie's belt-max
   plays/last-played/favorite into the library snapshot and its refreshed score into
   the queue entry, so the queue and snapshot can never drift apart on a half-failure
   (the snapshot's `built_at` is preserved — a watch refresh, not a rescan).
5. The marked list **displays in deletion-delay order** (soonest first) for
   headroom/cap.

A **Debug Cleanup runs this exact pipeline and persists it** — "Cleanup minus
deletion": it refreshes the cache like a real run, then only PREVIEWS what it would
delete (deletes nothing, trims nothing). The daily full scan is the one path that
rebuilds the whole queue + snapshot from a fresh library scan instead (adding
newly-added movies); when it carries a still-marked movie forward it refreshes that
entry's score/title too, so the marked prefix never keeps a stale score against a
freshly-rewritten snapshot. Redline emergencies delete straight from the queue and
skip the pipeline (the last tick already maintained it).

**Radarr cleanup** — fires the moment the deleted file is the copy in Radarr's
own section, regardless of surviving duplicates elsewhere. A copy KNOWN to be
in a different section never triggers it; only rows with unknown section
identity fall back to matching Radarr's folder against the deleted one. The
Redline fast path skips Radarr cleanup entirely (its queue entries carry no
TMDB/section identity).

**Deletion delay** (`DELETE_DELAY_DAYS`) — a daily Cleanup deletes a mark only
once its clock has aged N calendar days. Marks never authorize a deletion on
their own; a deletion re-verifies eligibility fresh (the full scan, or the
Redline fast path's own protection re-fetch), so a stale mark can never delete
a protected movie. Redline emergencies and manual Cleanups bypass the delay.

**Plan currency** — cleanup (arming automatic mode or pressing the manual
Cleanup button) is locked whenever the saved config changed in a way that
affects *what* gets deleted. A completed Simulate stamps the deletion-affecting
keys (`_PLAN_CONFIG_KEYS`, kept identical in both files) plus the monitored
paths into that `pending` record; if the current config doesn't match the
stamp, `simulate_required` is set and cleanup ghosts until a new Simulate.
Arming automatic mode additionally requires proof a Simulate has run at all —
within satisfied limits the queue can be legitimately empty, so the library
snapshot (written by every completed scan) serves as that evidence
(`_simulate_evidence()`). The manual Cleanup button stays ghosted while every
limit is satisfied regardless. See `_pending_plan_current()` (app) /
`write_plan_to_queue()` (engine).

**Fail-closed protection** — protected collections (Plex/Jellyfin), identity
mismatches between servers, and (when IMDb is in use) movies with no rating are
*skipped*, never deleted. Any API failure aborts a deleting run rather than
guessing.

## State files (all under `/config`, i.e. `OUTPUT_DIR`)

| File | Written by | Purpose |
| --- | --- | --- |
| `config.json` | app | Saved settings (single source of truth for both processes). |
| `cache.json` | engine + app | Movie metadata cache, schedule state (the app burns/reopens the daily window under a shared flock), storage stats, the library snapshot every completed scan rewrites (the Filtering & Scoring table), and (under `pending`) the marked & eligible queue + plan-currency stamp. Cleared on startup so a restart requires a fresh Simulate. |
| `lastrun.log` | engine | Most recent run log (overwritten each run; the app archives the prior one into `logs/` on startup). |
| `logs/` | engine + app | Archived run logs — the engine keeps any run that deleted; the app also archives the last `lastrun.log` here on startup. |
| `deleted.log` | engine (app can truncate) | Deletion history (survives startup); the dashboard's Erase button empties it. |
| `progress.json` | engine (app resets) | Cleanup progress for the dashboard; reset to "no runs yet" on startup alongside the cache. |
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
