# MediaReducer

MediaReducer is a Dockerized web app for keeping large movie libraries under
control. It reads your library from Plex/Tautulli, Jellyfin, or both, scores
every movie by watch history and IMDb rating, and deletes the lowest-value
files first when your storage thresholds are crossed.

It is a normal Docker Compose app and runs anywhere the container can see your
movie files at `/library`.

> **This app deletes movie files — there is no recycle bin.** Stay on Paused
> and use Simulate until the output matches exactly what you expect before
> ever using Live.

> **Do not expose the web UI to the internet.** There is no login — anyone who
> can reach port 7474 can reconfigure MediaReducer and delete your media. Keep
> it on your LAN or behind a VPN. (It does block the common browser-based
> attacks a malicious website could aim at your LAN, but that is a backstop,
> not a reason to put it online.) If you reach it through a reverse-proxy
> domain, list that domain in `MEDIAREDUCER_TRUSTED_HOSTS`.

## What It Does

- Reads your library from Plex/Tautulli, Jellyfin, or both.
- Only ever touches the `/library` folders you tell it to manage.
- Leaves alone anything in a protected collection, recently added, unplayed,
  highly rated, or marked a Jellyfin favorite (the last four are optional).
- Scores the rest on watch history and IMDb rating, blended by a single dial,
  and deletes the lowest scores first.
- Kicks in on a free-space target, an emergency floor, or a library size cap.
- Lets you preview all of it against a live sample of your real library before
  anything runs.
- Shows progress, logs, storage, and a permanent deletion history on the
  Dashboard.
- Can tell Radarr to unmonitor a movie once its last copy is gone.

## Requirements

