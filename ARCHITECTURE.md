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
Cleanup) writes the ENTIRE eligible list to the store's `queue` table in deletion
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
   A file confirmed **physically gone** is also pruned from the library snapshot in
   the same write (`save_pending(..., snapshot_delete_paths=…)`), so a title deleted
   outside MediaReducer doesn't linger as a phantom `movies` row until the next full
   scan. The redline fast path and the full-scan cleanup's external-vanish branch
   prune the snapshot the same way — every no-rescan path that confirms a file is
   gone sheds its row. (Protected-since marks leave the file on disk, so their
   snapshot row stays.)
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
Cleanup button) is locked whenever the saved plan can no longer be trusted:

- **Config changed what gets deleted → the queue reconciles in place, no Simulate.**
  A completed Simulate — or a config-save **reconcile** — stamps the
  deletion-affecting keys (`_PLAN_CONFIG_KEYS`, kept identical in both files) plus
  the monitored paths into the `pending` record. Saving a scoring, filter, or
  threshold change — or a protected-collection or Jellyfin-favorites change — kicks
  a background reconcile (the engine's quiet `reconcile` mode; `_reconcile_after_save`
  → `run_reconcile` → `reconcile_from_snapshot`): it re-scores every movie in the
  stored snapshot, re-applies the shared eligibility ladder (`_hard_filter_reason`,
  the same predicate `build_candidates` uses), re-marks the covering prefix to the
  current target (keeping each mark's delay clock), and re-stamps — so cleanup
  un-ghosts with **no manual Simulate** and no library walk. Pure scoring / filter /
  threshold changes recompute from the snapshot with no server call; a collections /
  favorites change re-fetches the live protected / favorite sets first (fail-closed —
  if that server is unreachable the reconcile is HELD and the Connections check flags
  it, retried automatically once the connection recovers or the server is disabled,
  `_retry_held_reconcile`). `simulate_required` (the stamp mismatch) is now the
  transient state shown until the reconcile lands, or the fallback when there is no
  snapshot yet. Note the two key sets: `_PLAN_CONFIG_KEYS` (what the stamp tracks)
  excludes the protected-collection lists and Radarr on/off — those are honored from
  the standing cache (the 15-min upkeep re-fetches protection and drops newly-protected
  marks; every deletion re-verifies fresh; the queue carries the Radarr identity) —
  while `_RECONCILE_*_KEYS` (what triggers a reconcile) *includes* collections and
  favorites. **Monitored-path changes** still force a manual Simulate: the snapshot
  reflects only the old paths, so it can't be reconciled (compared via the stamped
  `monitor_dirs`).
- **No full scan within the last two days.** A full library scan (a Simulate, or
  the automatic daily Cleanup, or the paused-mode daily maintenance Simulate — all
  rebuild the whole snapshot) must have completed within
  `_FULL_SCAN_MAX_AGE_SECONDS` (48h) — checked live off the snapshot's `built_at`
  (`_full_scan_overdue()`), no persistent flag. A daily scan normally keeps this
  fresh with a full day of slack (so moving `DAILY_RUN_TIME` later never trips
  it); if scans stop for two days (e.g. the APIs are unreachable) the plan ages
  out and cleanup ghosts until a manual Simulate. A completed scan refreshes
  `built_at` and lifts the lock on its own. A **paused** schedule still runs a
  once-a-day maintenance Simulate after `DAILY_RUN_TIME` (`_paused_daily_scan_due`,
  connections permitting) purely to keep this fresh — the plan stays current
  without an armed Cleanup.

Arming automatic mode additionally requires proof a Simulate has run at all —
within satisfied limits the queue can be legitimately empty, so the library
snapshot (written by every completed scan) serves as that evidence
(`_simulate_evidence()`). The manual Cleanup button stays ghosted while every
limit is satisfied regardless. See `_pending_plan_current()` /
`_full_scan_overdue()` (app) / `write_plan_to_queue()` (engine).

**Fail-closed protection** — protected collections (Plex/Jellyfin), identity
mismatches between servers, and (when IMDb is in use) movies with no rating are
*skipped*, never deleted. Any API failure aborts a deleting run rather than
guessing.

## State files (all under `/config`, i.e. `OUTPUT_DIR`)

| File | Written by | Purpose |
| --- | --- | --- |
| `config.json` | app | Saved settings (single source of truth for both processes). |
| `mediareducer.db` | engine + app | SQLite store (`db.py`), four tables: `metadata_cache` (per-movie API facts, so a rescan skips the slow per-movie lookups), `movies` (the **library snapshot** every completed scan rewrites — the Filtering & Scoring table), `queue` (the marked & eligible deletion queue + its plan-currency stamp), and `meta` (kv: **schedule state** — the app burns/reopens the daily window here — plus **storage stats** and the code/schema guards). WAL mode + a `busy_timeout` serialize the engine subprocess and Flask request threads; WAL writes `-wal`/`-shm` sidecars beside the `.db`. **Preserved across a restart** (`validate_store_on_startup`): the plan stays usable, and whether it can still be trusted is decided live at the gate (see **Plan currency**) — a plan-affecting config change, a monitored-path change, or a full scan older than two days locks Cleanup + arming until a fresh Simulate; a completed scan lifts it. Two guards keep it honest across upgrades: the engine's `code_checksum` (hash of `engine.py`) flushes the code-derived **rows** on the next write when the engine changes what it caches, and `db.py`'s `_schema_fingerprint` **rebuilds the tables** at connect when the schema changes — both keep `last_cleanup_date`. |
| `lastrun.log` | engine | Most recent run log (overwritten each run; the app archives the prior one into `logs/` on startup). |
| `logs/` | engine + app | Archived run logs — the engine keeps any run that deleted; the app also archives the last `lastrun.log` here on startup. |
| `deleted.log` | engine (app can truncate) | Deletion history (survives startup); the dashboard's Erase button empties it. |
| `progress.json` | engine (app resets) | Cleanup progress for the dashboard; carried across a restart with the preserved store (reset to "no runs yet" only by an explicit Clear-cache / Reset). |
| `title.ratings.tsv` | engine | IMDb ratings dataset (downloaded when needed). |

The file writes (`config.json`, `progress.json`, the logs) are atomic (temp file
+ `replace()`), so a crash or kill mid-write never leaves a torn file; the
`mediareducer.db` store gets the same guarantee from SQLite transactions (a
crash rolls back to the last commit).

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
