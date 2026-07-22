# MediaReducer

> [!WARNING]
> **This is early, experimental software — use it at your own risk.**
> MediaReducer is young, it is bound to have bugs I haven't found yet, and its
> whole job is deleting files. Assume something will eventually go wrong: keep
> backups, lean hard on Simulate, and don't point it at media you can't stand
> to lose.

MediaReducer is a Dockerized web app for keeping large movie libraries under
control. It reads your library from Plex/Tautulli, Jellyfin, or both, scores
every movie by watch history and IMDb rating, and deletes the lowest-value
files first when your storage thresholds are crossed.

It's built for one specific situation: a NAS where the media is secondary to
everything else on the box — the backups, photos, and documents come first,
and the movie library just lives in the leftover space. When that space starts
running out, MediaReducer's job is to guess which movies you're least likely
to miss and clear those first.

It will probably always be exactly that. If you want fine-grained, rule-based
control over what leaves your library and when, you probably want
[Maintainerr](https://github.com/jorenn92/Maintainerr) instead.

It is a normal Docker Compose app and runs anywhere the container can see your
movie files at `/library`.

> **This app deletes movie files — there is no recycle bin.** Stay on Paused
> and lean on Simulate until the output matches exactly what you expect before
> you ever run a real Cleanup.

> **Do not expose the web UI to the internet.** There is no login — anyone who
> can reach port 7474 can reconfigure MediaReducer and delete your media. Keep
> it on your LAN or behind a VPN. If you reach it through a reverse-proxy
> domain, list that domain in `MEDIAREDUCER_TRUSTED_HOSTS`.

## What It Does

- Reads your library from Plex/Tautulli, Jellyfin, or both.
- Only ever touches the `/library` folders you tell it to manage.
- Leaves alone anything in a protected collection, recently added, unplayed,
  highly rated, or marked a Jellyfin favorite (the last four are optional).
- Scores the rest on watch history and IMDb rating, blended by a single dial,
  and deletes the lowest scores first.
- Kicks in on a free-space target, an emergency floor, or a library size cap.
- Lets you preview all of it against your full library — scored live with the
  settings on screen — before anything runs.
- Shows progress, logs, storage, and a full deletion history on the
  Dashboard.
- Can tell Radarr to forget a movie once its copy is deleted, so it doesn't
  get re-downloaded.

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

The `/library` mount must be writable for Cleanups to delete files.

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

The library path stays the deletion boundary wherever you put it. The appdata
paths are only for auto-detecting API keys — skip them if you'd rather type
your URLs and keys into the UI.

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

### 1. Scheduler Mode

Leave this on **Paused** while setting up — you can still run everything
manually from the Dashboard. Automatic Cleanup stays locked until setup is
complete and the health check passes.

### 2. Connections

Pick your server software — Plex, Jellyfin, or both if they point at the same
files. **Auto Detect API Keys** fills the keys in from the mounted appdata;
add anything it misses. The URL fields can usually stay blank — MediaReducer
assumes your server's address on its standard port. The API key is the on/off
switch for each service: leave an optional one blank and it just stays off.

- Tautulli — required for Plex; default port 8181.
- Plex token — optional, unlocks protected Plex collections.
- Jellyfin — required for Jellyfin; default port 8096.
- Radarr — optional; default port 7878.

Hit **Check for Errors** to confirm MediaReducer can reach each API and match
your server's paths to real files under `/library`. When something is failing,
the Configuration tab turns red and jumps you to the bad fields.

### 3. Movie Library Paths

Add the `/library` folders MediaReducer is allowed to manage:

```text
Movies
Kids Movies
Holiday Movies
```

An empty list means MediaReducer manages nothing; run controls stay locked
until at least one path is saved.

When Plex or Jellyfin is connected, this section also shows protected
collection pickers — selected collections are always skipped. When Radarr is
connected, **Optional Radarr cleanup** appears as a single checkbox: when the
copy in Radarr's section is deleted, the movie is removed from Radarr so it
doesn't get re-downloaded. It never asks Radarr to delete files.

### 4. Space Thresholds

Space Thresholds unlock after a monitored path is saved.

- **Headroom target** — cleanup runs when free space drops below this amount
  and frees back up to it. 0 (the default) turns this trigger off; unticking
  the checkbox turns the headroom trigger off entirely — then a Redline floor
  and/or a Library Size Cap (either or both) drives cleanup instead.
- **Redline emergency floor** — optional. When free space drops below it,
  cleanup runs immediately and frees just enough to get back above the floor.
  It has to sit below the Headroom target.
- **Library Size Cap** — optional cap on the total size of your monitored
  movies, cleaned up on the same daily schedule as Headroom.
- **Deletion delay** — how many whole days a movie stays marked before a
  daily cleanup actually deletes it (Redline and manual runs skip the wait).

Anything that could delete more than you expect — or delete right away —
tells you so and asks for a second confirming click.

### Running without a Headroom target

Untick Headroom to turn the headroom trigger off. It's valid as long as a
Redline floor and/or a Library Size Cap is armed to drive cleanup:

- **Redline only** (a floor, no cap) is *redline-only mode*: Redline becomes the
  only thing that ever deletes, the deletion delay is retired, and Simulate keeps
  every eligible movie visible in deletion order so you always know what's on the
  chopping block.
- **Library Size Cap** (with or without a Redline floor) keeps running on the
  normal daily schedule with the deletion delay — just without a free-space
  headroom target. A Redline floor added alongside still handles emergencies.

### 5. Advanced

IMDb dataset settings, display/time settings, log retention, cache tools,
debug mode, the headroom safety cap, and **Reset MediaReducer**.

## Filtering & Scoring

The Filtering & Scoring tab holds every rule that decides *what* can be
deleted and *in what order*, and previews all of it against your full library.
Changes show up in the preview as you make them; **Save** keeps them.

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
  skipped; there isn't enough data to judge them.

Protected collections also affect eligibility; they are configured on the
Configuration tab.

### Scoring & Ordering

Every eligible movie gets a score from 0 to 100 — higher means keep, and the
lowest scores delete first. The score blends two things:

- **Watch history** — how often it's been played, how recently, and by how
  many people. A never-watched movie still gets credit for being recently
  added; **Max staleness** sets how long until it counts as fully stale.
- **IMDb rating** — the rating, weighted by how many votes back it up.

The dial starts at an even 50/50. If your library has little play history,
lean on IMDb; if it has a lot, lean on watch history.

Deletions go in score order, lowest first. **File size optimization** (on by
default) breaks near-ties by deleting bigger files first, so you lose the
fewest movies — and between two similar-scoring copies of the same movie, the
lower-quality copy goes first.

### Library table

The table shows your ENTIRE library as of the last run — every monitored-path
movie, scored with the settings on screen, paginated (25 rows by default,
switchable). Each row shows its score breakdown, and filtered movies say
exactly which rule filtered them. The **#** column is the actual deletion
order: type an **Over headroom calculator** target and it reorders live, exactly as a
real run would (the order always spans the whole library, not just the
visible page).

The table is empty until your first Simulate; every Simulate or Cleanup
refreshes it, and an interrupted run keeps the previous snapshot. Ratings come
from the run itself — it downloads the IMDb dataset when scoring needs it, and
stops rather than score against stale ratings. There's also a set of sliders
for dialing up a hypothetical movie and watching its score react.

## Dashboard

- **Storage** — free space, used space, and monitored library size, with a ↻
  button to refresh the numbers on demand.
- **Cleanup Targets** — the configured headroom and library cap. When a
  target is currently breached it shows the ~GB a run would free.
- **Last Run** — outcome, trigger, and the current automatic mode.
- **Run Controls** — Simulate, one-time Cleanup (double-click to confirm),
  and Stop.
- **Detailed Log** — streams the active or most recent run. Every run log opens
  with a **RUN CONTEXT** block — the mode, your targets, current disk and
  library size, and which target (if any) is breached — so it's the first thing
  to read when a run didn't do what you expected.
- **Marked & Eligible Deletions** — the standing deletion plan as "X - Y
  movies": how many are marked to delete (red when it's more than zero) and
  how many are eligible behind them, with the full ordered list a click away.
- **Deleted Movie History** — every real deletion recorded in `deleted.log`,
  including why it was picked (score, plays, last watch). Erasable if you
  want a clean slate.

Buttons disable while setup is incomplete, a selected API is unhealthy, or a
run is already active; the Cleanup button also ghosts while every space limit
is satisfied (a run would delete nothing). The tooltip always says which.

## Run Modes

### Simulate

Scans, scores, and logs exactly what would be deleted, without deleting
anything. This is also what writes the deletion plan Cleanups work from: in
every mode it queues the entire eligible list in deletion order, and only the
movies needed to meet the current targets are **marked** (and delay-clocked) —
the rest just show as eligible, next in line if more space is ever needed.

### Cleanup

The manual button deletes to every breached target immediately — no delay, no
daily schedule. It stays ghosted until a Simulate has shown you the plan for
your current settings, so you always see what a run removes before it can —
and while every limit is satisfied, since there'd be nothing to do.

### Automatic Cleanup

Set Scheduler Mode to **Automatic Cleanup** and MediaReducer runs on its own.
Arming it always requires that a Simulate has seen your library first — over a
breached limit that means a plan built under your current settings; within
limits, one completed Simulate is enough.

Once armed, the scheduler checks every 15 minutes:

- **Headroom** and **Library Size Cap** cleanups run at most once per calendar
  day, at the **Daily run time** you pick (default midnight).
- **Redline** fires immediately on any check that finds free space below the
  floor and frees just enough to get back above it — no delay, no waiting for
  the daily window.
- **Deletion delay** holds daily deletions for N days: a run first *marks* its
  candidates, and a later daily run deletes each mark once it comes due. Marked
  movies sit at the top of the **Marked & Eligible Deletions** list with their
  due dates; protecting a movie or changing the rules unmarks it.
- **The marked list keeps itself current between daily scans.** Every 15
  minutes MediaReducer re-checks the disk and re-sizes the marked set to what
  actually needs freeing: it drops movies whose files are gone, that you've
  since protected, or that a recent watch pulled out of the running, and marks
  more or fewer as the library grows or shrinks.
- **The plan must match your config.** Change any setting that affects what
  gets deleted and Automatic Cleanup locks until a fresh Simulate rebuilds the
  plan.
- **Time zone** is the clock all of this runs on. Auto follows the container
  clock — often UTC in Docker — so set your zone if you care when daily runs
  fire.

After a container restart the app always starts Paused — re-enable Automatic
Cleanup when you're ready. Stopping or restarting mid-run is safe: the engine
finishes the file it's on, records it, and shuts down cleanly; the next run
just starts fresh. And if your thresholds stop being safe while Automatic
Cleanup is armed — say a bulk copy pushes the cap past the safety percentage —
the scheduler pauses it with the reason instead of running.

## Safety Rules

MediaReducer is intentionally conservative:

- No monitored paths means no scan and no deletion.
- Every deletion must resolve inside `/library` *and* inside a monitored path.
- Any API failure during a run aborts the run.
- Protections fail closed: a protected collection that no longer matches
  anything on the server aborts the run rather than running unprotected.
- Plex/Jellyfin identity mismatches are skipped, never deleted.
- Protected collections and filtered movies are hard exclusions, not score
  penalties.
- Editing connection, monitoring, or threshold settings while Automatic Cleanup
  is on drops it back to Paused — review with Simulate, then re-enable. Settings
  are locked only while a run is actually active.
- Every run does a fresh safety pre-check before acting. Stop is always safe:
  deletions already made are permanent and always recorded in `deleted.log`,
  but nothing is ever left half-done.

## Debug Mode

**Debug mode** (Advanced) adds Debug buttons around the app that dump raw
connection and run state into copyable popups. **Download report** builds a
diagnostic snapshot that's safe to attach to a bug report — everything
identifying (titles, hosts, keys, paths, IPs) is scrubbed or replaced with
anonymous tokens.

## Persistent Files

These live in the `/config` mount (`MEDIAREDUCER_DATA` on the host).

| File or folder | Purpose |
| --- | --- |
| `config.json` | Saved configuration. |
| `lastrun.log` | Most recent run log — archived into `logs/` on startup so the run panel starts clean against the freshly-cleared plan. |
| `deleted.log` | Deletion history (erasable from the Dashboard). |
| `logs/` | Archived logs from runs that performed cleanup. |
| `cache.json` | All cached state in one file: movie metadata, schedule state, storage stats, the Filtering & Scoring library snapshot, and (under `pending`) the marked & eligible deletion plan the last Simulate built. Cleared on startup so a restart requires a fresh Simulate. |
| `progress.json` | Cleanup progress for the web UI — reset to "no runs yet" on startup alongside the cache. |
| `title.ratings.tsv` | IMDb ratings dataset. |

**Reset MediaReducer** (Advanced) removes the configuration and state files
but always keeps the logs (`deleted.log`, `lastrun.log`, `logs/`) and the IMDb
dataset.

You can hand-edit `config.json`, but it's checked against the same rules the
UI uses — an invalid edit locks things down until it's fixed. Easier to just
use the UI and leave the file alone.

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
- Scheduler Mode is Automatic Cleanup, or a run is active.

### Protected collections do not appear

Collections load only after the relevant API connects. Use **Check for
Errors**, then return to Movie Library Paths. Debug mode can show the raw
collection API output.

### The library table is empty

The Filtering & Scoring table is built by runs: run a Simulate (with a
connected media server and at least one saved monitored path) and it fills in
with every monitored-path movie. It refreshes on every subsequent run.

### Library size looks stale

Use the storage card's ↻ button or clear the cache. Stats also refresh on the
15-minute clock and before every run.

### Radarr did not remove a movie

Radarr cleanup runs only when it is enabled, Radarr is connected, and the
deleted file was the copy in Radarr's detected Plex section. A copy that's
known to live in a different section never touches Radarr — only when the
section can't be determined does it fall back to checking whether Radarr's
own folder was the one deleted. Redline emergency deletions from the marked
queue skip Radarr cleanup.

## License

MIT. See `LICENSE`.