- Docker Compose (or Unraid's Compose Manager).
- A movie library mounted into the container at `/library`.
- Plex with Tautulli, Jellyfin, or both:
  - Plex mode requires Tautulli. A Plex URL + token additionally unlock
    protected Plex collections.
  - Jellyfin mode requires a Jellyfin URL + API key.
- Internet access from the container for the IMDb ratings dataset.
- Radarr is optional.

## Path Requirement

MediaReducer deletes files from its own `/library` mount. Plex, Tautulli, and
Jellyfin may report different path prefixes, but each movie's path suffix must
line up with a real file under `/library`.

This is OK:

```text
MediaReducer: /library/movies/Bob (2020)/Bob.mkv
Plex:         /data/movies/Bob (2020)/Bob.mkv
Jellyfin:     /data/library/movies/Bob (2020)/Bob.mkv
```

This is not OK:

```text
MediaReducer: /library/movies/Bob (2020)/Bob.mkv
Plex:         /downloads/Bob.mkv
```

If the suffix cannot be matched, the health check blocks setup and run
controls. Click **Check for Errors** after fixing mounts.

## Install

Clone or extract this project on your server:

```bash
git clone https://github.com/Stencil923/MediaReducer.git /mnt/user/appdata/MediaReducer
cd /mnt/user/appdata/MediaReducer
```

Copy the example environment file and edit it for your paths:

```bash
cp .env.example .env
```

```env
PLEX_LIBRARY_PATH=/mnt/user/media
TAUTULLI_APPDATA=/mnt/user/appdata/tautulli
RADARR_APPDATA=/mnt/user/appdata/radarr
JELLYFIN_APPDATA=/mnt/user/appdata/jellyfin
MEDIAREDUCER_DATA=/mnt/user/appdata/mediareducer
```

`PLEX_LIBRARY_PATH` is the root folder that contains the movie folders you want
available to MediaReducer. For example, if your movies are at
`/mnt/user/media/Movies`, set `PLEX_LIBRARY_PATH` to `/mnt/user/media` and add
`Movies` as a monitored path in the UI.

Start the container and open the web UI:

```bash
docker compose up -d --build
```

```text
http://your-server-ip:7474
```

## Docker Volumes

| Container path | Purpose |
| --- | --- |
| `/library` | Movie files MediaReducer may scan and delete from. |
| `/config` | MediaReducer config, logs, cache, IMDb data, and deletion history. |
| `/tautulli` | Tautulli appdata for Auto Detect and health checks. |
| `/jellyfin` | Jellyfin appdata for Auto Detect and health checks. |
| `/radarr` | Radarr appdata for Auto Detect and optional cleanup. |

The `/library` mount must be writable for Live runs to delete files.

## Running Outside Docker

You don't have to use Docker. MediaReducer is just `app.py` (the web UI) and
`engine.py` (the worker); run `python3 app.py` and it serves on port 7474 (the
same port the Docker image publishes, so the URL is identical either way). The
five paths it expects default to the container mounts above, but each can point
anywhere via an environment variable:

| Variable | Default | Points at |
| --- | --- | --- |
| `MEDIAREDUCER_LIBRARY` | `/library` | Your movie library — the only place it can delete from. |
| `MEDIAREDUCER_CONFIG` | `/config/config.json` | Where config and state are written. |
| `MEDIAREDUCER_TAUTULLI_APPDATA` | `/tautulli` | Tautulli's config folder (for Auto Detect). |
| `MEDIAREDUCER_RADARR_APPDATA` | `/radarr` | Radarr's config folder. |
| `MEDIAREDUCER_JELLYFIN_APPDATA` | `/jellyfin` | Jellyfin's config folder. |

For example, point it at a library on a NAS:

```bash
MEDIAREDUCER_LIBRARY=/mnt/tank/media \
MEDIAREDUCER_CONFIG=~/.mediareducer/config.json \
python3 app.py
```

The library path stays the deletion boundary wherever you put it — monitored
folders still have to live inside it. The appdata paths are only for
auto-detecting API keys; if you'd rather just type your URLs and keys into the
UI, you can skip them. These are set once at startup, not from the web UI.

## Container User & Health

The container runs as root by default, which works on any host. To run as a
specific user instead, set `PUID`/`PGID` in the compose file — that user needs
write access to your movie files and `/config`. On Unraid that's usually
`PUID=99` / `PGID=100` (`nobody:users`); on other Linux hosts use your own
user's ids (`id -u` / `id -g`, commonly `1000`/`1000`). Deletions then happen
with exactly that user's permissions.

The image has a built-in health check, so `docker ps` and the Unraid dashboard
show it as `healthy` rather than just `running`.

## First-Time Setup

On first launch the web UI shows a welcome guide with a quick start and safety
disclaimers. Reopen it any time with the **?** button in the header.

Work through the Configuration tab from top to bottom.

### 1. Automatic Run Mode

Leave this on **Paused** while setting up. Paused only disables scheduled Live
cleanup — manual Dashboard actions still work once setup is valid. Live stays
locked until a monitored path is saved and the health check passes, and
switching to it takes a second confirming click.

### 2. Connections

Pick your server software — Plex, Jellyfin, or both if they point at the same
files. Both start off, and nothing works until you enable one and its API
connects. **Auto Detect API Keys** fills the keys in from the mounted appdata;
add anything it misses. You can usually leave the URL fields blank — MediaReducer
uses your server's detected address on its standard port, and assumes `http://`.
Fill one in only if yours is non-standard.

The API key is the on/off switch for each service. Leave an optional service's
key blank and that integration just stays off.

- Tautulli — required for Plex; default port 8181.
- Plex token — optional, unlocks protected Plex collections; default port 32400.
- Jellyfin — required for Jellyfin; default port 8096.
- Radarr — optional; default port 7878.

Hit **Check for Errors** and MediaReducer confirms it can reach each API, read
its appdata, write to `/config`, and match your server's paths to real files
under `/library`. A broken connection never blocks a save — the save goes
through and the features that need that connection lock instead. When something
is failing, the Configuration tab turns red and jumps you to the bad fields.
If a server's API fails the check (or its key is blank), that server is turned
off automatically; fix it and turn it back on.

### 3. Movie Library Paths

Add the `/library` folders MediaReducer is allowed to manage:

```text
Movies
Kids Movies
Holiday Movies
```

An empty list means MediaReducer manages nothing — intentional and safe, but
Space Thresholds and run controls stay locked until at least one monitored
path is saved.

When Plex or Jellyfin is connected, this section also shows protected
collection pickers. Selected collections are always skipped. If a selected
collection disappears from the server, it is dropped from the saved config.

When Radarr is connected, **Optional Radarr cleanup** appears here as a single
checkbox — the matching Plex section is detected automatically. When enabled,
MediaReducer unmonitors a movie in Radarr only after deleting the last managed
copy. It never asks Radarr to delete files.

### 4. Space Thresholds

Space Thresholds unlock after a monitored path is saved.

- **Headroom target** — cleanup runs when free space drops below this amount,
  freeing back up to it. 0 (the default) turns just this trigger off — the
  Redline floor and Library Size Cap still work on their own, and with nothing
  armed at all Live stays blocked until you set a target. Unticking the
  checkbox is different: that switches to **redline-only mode** (below), which
  requires a Redline floor.
- **Redline emergency floor** — optional immediate-cleanup floor. When free
  space drops below it, cleanup runs on the next tick and frees only enough to
  get back to the floor (not up to the headroom target). It re-scores your
  library and deletes lowest-value first, so it clears the already-marked movies
  (which are the lowest-value ones) in order — and if you have since changed
  monitored paths, filters, or scoring, it follows that updated order. It must
  sit strictly below the Headroom value — for a Redline at or above it, use
  redline-only mode instead.
- **Library Size Cap** — optional cap on the total size of monitored movie
  files. Shares the daily cleanup schedule and the deletion delay, and works
  with the Headroom value at 0 (cap-only setups) — it's only unavailable in
  redline-only mode.

- **Deletion delay** — whole days a movie stays marked before a daily
  cleanup deletes it (minimum 1: never the same day it's marked — the earliest
  is the next day's run; Redline and manual Live Runs ignore it).

### Redline-only mode

For people who don't want the headroom concept at all and just want a big list
of what will be deleted when space runs low: set a **Redline floor**, then
untick **Headroom target** (the checkbox is the mode switch — a Redline floor
must exist first). In this mode the Library Size Cap and the deletion delay
are retired — Redline is the only thing that ever deletes.

Simulate is still required before Live can be enabled (automatic or the manual
button): it marks at least the first 50 movies in deletion order as a standing
preview — the list Redline will work down, worst-scored first, whenever free
space hits the floor. The preview refreshes on each Simulate, goes stale (and
re-locks Live) when you change monitored paths, filters, or scoring, and each
entry in the deletion history modal reads "#N — deletes when Redline hits."

Optional fields can't be set to 0 — disabling is the off switch. A disabled
field keeps its last value (greyed out), even across restarts.

The Dashboard's storage numbers are cached and refresh every 15 minutes, after
config saves, and whenever you hit the ↻ button — but every run re-checks the
real numbers before it does anything.

If a change would make MediaReducer delete more the next time it runs — say, a
cap below your current library size, or lowering the deletion delay so movies
already marked come due sooner — a persistent notice explains exactly what will
happen and the save takes a second, confirming click. Anything that can delete
right away takes two deliberate clicks.

### 5. Advanced

IMDb dataset settings, display/time settings, log retention, cache tools,
debug mode, the headroom safety cap, and **Reset MediaReducer**.

## Filtering & Scoring

The Filtering & Scoring tab holds every rule that decides *what* can be
deleted and *in what order*, and previews all of it against a live sample of
your real library. Changes show up in the preview as you make them; **Save**
keeps them.

### Eligibility filters

- **Minimum age (grace period)** — movies added within this many days are
  skipped.
- **Don't delete unplayed movies** — optional; skips anything with no play
  history.
- **Maximum IMDb rating** — optional; movies rated above the cutoff are never
  deleted.
- **Jellyfin favorites** (Jellyfin setups) — optional; movies any user
  favorited are skipped.
- **No IMDb data** (always on) — movies with no IMDb rating or votes are
  skipped automatically: half the scoring evidence is missing, so there is
  not enough data to judge them.

Protected collections also affect eligibility; they are configured on the
Configuration tab.

### Scoring & Ordering

Every eligible movie gets a score from 0 to 100 — higher means keep, and the
lowest scores delete first. The score blends two things:

- **Watch history** — how often it's been played, how recently, and by how
  many people. A movie that's never been watched still gets "recency" credit
  from when it was added, so a recent addition isn't judged as harshly as one
  that's sat unwatched for years. **Max staleness** (in Scoring & Ordering)
  sets how long that takes — a movie unwatched longer than this scores as
  fully stale (default 36 months).
- **IMDb rating** — the rating, weighted by how many votes back it up. A big
  vote count on its own won't save a movie nobody watches.

The dial starts centered at an even 50/50; slide it toward watch history or
IMDb. If your library has little play history, lean on IMDb; if it has a lot,
lean on watch history.

Deletions go in score order, lowest first. **File size optimization** (on by
default) tweaks that only at the boundary where the choice actually matters:
when the space you still need to free falls inside a group of near-tied
movies, it deletes the bigger files first — and if one of them alone covers
what's left, it takes that one — so you lose the fewest movies. Turn it off to
delete in strict score order. And if you have two copies of the same movie
scored about the same, the lower-quality copy goes first.

### Library sample

The sample table shows real movies pulled from your monitored paths with
merged Plex/Jellyfin data, scored with the settings on screen. Each row shows
its score breakdown and eligibility — filtered movies say exactly which rule
filtered them. **Refresh** pulls a brand-new batch at the selected size
(5–100 movies).

The **#** column is the actual deletion order. Type an **Over headroom**
target and it reorders live, exactly as a real run would, with a bracket
marking the group where file size optimization kicks in. The score is how much
a movie deserves to stay; the # is when it would actually go.

The first sample builds once a server is connected and a monitored path is
saved. Scoring needs the IMDb ratings dataset, so the build downloads it if
it's missing or stale — and if that download fails, you'll get a heads-up and
the sample sits out until you add the dataset by hand and hit **Refresh**.
Real runs hold to the same rule: they stop rather than score against stale
ratings. Below the table, the movie-fact sliders let you dial up a
hypothetical movie and watch its score react.

## Dashboard

- **Storage** — free space, used space, and monitored library size, with a ↻
  button to refresh the numbers on demand.
- **Cleanup Targets** — the configured headroom and library cap. When a
  target is currently breached it shows the ~GB a run would free.
- **Last Run** — outcome, trigger, and the current automatic mode.
- **Run Controls** — Simulate, one-time Live Run (double-click to confirm),
  and Stop.
- **Detailed Log** — streams the active or most recent run.
- **Deletion History** — every real deletion recorded in `deleted.log`,
  including why it was picked (score, plays, last watch).

Buttons disable while setup is incomplete, a selected API is unhealthy, a run
is already active, or every space limit is currently satisfied (a run would
delete nothing) — the tooltip always says which.

## Run Modes

### Simulate

Scans, filters, scores, and logs exactly what would be deleted. It never
deletes files and does not consume the daily cleanup schedule.

### Live Run

Deletes to every breached target (Headroom, Redline, Library Size Cap)
**immediately** — the deletion delay and the once-per-day schedule pace
automatic runs, not a deliberate button press. Because it deletes on the
spot, the button is ghosted while over space limits until a Simulate has
written the deletion plan for the current thresholds, so you always see
what a run removes before it can. Within limits it deletes nothing.

### Automatic Live

With Automatic Run Mode on **Live**, the scheduler checks every 15 minutes:

- **Headroom** and **Library Size Cap** cleanups trigger once per calendar
  day when breached, at the **Daily run time** (Automatic Run Mode; default
  midnight) — an eligible day waits for that time of day. The daily window
  is only consumed by a cleanup that actually runs — a within-limits check
  or one that's still waiting for the run time never blocks a breach later
  that day. Moving the run time to a slot still ahead today keeps today's run
  at the new time — even if today's run already happened, moving it to a later
  slot lets it run once more today (safe: with the deletion delay a same-day
  re-run only re-marks, deleting nothing extra). Moving it to a slot already
  past skips today and starts at the new time tomorrow (it never triggers an
  instant catch-up run).
- **Redline** cleanups trigger immediately on any tick that finds free space
  below the floor, ignoring the once-per-day schedule (and the deletion delay),
  and free only enough to get back to the floor. With a **current** marked plan
  (from Simulate or a daily run), the emergency takes a fast path: it deletes
  straight down the marked queue in plan order — re-verifying each file fresh
  against monitored paths, protected collections, and Jellyfin favorites, but
  skipping the full library rescan, so space frees in seconds. It then rebuilds the preview with
  a background Simulate. Without a current plan (rules or paths changed, queue
  too small, or protection can't be verified) it falls back to a full re-scored
  run, deleting lowest-value first.
- **Redline-only mode** (Headroom unticked): the daily schedule never fires —
  Redline is the only deletion trigger, and the marked queue is the standing
  preview Simulate maintains, not a schedule.
- **Deletion delay** (Space Thresholds) holds daily deletions for N whole
  days (minimum 1 — a movie is never deleted the same day it's marked).
  **Simulate writes the plan**: it marks the candidates (deleting nothing),
  and each mark becomes eligible at the daily run N days later — a daily run
  from then on deletes it (a manual Live Run ignores the delay entirely). Protecting a movie or changing the rules unmarks it
  (protection clears within ~15 minutes; rule changes reconcile on the
  next run). Marked movies show at the top of the Deleted Movie History
  (with their eligibility dates) and as a count on the Lifetime pruned
  button. 0 = eligible immediately: the next daily run (the next calendar
  day at the Daily run time) deletes them.
- **The plan must match the config.** A completed Simulate stamps every
  deletion-affecting setting — thresholds, filters, scoring, and monitored
  paths — into the queue (a stopped or partial Simulate never writes one).
  While over space limits, changing ANY of those settings ghosts both Live
  actions — arming automatic mode and the manual Live Run button — until a
  fresh Simulate rebuilds the plan. The re-Simulate is always a full scan,
  so it also picks up plays and last-played changes since the last run.
  Existing marks keep their original mark date through a re-Simulate; only
  movies newly entering the plan start a fresh delay clock. Automatic runs
  are exempt: an armed scheduler recomputes and re-marks its own plan
  every run.
- **Time zone** (Automatic Run Mode) is the clock all of this runs on:
  the Daily run time and calendar days mean that zone, and every timestamp
  in the UI and logs displays in it. Auto follows the container clock
  (often UTC in Docker) — set your zone so daily runs fire on your
  calendar. The setting shows the server's current time and warns if the
  host clock itself is off from your device by more than a couple of
  minutes.

While Paused, the same clock quietly refreshes storage stats instead, so the
Dashboard never goes stale. Any Live↔Paused change resets the clock, so the
first automatic run is always a full interval after Live is armed.

After a container restart the app always starts Paused — re-enable Live
explicitly when you are ready. Whenever MediaReducer pauses Live itself
(restart safety, or a save that changed connections/paths), the reason shows
on the Configuration page and on the Dashboard sub-line's hover.

Stopping or restarting the container during an active run is safe. A run is
never resumed — the next run recomputes its plan from a fresh scan — and a
`docker stop` forwards the stop to the run so the engine finishes the file it
is on (recording it to `deleted.log`) and archives its partial log before the
app exits, the same clean shutdown as the Dashboard's Stop button. Files
already deleted are permanent; nothing is left half-deleted, and no wrong file
can be removed by an interrupted run.

Every tick re-checks the thresholds against the current disk and library. If
they stop being safe while Live is armed — say a bulk copy into the library
pushes the cap past the safety percentage — the scheduler pauses Live with
the reason instead of running, and the engine independently refuses such a
run as a last line of defense.

## Safety Rules

MediaReducer is intentionally conservative:

- No monitored paths means no scan and no deletion.
- Every deletion must resolve inside `/library` *and* inside a monitored path.
- Any API failure during a run aborts the run.
- Protections fail closed: a configured protected collection that matches
  nothing on the server (renamed or deleted) aborts the run rather than
  running unprotected, and the Config page flags missing selections instead
  of silently dropping them.
- Plex/Jellyfin identity mismatches are skipped, never deleted.
- Protected collections and filtered movies are hard exclusions, not score
  penalties.
- Stop is safe but honest: deletions already made are permanent, the record
  of each deletion always lands in `deleted.log` (even if the stop arrives
  mid-deletion), and a stopped live run that deleted files always archives
  its log.
- Editing connection or monitoring settings while Live is on drops the mode to
  Paused; re-enabling takes a deliberate double-click.
- Run-affecting configuration is locked while a run is active.
- Live requires a passing health check and valid thresholds, and every run
  does a fresh pre-check before acting.
- Simulate and Live never touch the Filtering & Scoring library sample.

## Debug Mode

**Debug mode** (Advanced) adds Debug buttons around the app that dump raw
connection, collection, and run state into copyable popups. **Download report**
builds a diagnostic snapshot and saves it through your browser — nothing is
written to the container — so it's safe to attach to a bug report. Alongside
config and live connection/API checks, the report captures the decision state
that explains a run: the scheduler and effective clock (next tick, resolved
time zone), the current space verdict and forecast, the deletion plan and why
Live may be locked, protected-collection resolution, IMDb dataset age, and the
last run's flagged errors. Everything identifying — titles, collection names,
hosts, keys, paths, IPs — is scrubbed or replaced with stable hash tokens.

## Persistent Files

These live in the `/config` mount (`MEDIAREDUCER_DATA` on the host).

| File or folder | Purpose |
| --- | --- |
| `config.json` | Saved configuration. |
| `lastrun.log` | Most recent run log. |
| `deleted.log` | Permanent deletion history. |
| `logs/` | Archived logs from runs that performed cleanup. |
| `cache.json` | Movie metadata cache, schedule state, storage stats, and the Filtering & Scoring library sample. |
| `progress.json` | Live run progress for the web UI. |
| `title.ratings.tsv` | IMDb ratings dataset. |

**Reset MediaReducer** (Advanced) removes the configuration and state files
but always keeps the logs (`deleted.log`, `lastrun.log`, `logs/`) and the IMDb
dataset.

You can hand-edit `config.json`, but it's checked against the same rules the
UI uses. If an edit is invalid, MediaReducer locks everything down and the
Configuration page tells you exactly what's wrong, with a button to reset just
the bad values. Easiest to change settings in the UI and leave the file alone.

## Tests

`tests/run_tests.sh` runs the unit and scoring-parity suites (hermetic, no
network); add `--e2e` for browser end-to-end tests against a disposable app
instance (skipped cleanly if Playwright isn't installed). See `tests/README.md`.

New to the codebase? [ARCHITECTURE.md](ARCHITECTURE.md) maps how `app.py` and
`engine.py` fit together — the run modes, the request→run flow, the
plan-currency and deletion-delay models, and the state files.

## Updating

```bash
cd /mnt/user/appdata/MediaReducer
git pull
docker compose up -d --build
```

Settings and logs live in `/config`, so rebuilding the image never removes
them.

## Troubleshooting

### The Configuration tab is highlighted red

A selected connection is failing. Opening the page jumps to the failing
fields; fix the values or the mounts, then **Check for Errors**.

### A Config section is locked

Read the warning banner on the locked section. Common causes:

- No server software selected, or credentials missing/failing.
- `/config` cannot be read or written.
- Server-reported media paths do not match files under `/library`.
- No monitored path has been saved.
- Automatic Run Mode is Live, or a run is active.

### Protected collections do not appear

Collections load only after the relevant API connects. Use **Check for
Errors**, then return to Movie Library Paths. Debug mode can show the raw
collection API output.

### The library sample is empty

The sample needs a connected media server, at least one saved monitored path,
and the IMDb ratings dataset — only movies inside monitored paths qualify. If
the dataset download failed, add `title.ratings.tsv.gz` to the config folder
(see the popup steps), then press **Refresh** on the Filtering & Scoring tab
to pull a new batch.

### Library size looks stale

Use the storage card's ↻ button or clear the cache. Stats also refresh on the
15-minute clock and before every run.

### Radarr did not remove a movie

Radarr cleanup runs only when it is enabled, Radarr is connected, the deleted
movie belongs to the detected Plex section, and no other managed copy remains
on disk.

## License

MIT. See `LICENSE`.
