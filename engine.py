#!/usr/bin/env python3
"""
engine.py — the MediaReducer deletion engine.

Monitors disk usage and monitored movie-library size and removes low-value
movie files when configured thresholds are exceeded. Reads libraries from
Plex/Tautulli, Jellyfin, or both; Plex and Jellyfin supply protected-collection
metadata; Radarr can drop a deleted movie from monitoring once its last copy is
gone. Pure Python 3 standard library — no pip installs.

HOW IT RUNS
-----------
The web UI (app.py) launches this as a subprocess for every scan: the scheduler
ticks it repeatedly, the Dashboard Simulate/Live Run buttons invoke it once, and
quiet modes (MEDIAREDUCER_MODE_OVERRIDE) back the storage refresh and the
Filtering & Scoring library sample. It also runs standalone from a shell or cron
off the hardcoded settings below when MEDIAREDUCER_CONFIG is unset. Connection
settings come from the saved config; a blank required value stops the run.

WHAT GETS DELETED — AND IN WHAT ORDER
--------------------------------------
Hard exclusions (never weighted, always skipped): protected Plex/Jellyfin
collections; added within GRACE_PERIOD_DAYS; no resolvable path; outside
MONITOR_DIRS; when IMDb is in use, no IMDb rating/votes (too little data to
judge — off at 100% watch history, where unrated movies stay eligible);
SKIP_UNPLAYED_MOVIES; MAX_IMDB_RATING cutoff; PROTECT_JELLYFIN_FAVORITES.

Eligible movies are ranked by an additive RetentionScore — HIGHER means "keep".
The only knob is SCORE_BALANCE (0–100, default 50): left favors what the
household watches, right favors what IMDb rates highly. Both sides are on the
same 0–100 scale, so the dial shifts PRIORITY rather than shrinking the score.
Every curve constant lives in scoring_constants.py.

  history (0–100)
            +USAGE_MAX_PTS × log1p(plays)/log1p(USAGE_FULL_PLAYS), capped
             (watch frequency)
            +RECENCY_TIERS points for how recently it was watched — or, for a
             never-watched movie, how recently it was ADDED (a fresh add reads
             like a fresh watch, without frequency/user credit)
            +MULTI_USER_PTS per distinct user watched, capped (Jellyfin counts
             real users; a played Plex movie via Tautulli counts as one).
             Distinct users also STRETCH the staleness window so a
             widely-watched movie's recency decays slower
             (USER_DECAY_PER_USER / USER_DECAY_MAX_MULT)
            +soft shelf past the staleness cliff, only while BOTH sides carry
             weight: an added-date gradient (SHELF_MAX_PTS, tent-weighted — zero
             at either dial end, peak at 50/50) so stale never-watched movies
             keep an age order once IMDb blends in
  imdb (0–100)
            imdb_rating × 10 × vote_confidence, capped at +100. vote_confidence
            rises logarithmically to 1.0 with imdb_num_votes (unknown count gets
            a default). Votes are CONFIDENCE for the rating, never standalone
            popularity — many votes cannot protect a bad, unused movie.

  score = history × HISTORY_WEIGHT + imdb × QUALITY_WEIGHT   (0–100)

score_balance_weights() maps the dial linearly: quality = balance/100,
history = 1 − quality, always summing to 1.0 (0 = pure history, 100 = pure
IMDb). Bias toward IMDb when watch history is thin; toward history when it is
rich. Never-played movies earn no history points and sink.

Deletion order is RetentionScore ASCENDING (exact ties: never-watched first,
then — only when IMDb is in use — lowest IMDb rating, oldest added, larger
files, title). File size optimization (NEAR_TIE_PTS window, default 2; None =
off) reorders only at the boundary where the order decides what survives — see
_pop_next_deletion. Near-tied copies of the SAME movie reorder so the
lowest-quality copy deletes first (resolution, bitrate, size), then the copy
Radarr does not monitor — the best copy survives longest.

SCHEDULING (invoked every few minutes by the web UI's scheduler)
----------
  - Headroom: once per calendar day when usage exceeds HEADROOM_GB. Frees back
    to the HEADROOM_GB target.
  - Redline: immediate on every tick if free space drops below REDLINE_GB. Frees
    only back to the REDLINE_GB floor (just enough to clear the emergency),
    deleting lowest-value first from a fresh re-score — which clears the
    already-marked movies (the lowest-value ones) in order, reflecting the
    current paths/filters/scoring.
  - Library cap: immediate on every tick if on-disk size exceeds MAX_LIBRARY_GB
    (same trigger cadence as redline).
  - RUN_MODE="debug_sim": full simulation, no deletions, ignores schedule.
  - RUN_MODE="debug_info": status and library size vs. limits, then exits.
  - RUN_MODE="headroom": live mode; enforces the space limits and MAX_LIBRARY_GB
    when the cap is enabled.

OUTPUT FILES (all under OUTPUT_DIR)
------------------------------------
  lastrun.log     — most recent run (overwritten each time)
  deleted.log     — permanent append-only record of every real deletion
  logs/           — archived logs from every run that performed cleanup
  cache.json      — metadata cache, daily schedule state, dashboard storage snapshot, Filtering & Scoring sample
  progress.json   — structured live progress for the web UI
  title.ratings.tsv — IMDb ratings dataset (refreshed past IMDB_RATINGS_MAX_AGE_DAYS)
"""

import atexit
import calendar
import datetime as _dt
import os as _os
import gzip
import hashlib
import io
import json
import math
import random
import re
import shutil
import signal
import time

from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # non-POSIX dev box — single-writer assumption holds there
    fcntl = None

from scoring_constants import SCORING
import urllib.parse
import urllib.request
from pathlib import Path

# =========================
# CONFIG
# =========================

# ── Deployment roots ──────────────────────────────────────────────────────────
# The library mount and appdata mounts default to their Docker paths but can be
# relocated for a bare-metal install via environment variables. The web app
# reads the SAME vars and the engine subprocess inherits its env, so the two
# always agree. Deploy-time infrastructure, never editable from the UI or config.
def _load_dotenv() -> None:
    """Load a .env file (next to engine.py, or MEDIAREDUCER_ENV_FILE) for a
    direct engine run; a real env var always wins. Normally the app launches
    the engine with its own env already loaded, so this is a no-op then."""
    env_path = Path(_os.environ.get("MEDIAREDUCER_ENV_FILE") or (Path(__file__).parent / ".env"))
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in _os.environ:
            _os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv()


def _root_from_env(var: str, default: str) -> Path:
    return Path(_os.environ.get(var, default).rstrip("/") or default)


# ── Script output ─────────────────────────────────────────────────────────────
OUTPUT_DIR  = Path("/config")   # log, cache, ratings go here
CHECK_PATH  = _root_from_env("MEDIAREDUCER_LIBRARY", "/library")  # filesystem to monitor

LOGFILE           = OUTPUT_DIR / "lastrun.log"     # overwritten each run
LOGS_DIR          = OUTPUT_DIR / "logs"            # archived cleanup run logs
DELETED_LOG       = OUTPUT_DIR / "deleted.log"     # permanent deletion history
IMDB_RATINGS_PATH = OUTPUT_DIR / "title.ratings.tsv"
CACHE_FILE        = OUTPUT_DIR / "cache.json"
PROGRESS_FILE     = OUTPUT_DIR / "progress.json"   # structured run progress for the web UI
PENDING_FILE      = OUTPUT_DIR / "pending_deletions.json"  # marked-for-deletion queue (deletion delay)
CONFIG_ERRORS     = []  # populated while loading config.json; live/sim runs abort on these
RADARR_SECTION_METHOD_LABELS = {
    "path-prefix": "path prefix",
    "root-prefix": "root folder path",
    "folder-name": "library folder name",
}


def radarr_section_method_label(method):
    method = str(method or "").strip()
    return RADARR_SECTION_METHOD_LABELS.get(method, method.replace("-", " ") if method else "")

# ── Run mode ──────────────────────────────────────────────────────────────────
#   "debug_info" — status only (no scan/delete): connections, filesystem and
#                  library sizes vs. limits, then exits. Start here to read your
#                  library size before touching MAX_LIBRARY_GB.
#   "debug_sim"  — full dry run: scans, scores, and logs the candidate list as it
#                  would run live, but touches nothing and ignores the schedule.
#   "headroom"   — live mode: enforces HEADROOM_GB/REDLINE_GB, plus MAX_LIBRARY_GB
#                  when the cap is set.
RUN_MODE = "debug_info"

# The only RUN_MODE values that may proceed past main()'s safety gate. Anything
# else — the Docker default "paused", a blank value, or a typo — is treated as a
# no-op so a direct/cron invocation can never fall through to live deletion.
EXECUTABLE_RUN_MODES = ("debug_info", "debug_sim", "headroom")

# ── Space thresholds ───────────────────────────────────────────────────────────
HEADROOM_GB = 1000  # free space to maintain (once-per-day cleanup trigger). ~1 TB
                    # is a reasonable start on a 20 TB array.
REDLINE_GB  = 200   # emergency floor: immediate cleanup when free space drops below
                    # this, freeing only back to this floor (not the headroom target).
                    # Cannot exceed HEADROOM_GB. Set equal to HEADROOM_GB for a single
                    # threshold enforced every run ("redline-only"); None disables.

# ═══════════════════════════════════════════════════════════════════════════════

# ── Library size cap ──────────────────────────────────────────────────────────
# Caps the total size of movie files under MONITOR_DIRS. Measured from disk using
# MOVIE_EXTENSIONS, so dashboard storage, run triggers, and deletion targets share
# one source of truth. Over the cap, cleanup runs on every cron tick without
# consuming the daily headroom window (same as REDLINE_GB). Enforced automatically
# whenever it is set; RetentionScore ordering bounds the blast radius (lowest-value
# movies delete first). To size it, read "Library size: X.X GB" from a debug_info
# run before setting a value at or below it.
MAX_LIBRARY_GB = None  # maximum on-disk library size in GB. None = disabled.

# Deletion delay in whole calendar days, minimum 1. A daily cleanup first MARKS
# candidates (pending_deletions.json) and deletes a mark only once it is N+
# calendar days old — so a movie is never deleted the same day it is marked; the
# earliest deletion is the next day's daily run (N=1). Larger N widens the grace
# window to protect a movie or change the rules. Redline emergencies and manual
# Live Runs ignore the delay (waiting defeats an emergency floor / a deliberate
# button press); a redline re-scores fresh and deletes lowest-value first, so it
# clears the already-marked movies (the lowest-value ones) in the current order.
DELETE_DELAY_DAYS = 1

# Time of day (24h HH:MM, in the operating time zone) the once-per-day
# headroom/cap cleanup may fire — an eligible day waits for this time. The
# calendar-day window itself is unchanged; redline emergencies and manual
# Live Runs ignore it.
DAILY_RUN_TIME = "00:00"

# ═══════════════════════════════════════════════════════════════════════════════

# ── Movie library ─────────────────────────────────────────────────────────────
# In the container the movie files are bind-mounted at one fixed location,
# /library, and nothing else of the host is mounted. That mount is both the only
# place the script can read/delete and the deletion-safety boundary — no
# whole-filesystem view to guard against, no Plex→disk path translation.
# Media servers may report the same files from a different container root (e.g.
# /data/Movies/... vs /library/Movies/...); startup health checks accept that as
# long as each sampled path has a matching suffix under the library root.
LIBRARY_ROOT = CHECK_PATH  # the only readable/deletable location; the safety boundary

# MONITOR_DIRS — OPTIONAL allow-list of folders under /library that the script is
# permitted to manage, e.g. ["/library/Movies", "/library/Movies LQ"]. Leave it
# empty to manage nothing. To deliberately manage the entire mounted library,
# add "/" or "/library" as a monitored path. Bare names ("Movies") are
# accepted too and treated as /library/<name>.
MONITOR_DIRS = []

MOVIE_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv"}  # only files with these extensions are eligible

MAX_STALENESS_MONTHS        = SCORING["RECENCY_DEFAULT_MONTHS"]  # recency fades to 0 over this window.

# ── Filtering ─────────────────────────────────────────────────────────────────
PROTECTED_COLLECTIONS       = set()          # Plex collection name(s) — never deleted.
GRACE_PERIOD_DAYS           = 30             # Skip movies added to the library within this many days.
SKIP_UNPLAYED_MOVIES        = False          # When true, movies with no play history are never deleted.
MAX_IMDB_RATING             = None           # Optional eligibility cutoff: movies rated ABOVE this on
                                             # IMDb are never deleted (hard rule, not a score). None =
                                             # disabled. Unrated movies are unaffected either way.
PROTECT_JELLYFIN_FAVORITES  = False          # When true, movies ANY Jellyfin user favorited are never
                                             # deleted. Off by default — favorites are not cleanly
                                             # shared between Plex/Tautulli and Jellyfin, so they are
                                             # an explicit protection override, never a scoring signal.

# ── Retention-score balance ───────────────────────────────────────────────────
# SCORE_BALANCE (0–100) is the ONLY scoring knob. The loader derives the two
# side weights below via score_balance_weights(); compute_retention_score()
# reads only those. Weights always sum to 1.0 (see the module docstring).
HISTORY_WEIGHT = 0.50  # watch history & recency side share
QUALITY_WEIGHT = 0.50  # IMDb rating & votes side share
SCORE_BALANCE     = 50     # UI slider position (0=watch history … 100=IMDb rating).
                           # Authoritative: the loader derives the weights from it.


def score_balance_weights(balance):
    """Map the 0–100 SCORE_BALANCE dial to (history_weight, quality_weight).

    Linear: quality = balance / 100, history = 1 − quality, always summing to
    1.0 so the retention score stays on one 0–100 scale and the dial shifts
    priority instead of shrinking the score. Center (50) is an even split;
    ends are 100/0 and 0/100. Mirrored exactly by balanceWeights() in the
    Score Explorer.
    """
    quality = min(max(float(balance), 0.0), 100.0) / 100.0
    return 1.0 - quality, quality
RADARR_OVERSEERR_SECTION_ID = None           # Plex section ID monitored by Radarr.
RADARR_OVERSEERR_SECTION_ID_SOURCE = "disabled"  # disabled, manual, auto, or auto-failed.
                                             # When set, the script removes deleted movies from Radarr
                                             # so they can be re-requested and re-grabbed normally.
                                             # (Overseerr updates itself automatically once Radarr
                                             # drops the movie, so no Overseerr call is needed.)
                                             # Set to the numeric ID of your main movie section (usually
                                             # "1") or leave as None to disable.

# ── IMDB ratings dataset ──────────────────────────────────────────────────────
IMDB_RATINGS_URL          = "https://datasets.imdbws.com/title.ratings.tsv.gz"
IMDB_RATINGS_MAX_AGE_DAYS = 7  # Re-download after this many days

# ── Logging retention ─────────────────────────────────────────────────────────
LOG_RETENTION_DAYS    = 30     # Delete archived run logs older than this. 0 = keep forever.
KEEP_INTERRUPTED_LOGS = False  # Also archive the partial log when a stopped run deleted NOTHING.
                               # A stopped live run that deleted files always archives its log —
                               # that record must not depend on a setting.
# Runs that reach these modes perform real work and archive their log (whether
# they finish, fail, or — if opted in — get interrupted). debug_info Summary
# refreshes and the paused no-op never archive.
ARCHIVABLE_RUN_MODES  = ("debug_sim", "headroom")

# ── Connections ───────────────────────────────────────────────────────────────
# URLs and API keys are saved in config.json by the web UI. The Config page has
# an explicit Auto Detect button that can copy values from mounted appdata into
# these fields; this runtime script never fills blank values on its own.
# If a required saved value is blank, the script stops and asks you to fill it.

# Appdata mounts used by the web UI's Auto Detect and health checks (relocatable
# via env, same as the library root).
TAUTULLI_APPDATA  = _root_from_env("MEDIAREDUCER_TAUTULLI_APPDATA", "/tautulli")
RADARR_APPDATA    = _root_from_env("MEDIAREDUCER_RADARR_APPDATA", "/radarr")

# Saved connection values. Blank means "not configured" at runtime.
TAUTULLI_URL      = ""
TAUTULLI_API_KEY  = ""
PLEX_URL          = ""
PLEX_TOKEN        = ""
RADARR_URL        = ""
RADARR_API_KEY    = ""

# Server software + Jellyfin (native API). The read-only client lives below;
# defaults keep Jellyfin fully inert until enabled.
USE_PLEX                       = True
USE_JELLYFIN                   = False
JELLYFIN_URL                   = ""
JELLYFIN_API_KEY               = ""
JELLYFIN_PROTECTED_COLLECTIONS = set()   # Jellyfin collection name(s) — kept separate from Plex.

# ═══════════════════════════════════════════════════════════════════════════════

# ── Headroom safety cap ───────────────────────────────────────────────────────
# HEADROOM_GB cannot exceed this percentage of total filesystem capacity.
#
# !! WARNING: DO NOT RAISE THIS VALUE WITHOUT GOOD REASON !!
# This prevents a misconfigured HEADROOM_GB from making the script believe it
# needs to free an unreasonable amount of space, which could result in deleting
# your entire eligible library before the limit is ever actually reached.
#
# At 15% on a 54 TB array the maximum HEADROOM_GB is ~8,100 GB.
# If HEADROOM_GB exceeds the cap:
#   debug modes    → script logs a loud warning and continues simulating
#   Live      → script REFUSES TO RUN until HEADROOM_GB is corrected
#
# Only change this if you have a very specific reason and understand the risk.
MAX_HEADROOM_PCT = 15  # percent of total filesystem capacity

# =========================
# HELPERS
# =========================

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    print(line, flush=True)
    # Best-effort file write: a full or unwritable log volume must never abort
    # a run mid-deletion. The console line above still reaches docker logs.
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _find_appdata_file(base, *names):
    """Locate one of `names` inside a mounted appdata directory, or None.

    Docker images place config at the mapped folder's root, in a `config/`
    subfolder, or a directory deeper depending on the Unraid template. Check the
    common locations first, then do a shallow bounded search.
    """
    base = Path(base)

    # Fast path for the expected layouts.
    for d in (base, base / "config"):
        for name in names:
            p = d / name
            try:
                if p.exists():
                    return p
            except OSError:
                pass

    # Fallback for templates that mount a parent appdata directory.  Keep this
    # intentionally shallow so we do not wander through large media/cache trees.
    try:
        if base.exists():
            for root, dirs, files in _os.walk(base):
                rel = Path(root).relative_to(base)
                depth = len(rel.parts)
                if depth >= 4:
                    dirs[:] = []
                    continue
                for name in names:
                    if name in files:
                        return Path(root) / name
    except OSError:
        pass

    return None


def _norm_service_path(value):
    """Normalize a Plex/Radarr path for cross-container prefix comparisons."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    raw = re.sub(r"/+", "/", raw)
    if len(raw) > 1:
        raw = raw.rstrip("/")
    return raw.lower()


def _path_under_or_same(path, root):
    p = _norm_service_path(path)
    r = _norm_service_path(root)
    return bool(p and r and (p == r or p.startswith(r.rstrip("/") + "/")))


def _radarr_json(path, timeout=20):
    if not RADARR_URL or not RADARR_API_KEY:
        return None
    url = f"{RADARR_URL.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"X-Api-Key": RADARR_API_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def detect_radarr_plex_section_id():
    """
    Best-effort auto-detection for the Plex movie section Radarr manages.

    Radarr does not store a Plex section ID directly. We infer it by comparing
    Radarr movie/root paths against Plex library section root folders. Exact
    prefix matches win; if Docker mount names differ, a unique folder-name match
    is accepted as a fallback. Ambiguous layouts return no detection.
    """
    result = {
        "ok": False,
        "section_id": None,
        "section_name": None,
        "method": None,
        "message": "Unable to detect Radarr's Plex section.",
        "counts": {},
        "sections": [],
    }

    if not RADARR_URL or not RADARR_API_KEY:
        result["message"] = "Radarr URL/API key are not available."
        return result
    if not PLEX_URL or not PLEX_TOKEN:
        result["message"] = "Plex URL/token are not available."
        return result

    try:
        # Fail fast: this runs on every config load when the section ID is set
        # to "auto", so an unreachable Radarr must not stall the whole run.
        movies = _radarr_json("/api/v3/movie", timeout=6) or []
    except Exception as e:
        result["message"] = f"Could not read Radarr movies: {e}"
        return result

    if not isinstance(movies, list) or not movies:
        result["message"] = "Radarr returned no movies to compare."
        return result

    radarr_paths = []
    radarr_roots = []
    for movie in movies:
        if not isinstance(movie, dict):
            continue
        path = movie.get("path") or movie.get("folderName")
        root = movie.get("rootFolderPath")
        if path:
            radarr_paths.append(str(path))
        if root:
            radarr_roots.append(str(root))

    if not radarr_paths and not radarr_roots:
        result["message"] = "Radarr movies did not include usable paths."
        return result

    status, plex_data = plex_request("/library/sections", timeout=6)
    if status != 200 or not plex_data:
        result["message"] = "Could not read Plex library sections."
        return result

    plex_dirs = ((plex_data or {}).get("MediaContainer") or {}).get("Directory") or []
    if isinstance(plex_dirs, dict):
        plex_dirs = [plex_dirs]

    sections = []
    for sec in plex_dirs:
        if not isinstance(sec, dict):
            continue
        if sec.get("type") not in (None, "movie") and sec.get("type") != "movie":
            continue
        locations = [str(loc.get("path", "")) for loc in (sec.get("Location") or []) if loc.get("path")]
        sid = str(sec.get("key", "")).strip()
        if not sid or not locations:
            continue
        sections.append({
            "id": sid,
            "name": str(sec.get("title") or sec.get("titleSort") or sid),
            "locations": locations,
        })

    result["sections"] = [{"id": s["id"], "name": s["name"], "locations": s["locations"]} for s in sections]
    if not sections:
        result["message"] = "Plex returned no movie library sections with locations."
        return result

    # 1) Exact/path-prefix matching by movie path.
    exact_counts = {s["id"]: 0 for s in sections}
    for path in radarr_paths:
        for sec in sections:
            if any(_path_under_or_same(path, loc) for loc in sec["locations"]):
                exact_counts[sec["id"]] += 1

    def _unique_winner(counts, total_needed=None):
        positive = [(sid, count) for sid, count in counts.items() if count > 0]
        if len(positive) != 1:
            return None
        sid, count = positive[0]
        if total_needed is not None and count < total_needed:
            return None
        return sid

    winner = _unique_winner(exact_counts, total_needed=len(radarr_paths) if radarr_paths else None)
    method = "path-prefix"
    counts = exact_counts

    # 2) Prefix matching by Radarr root folders. This helps when a few movie
    # rows are missing path but rootFolderPath is populated.
    if not winner and radarr_roots:
        root_counts = {s["id"]: 0 for s in sections}
        unique_roots = sorted(set(radarr_roots))
        for root in unique_roots:
            for sec in sections:
                if any(_path_under_or_same(root, loc) or _path_under_or_same(loc, root) for loc in sec["locations"]):
                    root_counts[sec["id"]] += 1
        winner = _unique_winner(root_counts, total_needed=len(unique_roots))
        method = "root-prefix"
        counts = root_counts

    # 3) Fallback: compare final folder names when Plex/Radarr use different
    # Docker mount roots, e.g. /data/Movies vs /movies/Movies.
    if not winner:
        folder_counts = {s["id"]: 0 for s in sections}
        radarr_names = []
        for value in sorted(set(radarr_roots or [])) or radarr_paths:
            name = Path(str(value).strip().replace("\\", "/")).name.lower()
            if name:
                radarr_names.append(name)
        for name in radarr_names:
            for sec in sections:
                section_names = {Path(loc).name.lower() for loc in sec["locations"] if Path(loc).name}
                if name in section_names:
                    folder_counts[sec["id"]] += 1
        winner = _unique_winner(folder_counts, total_needed=len(radarr_names) if radarr_names else None)
        method = "folder-name"
        counts = folder_counts

    result["counts"] = counts
    if not winner:
        nonzero = {sid: c for sid, c in counts.items() if c}
        if nonzero:
            result["message"] = f"Radarr paths matched multiple Plex sections: {nonzero}. Set the section manually."
        else:
            result["message"] = "Radarr paths did not match any Plex movie section. Set the section manually."
        return result

    sec = next((s for s in sections if s["id"] == winner), None)
    method_label = radarr_section_method_label(method)
    result.update({
        "ok": True,
        "section_id": winner,
        "section_name": sec["name"] if sec else winner,
        "method": method,
        "method_label": method_label,
        "message": f"Detected Plex section {winner}" + (f" ({sec['name']})" if sec else "") + f" from Radarr {method_label} match.",
    })
    return result


def _resolve_auto_radarr_overseerr_section_id():
    """Resolve config value 'auto' into a concrete section ID before the run starts."""
    global RADARR_OVERSEERR_SECTION_ID, RADARR_OVERSEERR_SECTION_ID_SOURCE
    RADARR_OVERSEERR_SECTION_ID_SOURCE = "manual" if RADARR_OVERSEERR_SECTION_ID else "disabled"
    if str(RADARR_OVERSEERR_SECTION_ID or "").strip().lower() != "auto":
        return
    detected = detect_radarr_plex_section_id()
    if detected.get("ok") and detected.get("section_id"):
        RADARR_OVERSEERR_SECTION_ID = str(detected["section_id"])
        RADARR_OVERSEERR_SECTION_ID_SOURCE = "auto"
    else:
        RADARR_OVERSEERR_SECTION_ID_SOURCE = "auto-failed"
        RADARR_OVERSEERR_SECTION_ID = None
        # Cleanup is optional. If auto-detection fails, disable the optional
        # post-delete cleanup for this run rather than aborting local cleanup.
        # The web UI health check surfaces this as a configuration warning.



# ── Validate saved connections ────────────────────────────────────────────────


_CONNECTION_VALIDATION_ERRORS: list = []   # first entry feeds the run-abort message


def validate_connections():
    """Check required connection values for the selected server(s).

    Plex requires Tautulli (the library/watch-history source). Jellyfin requires
    its URL + API key. At least one server must be selected. Plex/Radarr URLs are
    optional helpers.
    """
    # Collected so the run-abort message can carry the FIRST specific problem
    # into the dashboard's progress panel instead of "see the log for details".
    global _CONNECTION_VALIDATION_ERRORS
    _CONNECTION_VALIDATION_ERRORS = []

    def _conn_error(message):
        _CONNECTION_VALIDATION_ERRORS.append(message)
        log(f"ERROR: {message}")

    if not (USE_PLEX or USE_JELLYFIN):
        _conn_error("No server software is selected. Enable Plex or Jellyfin in the Config page's Connections section.")
        return False

    ok = True

    if USE_PLEX:
        missing = [name for name, val in (("TAUTULLI_URL", TAUTULLI_URL),
                                          ("TAUTULLI_API_KEY", TAUTULLI_API_KEY)) if not val]
        if missing:
            for name in missing:
                _conn_error(f"{name} is blank in the saved Connections config.")
            log("Set the required Tautulli URL/API key in the Config page's URLs and API Keys section.")
            log("Use Auto Detect there to copy values from mounted appdata, or fill them in manually if your Docker port/proxy differs.")
            ok = False
        if PROTECTED_COLLECTIONS and (not PLEX_URL or not PLEX_TOKEN):
            _conn_error("Plex URL/token are blank, but Plex protected collections are configured.")
            ok = False
        elif not PROTECTED_COLLECTIONS:
            log("Plex protected collection checks disabled (PROTECTED_COLLECTIONS is empty); Plex API check skipped.")

    if USE_JELLYFIN:
        jmissing = [name for name, val in (("JELLYFIN_URL", JELLYFIN_URL),
                                           ("JELLYFIN_API_KEY", JELLYFIN_API_KEY)) if not val]
        if jmissing:
            for name in jmissing:
                _conn_error(f"{name} is blank in the saved Connections config.")
            log("Set the Jellyfin URL and API key in the Config page's Connections section (create an API key in Jellyfin's Dashboard → API Keys).")
            ok = False

    return ok


def _abort_api_failure(message, *, phase="checking"):
    """Fail closed when a selected media API stops answering during a run."""
    log(f"ABORT: {message}")
    emit_progress(status="error", phase=phase, message=message)
    raise SystemExit(1)


def verify_runtime_api_health():
    """Probe selected APIs immediately before a run starts making decisions."""
    if USE_PLEX:
        try:
            tautulli_api("get_libraries")
        except Exception as e:
            _abort_api_failure(f"Tautulli API check failed during run startup: {e}")
        if PROTECTED_COLLECTIONS:
            if not (PLEX_URL and PLEX_TOKEN):
                _abort_api_failure("Plex protected collections are configured, but Plex URL/token are not available.")
            try:
                section_ids = _plex_movie_section_ids_direct()
            except Exception as e:
                _abort_api_failure(f"Plex API check failed during run startup: {e}")
            if not section_ids:
                _abort_api_failure("Plex API check failed during run startup: no movie sections were returned.")

    if USE_JELLYFIN:
        try:
            _jellyfin_request("System/Info", timeout=6)
        except Exception as e:
            _abort_api_failure(f"Jellyfin API check failed during run startup: {e}")

    if RADARR_OVERSEERR_SECTION_ID and RADARR_URL and RADARR_API_KEY:
        try:
            _radarr_json("/api/v3/system/status", timeout=6)
        except Exception as e:
            _abort_api_failure(f"Radarr API check failed during run startup: {e}")

    verify_media_path_compatibility()


def _sample_tautulli_reported_paths(limit=12):
    paths = []
    libraries = tautulli_api("get_libraries") or []
    section_ids = [
        lib.get("section_id") for lib in libraries
        if isinstance(lib, dict) and lib.get("section_type") == "movie" and lib.get("is_active", 1)
    ]
    possible_keys = ["file", "file_path", "media_file", "location", "path"]

    def add_paths(item):
        if not isinstance(item, dict):
            return
        for key in possible_keys:
            value = item.get(key)
            if value:
                paths.append(str(value))
        media_info = item.get("media_info")
        if isinstance(media_info, list):
            for media in media_info:
                if not isinstance(media, dict):
                    continue
                for key in possible_keys:
                    value = media.get(key)
                    if value:
                        paths.append(str(value))
                for part in media.get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    for key in possible_keys:
                        value = part.get(key)
                        if value:
                            paths.append(str(value))

    for section_id in section_ids:
        data = tautulli_api(
            "get_library_media_info",
            section_id=section_id,
            section_type="movie",
            start=0,
            length=25,
            order_column="title",
            order_dir="asc",
        )
        rows = data if isinstance(data, list) else (((data or {}).get("data")) or [])
        for row in rows:
            add_paths(row)
            if len(paths) >= limit:
                return paths[:limit]
        for row in rows[: min(len(rows), 8)]:
            rating_key = row.get("rating_key") if isinstance(row, dict) else None
            if not rating_key:
                continue
            metadata = tautulli_api("get_metadata", rating_key=rating_key, media_info=1)
            add_paths(metadata)
            if len(paths) >= limit:
                return paths[:limit]
    return paths[:limit]


def _sample_jellyfin_reported_paths(limit=12):
    paths = []
    data = (_jellyfin_request("Items", {
        "IncludeItemTypes": "Movie",
        "Recursive": "true",
        "Fields": "Path,MediaSources",
        "Limit": max(limit, 25),
    }, timeout=10) or {}).get("Items", [])
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("Path"):
            paths.append(str(item.get("Path")))
        for ms in item.get("MediaSources") or []:
            if isinstance(ms, dict) and ms.get("Path"):
                paths.append(str(ms.get("Path")))
        if len(paths) >= limit:
            break
    return paths[:limit]


def verify_media_path_compatibility():
    """Fail closed if selected APIs report paths that cannot map under /library."""
    checks = []
    if USE_PLEX:
        checks.append(("Plex/Tautulli", _sample_tautulli_reported_paths))
    if USE_JELLYFIN:
        checks.append(("Jellyfin", _sample_jellyfin_reported_paths))

    for label, sampler in checks:
        try:
            raw_paths = sampler()
        except Exception as e:
            _abort_api_failure(f"{label} path compatibility check failed during run startup: {e}", phase="checking")

        if not raw_paths:
            log(f"WARN {label} path compatibility: API returned no movie file paths to validate against {LIBRARY_ROOT}.")
            continue

        matched = [raw for raw in raw_paths if resolve_under_library(raw)]
        if len(matched) != len(raw_paths):
            unmatched = [raw for raw in raw_paths if not resolve_under_library(raw)]
            examples = "; ".join(raw_paths[:3])
            _abort_api_failure(
                f"{label} reports movie paths that MediaReducer cannot match under {LIBRARY_ROOT}. "
                f"Check the media mounts, then rerun. "
                f"Examples: {examples}. Unmatched: {'; '.join(unmatched[:3])}",
                phase="checking",
            )
        log(f"{label} path compatibility: {len(matched)}/{len(raw_paths)} sampled path(s) matched under {LIBRARY_ROOT}.")


def log_blank():
    """Blank line to stdout and log file for visual separation between sections."""
    print("", flush=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOGFILE, "a") as f:
        f.write("\n")


def log_stage(title):
    """Consistent full-width banner that opens each run stage, so the log reads
    the same way from Startup through Summary."""
    log_blank()
    log("=" * 55)
    log(f"  {title}")
    log("=" * 55)


def reset_log():
    """Truncate lastrun.log at the start of each run so it only ever shows the most recent run."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGFILE.write_text("", encoding="utf-8")


def archive_log(run_start):
    """Copy lastrun.log into logs/<datetime>.log for any run that performed cleanup."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    dest = LOGS_DIR / f"{run_start}.log"
    shutil.copy2(LOGFILE, dest)
    log(f"Run log archived to: {dest}")


# ── Run-log archiving / retention ─────────────────────────────────────────────
# A run's log is archived exactly once, at process exit, so completed AND failed
# runs are always kept without sprinkling archive_log() through every early
# return. Interrupted (stopped) runs are archived only when the user opts in;
# retention pruning runs afterwards.
_RUN_START       = None
_RUN_ARCHIVABLE  = False
_RUN_INTERRUPTED = False
_RUN_FINALIZED   = False
_RUN_DELETED_FILES = False   # a live run really removed something — its log is evidence
_IN_DELETE_CRITICAL = False  # inside unlink()→deleted.log append (stop defers here)
_SIGTERM_DEFERRED = False    # a stop arrived mid-critical-section; exit right after it


def _prune_old_logs():
    """Delete archived run logs older than LOG_RETENTION_DAYS (0 = keep forever)."""
    days = parse_int(LOG_RETENTION_DAYS, 0)
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    try:
        for f in LOGS_DIR.glob("*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _finalize_run():
    """Archive this run's log (once) and prune old logs. Runs at process exit.

    Completed and failed runs are always archived, and so is any interrupted
    run that DELETED files (the log is the record of what a panic-stop removed).
    An interrupted run that deleted nothing is archived only when
    KEEP_INTERRUPTED_LOGS is on. Non-work invocations (Summary refresh,
    paused no-op) never set _RUN_ARCHIVABLE, so they are skipped entirely.
    """
    global _RUN_FINALIZED
    if _RUN_FINALIZED or not _RUN_ARCHIVABLE or not _RUN_START:
        return
    _RUN_FINALIZED = True
    try:
        # A stopped LIVE run that actually deleted files always archives: the
        # log is the only full record of what a panic-stopped run removed and
        # why — KEEP_INTERRUPTED_LOGS only governs runs that deleted nothing.
        if (not _RUN_INTERRUPTED) or KEEP_INTERRUPTED_LOGS or _RUN_DELETED_FILES:
            archive_log(_RUN_START)
        else:
            log("Run log not archived: run was interrupted, deleted nothing, and KEEP_INTERRUPTED_LOGS is off.")
        _prune_old_logs()
    except Exception as e:
        # Log housekeeping must never crash the interpreter on the way out — but a
        # SILENT failure here is exactly why archived logs can go missing, so make
        # it visible in stderr and (best-effort) in lastrun.log instead of hiding it.
        msg = (f"ERROR: could not archive run log to {LOGS_DIR}: {type(e).__name__}: {e}. "
               f"Check that the logs directory is writable inside the container.")
        try:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}", flush=True)
        except Exception:
            pass
        try:
            with open(LOGFILE, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
        except Exception:
            pass


def _handle_sigterm(signum, frame):
    """Turn a stop (SIGTERM from the web app) into a clean exit so _finalize_run
    still runs and can keep the partial log when the user has opted in.

    If the stop lands inside the delete-and-record critical section, the exit
    is DEFERRED a few milliseconds: exiting between unlink() and the
    deleted.log append would erase a real deletion from the history."""
    global _RUN_INTERRUPTED, _SIGTERM_DEFERRED
    _RUN_INTERRUPTED = True
    if _IN_DELETE_CRITICAL:
        _SIGTERM_DEFERRED = True
        return
    raise SystemExit(143)


def log_deleted(title, path, size_bytes=None, *, score=None, plays=None, last_played=None):
    """Append a single line to deleted.log for every real (non-dry-run) deletion.

    Carries the WHY alongside the what: retention score, play count, and last
    watch. deleted.log is the record that survives even a stopped run whose
    lastrun.log gets overwritten — without the rationale here, a user looking
    at the history later has no way to see why each movie was picked."""
    try:
        size_part = f" | size_bytes={int(size_bytes)}" if size_bytes is not None and int(size_bytes) >= 0 else ""
    except (TypeError, ValueError):
        size_part = ""
    why = ""
    try:
        if score is not None:
            why += f" | score={round(float(score), 1)}"
        if plays is not None:
            why += f" | plays={parse_int(plays, 0)}"
        if last_played is not None:
            why += f" | last_played={format_epoch(parse_int(last_played, 0))}"
    except Exception:
        pass   # rationale is best-effort; the deletion record itself must land
    entry = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {title} | {path}{size_part}{why}\n"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DELETED_LOG, "a", encoding="utf-8") as f:
        f.write(entry)


# Structured run-progress for the web UI. This is PURELY additive observability:
# emit_progress() only writes a small status file and never touches a scan,
# scoring, or deletion decision. Every write is wrapped so a progress-file
# problem (disk full, permissions, races) can never perturb or abort a run.
_PROGRESS: dict = {}
# When True (Summary / debug_info background refresh) emit_progress is a no-op so
# the dashboard progress panel keeps showing the last real run.
_QUIET_PROGRESS = False

def emit_progress(**fields):
    """Merge fields into the run-progress state and atomically write progress.json."""
    if _QUIET_PROGRESS:
        return
    try:
        _PROGRESS.update(fields)
        _PROGRESS["updated_at"] = time.time()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        # pid-unique tmp: the web app also writes progress.json (start stub,
        # terminal marker) — a shared tmp name lets two writers interleave and
        # publish a torn file, the exact hazard the cache writer avoids.
        tmp = PROGRESS_FILE.with_name(f"{PROGRESS_FILE.name}.{_os.getpid()}.tmp")
        tmp.write_text(json.dumps(_PROGRESS), encoding="utf-8")
        tmp.replace(PROGRESS_FILE)
    except Exception:
        # Observability must never break a run.
        pass


def emit_stats(**fields):
    """Merge dashboard storage stats into cache.json.

    Written on every run regardless of mode, including the quiet Summary refresh.
    Merges with the existing cache so a transient library-size failure (library_gb
    not supplied) preserves the last-known value instead of blanking it.
    """
    try:
        with _cache_write_lock():
            data = _cache_base_for_merge()
            dashboard_stats = data.get("dashboard_stats")
            if not isinstance(dashboard_stats, dict):
                dashboard_stats = {}
            dashboard_stats.update(fields)
            dashboard_stats["updated_at"] = time.time()
            data["dashboard_stats"] = dashboard_stats
            _replace_cache_file(data)
    except Exception:
        pass


def bytes_to_gb(num):
    return num / 1_000_000_000



def get_usage_info():
    total, used, free = shutil.disk_usage(CHECK_PATH)
    used_gb = round(bytes_to_gb(used), 1)
    max_gb = round(bytes_to_gb(total) - HEADROOM_GB, 1)
    return {"total": total, "used": used, "free": free, "used_gb": used_gb, "max_gb": max_gb}


def log_usage():
    info = get_usage_info()
    log(
        f"Storage check path: {CHECK_PATH} | "
        f"Used: {info['used_gb']:.1f} GB / "
        f"{bytes_to_gb(info['total']):.1f} GB | "
        f"Free: {bytes_to_gb(info['free']):.1f} GB | "
        f"MAX_GB={info['max_gb']:.1f} GB ({bytes_to_gb(info['total']):.1f} GB total - {HEADROOM_GB} GB headroom)"
    )


def format_epoch(ts):
    try:
        ts = int(ts)
        if ts <= 0:
            return "never"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return "unknown"


def parse_int(value, default=0):
    try:
        if value in (None, "", "None"):
            return default
        return int(float(value))
    except Exception:
        return default


def _as_list(value):
    """Normalize API fields that may be a single dict, a list, or blank."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _protected_name_set(names):
    """Case-insensitive collection-name set for API comparisons."""
    return {str(name).strip().lower() for name in (names or []) if str(name).strip()}


def _item_raw_paths(item):
    """Raw file paths from Plex or Jellyfin item payloads."""
    paths = set()
    if item.get("Path"):
        paths.add(str(item.get("Path")).strip())
    for ms in _as_list(item.get("MediaSources")):
        if isinstance(ms, dict) and ms.get("Path"):
            paths.add(str(ms.get("Path")).strip())
    for media in _as_list(item.get("Media")):
        if not isinstance(media, dict):
            continue
        for part in _as_list(media.get("Part")):
            if isinstance(part, dict) and part.get("file"):
                paths.add(str(part.get("file")).strip())
    return {p for p in paths if p}


def _item_resolved_paths(item):
    """Resolved /library path strings from Plex or Jellyfin item payloads."""
    paths = set()
    for raw in _item_raw_paths(item):
        resolved = resolve_under_library(raw)
        if resolved:
            paths.add(str(resolved))
    return paths


def _item_provider_ids(item):
    """Normalized (imdb_ids, tmdb_ids) from Jellyfin ProviderIds or Plex Guid data."""
    imdbs = set()
    tmdbs = set()
    prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
    if prov.get("imdb"):
        imdbs.add(str(prov.get("imdb")).strip().lower())
    if prov.get("tmdb"):
        tmdbs.add(str(prov.get("tmdb")).strip())

    guid_values = []
    if item.get("guid"):
        guid_values.append(str(item.get("guid")))
    for guid in _as_list(item.get("Guid")):
        if isinstance(guid, dict) and guid.get("id"):
            guid_values.append(str(guid.get("id")))
        elif isinstance(guid, str):
            guid_values.append(guid)
    for guid in guid_values:
        low = guid.strip().lower()
        if low.startswith("imdb://"):
            imdbs.add(low.split("imdb://", 1)[1])
        elif low.startswith("tmdb://"):
            tmdbs.add(low.split("tmdb://", 1)[1])
    return {i for i in imdbs if i}, {t for t in tmdbs if t}


def tautulli_api(cmd, **params):
    params.update({"apikey": TAUTULLI_API_KEY, "cmd": cmd})
    url = f"{TAUTULLI_URL.rstrip('/')}/api/v2?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read().decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # A reverse proxy or the server itself answered with an HTML error
        # page — diagnose the cause instead of surfacing a raw decode trace.
        raise RuntimeError(f"Tautulli returned non-JSON for cmd={cmd} "
                           f"(proxy error page or wrong URL?): {raw[:120]!r}")
    if payload.get("response", {}).get("result") != "success":
        raise RuntimeError(f"Tautulli API error: {payload}")
    return payload["response"]["data"]


def http_request(method, url, headers=None, body=None):
    """Generic HTTP helper for Radarr API calls."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw) if raw else {}


def _movie_folder_name(path_value):
    raw = str(path_value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    raw = raw.rstrip("/")
    if not raw:
        return ""
    p = Path(raw)
    if p.suffix.lower() in MOVIE_EXTENSIONS:
        return p.parent.name.lower()
    return p.name.lower()


def _radarr_movie_matches_deleted_path(radarr_movie, deleted_path):
    if not isinstance(radarr_movie, dict):
        return False
    deleted_folder = _movie_folder_name(deleted_path)
    if not deleted_folder:
        return False
    for key in ("path", "folderName"):
        value = radarr_movie.get(key)
        if value and _movie_folder_name(value) == deleted_folder:
            return True
    return False


def resolve_under_library(path_str):
    """Map a media-server-reported path to the real file under /library, or None.

    Media servers often see the same files at a different container root, so the
    longest existing trailing run of path segments wins:

        /data/Movies/Film (2020)/film.mkv
          -> /library/data/Movies/Film (2020)/film.mkv   (miss)
          -> /library/Movies/Film (2020)/film.mkv        (hit)

    The match must keep at least parent-folder + filename — a movie's folder is
    part of its identity, and a bare filename could match an unrelated film with
    the same name and delete the wrong movie.
    """
    if not path_str:
        return None
    raw = str(path_str).strip().replace("\\", "/")
    if not raw:
        return None

    # Already a real /library path (e.g. Plex itself points at /library).
    if raw == str(LIBRARY_ROOT) or raw.startswith(str(LIBRARY_ROOT) + "/"):
        rel = raw[len(str(LIBRARY_ROOT)):].lstrip("/")
        p = LIBRARY_ROOT / rel if rel else LIBRARY_ROOT
        return p if p.exists() else None

    parts = [seg for seg in raw.split("/") if seg]
    if len(parts) < 2:
        return None

    # Stop before the bare-filename-only tail: require folder + file to line up.
    for start in range(len(parts) - 1):
        candidate = LIBRARY_ROOT.joinpath(*parts[start:])
        if candidate.exists():
            return candidate
    return None


def monitored_roots():
    """
    The subtrees under /library the script is allowed to manage. An empty
    MONITOR_DIRS is intentionally a safe no-op: manage nothing until the user
    explicitly adds at least one library path. Entries may be absolute
    (/library/Movies) or bare names (Movies).
    """
    roots = []
    for d in MONITOR_DIRS:
        s = str(d).strip().replace("\\", "/")
        if not s:
            continue
        if s in ("/", str(LIBRARY_ROOT), "library"):
            p = LIBRARY_ROOT
        elif s.startswith(str(LIBRARY_ROOT) + "/"):
            rel = s[len(str(LIBRARY_ROOT)):].strip("/")
            p = LIBRARY_ROOT / rel
        elif s.startswith("library/"):
            rel = s[len("library/"):].strip("/")
            p = LIBRARY_ROOT / rel
        else:
            rel = s.strip("/")
            p = LIBRARY_ROOT / rel
        if not str(p):
            continue
        if p not in roots:
            roots.append(p)
    return roots


def compute_config_hash():
    """Hash only the config that can change a movie's CACHED metadata, so a change
    forces a full re-fetch. Narrow by design: EXCLUDES thresholds, scoring,
    scheduling, logging, Radarr, and monitored paths — none touch a movie's cached
    tmdb_id/imdb_id/protected status, so changing them keeps the cache. INCLUDES
    anything that changes where metadata comes from (connections, which servers)
    or how protection resolves (protected collections). When in doubt, include it
    — a needless reset is cheap, a missed one is not.
    """
    data = json.dumps({
        # Metadata source — tmdb_id / imdb_id are derived from here.
        "TAUTULLI_URL":                   TAUTULLI_URL,
        "TAUTULLI_API_KEY":               TAUTULLI_API_KEY,
        # Protection source.
        "PLEX_URL":                       PLEX_URL,
        "PLEX_TOKEN":                     PLEX_TOKEN,
        "PROTECTED_COLLECTIONS":          sorted(PROTECTED_COLLECTIONS),
        # Which servers are active + the Jellyfin source/protection.
        "USE_PLEX":                       bool(USE_PLEX),
        "USE_JELLYFIN":                   bool(USE_JELLYFIN),
        "JELLYFIN_URL":                   JELLYFIN_URL,
        "JELLYFIN_API_KEY":               JELLYFIN_API_KEY,
        "JELLYFIN_PROTECTED_COLLECTIONS": sorted(JELLYFIN_PROTECTED_COLLECTIONS),
    }, sort_keys=True)
    return hashlib.md5(data.encode()).hexdigest()


_CODE_CHECKSUM = None

def code_checksum():
    """SHA-256 of this engine source file, computed once per process.

    Replaces manual cache versioning: all code that reads/writes/derives cache
    content lives here, so a file change changes the checksum and flushes the
    cache automatically — the cache can never be read by code other than the code
    that wrote it, with no version number to bump. engine.py ONLY: the cache holds
    raw API facts whose shape lives entirely in this file. scoring_constants.py is
    excluded because scores are recomputed every run and never cached — including
    it would force a full API refetch on every scoring tweak for no benefit.
    """
    global _CODE_CHECKSUM
    if _CODE_CHECKSUM is None:
        try:
            _CODE_CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        except Exception as e:
            # If the source can't be read for any reason, fall back to a constant
            # so the cache still works (rather than flushing every run).
            log(f"WARN could not checksum engine source ({e}); cache code-guard disabled.")
            _CODE_CHECKSUM = "unknown"
    return _CODE_CHECKSUM

# =========================
# CONFIG FILE LOADER
# =========================
# When MEDIAREDUCER_CONFIG is set (e.g. by the Docker web UI), all settings
# below are overridden by values from that JSON file on each run. When not
# set, the hardcoded values above are used as-is (standalone script mode).
_CONFIG_FILE     = _os.environ.get("MEDIAREDUCER_CONFIG", "")
_MODE_OVERRIDE   = _os.environ.get("MEDIAREDUCER_MODE_OVERRIDE", "")
# Set by the web app for Dashboard-button runs. A manual Live Run prunes to
# every breached target immediately — the deletion delay and the once-per-day
# window pace AUTOMATIC runs, not a deliberate button press.
_MANUAL_RUN      = _os.environ.get("MEDIAREDUCER_MANUAL", "") == "1"
# sample_pool mode only: how many movies to pull (Score Explorer batch size).
_SAMPLE_TARGET   = _os.environ.get("MEDIAREDUCER_SAMPLE_TARGET", "")


def _normalize_library_path(value):
    """
    Normalise one Movie-Library-Paths entry to an absolute path under the
    library root. Accepts bare names ("Movies"), root-relative, already-absolute,
    and root selectors ("/" or the root). Returns None only for blanks.
    """
    root = str(LIBRARY_ROOT)
    root_name = root.rsplit("/", 1)[-1] or "library"
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw in ("/", root, root_name):
        return root
    if raw.startswith(root + "/"):
        suffix = raw[len(root) + 1:]
    elif raw.startswith(root_name + "/"):
        suffix = raw[len(root_name) + 1:]
    elif raw.startswith("/"):
        # Unknown absolute path outside the library root. Its root can't be
        # inferred, so keep only the library folder name -> root/<name>.
        suffix = raw.rstrip("/").split("/")[-1]
    else:
        suffix = raw.lstrip("/")
    suffix = suffix.strip("/")
    return f"{root}/{suffix}" if suffix else None


def _monitor_dirs_from_config(cfg):
    """
    Build the optional allow-list from the config's MONITOR_DIRS. Path maps are
    gone, so each entry is simply normalised to a /library path. An empty result
    means 'manage nothing'; '/' or '/library' explicitly means all of /library.
    """
    monitor_dirs = []
    for raw in _coerce_string_list(cfg.get("MONITOR_DIRS", []), "MONITOR_DIRS"):
        normalized = _normalize_library_path(raw)
        if normalized and normalized not in monitor_dirs:
            monitor_dirs.append(normalized)
    return monitor_dirs


def _coerce_config_number(raw, name, *, allow_none=False, min_value=None, max_value=None, default=None):
    """Coerce numeric config values safely and record manual-edit errors."""
    if raw is None or (allow_none and str(raw).strip().lower() in ("", "none", "null")):
        return None if allow_none else default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        CONFIG_ERRORS.append(f"{name} must be a number.")
        return default
    if min_value is not None and value < min_value:
        CONFIG_ERRORS.append(f"{name} must be {min_value} or greater.")
        return default
    if max_value is not None and value > max_value:
        CONFIG_ERRORS.append(f"{name} must be {max_value} or lower.")
        return default
    if float(value).is_integer():
        return int(value)
    return value


def _coerce_config_positive_or_none(raw, name, *, default=None):
    """Coerce optional positive numeric config values safely."""
    if raw is None or str(raw).strip().lower() in ("", "none", "null"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        CONFIG_ERRORS.append(f"{name} must be a number, or null to disable it.")
        return default
    if value <= 0:
        CONFIG_ERRORS.append(f"{name} must be greater than zero, or null to disable it.")
        return default
    if float(value).is_integer():
        return int(value)
    return value


def _coerce_string_list(raw, name):
    """
    Coerce a UI/manual JSON value that should be a list of strings.
    Accepts either a JSON list or a comma/newline-separated string.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[\n,]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        CONFIG_ERRORS.append(f"{name} must be a list of text values.")
        return []
    out = []
    for item in parts:
        value = str(item).strip()
        if value and value not in out:
            out.append(value)
    return out


def _coerce_movie_extensions(raw):
    values = _coerce_string_list(raw, "MOVIE_EXTENSIONS")
    extensions = set()
    for value in values:
        ext = value.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        extensions.add(ext)
    return extensions


def _coerce_config_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _load_config_from_file():
    """
    Load configuration from the JSON file pointed to by MEDIAREDUCER_CONFIG.
    Overwrites module-level config globals with values from the file.
    Type-coerces each value back to what the rest of the script expects:
      - MONITOR_DIRS:          list of str       → normalized /library paths (allow-list)
      - PROTECTED_COLLECTIONS: list of str       → set of str
      - MOVIE_EXTENSIONS:      list of str       → set of str
      - *APPDATA paths:        forced to the fixed Docker mounts
      - OUTPUT_DIR:            str               → Path (also rebuilds sub-paths)
    """
    if not _CONFIG_FILE or not _os.path.exists(_CONFIG_FILE):
        return

    import json as _j
    with open(_CONFIG_FILE) as _f:
        _c = _j.load(_f)

    # Adopt the configured operating time zone before anything reads the
    # clock — log timestamps, the daily-run window, and deletion-delay aging
    # all key off local time. The web app validates the zone and exports TZ
    # when it launches us; applying it here too covers manual CLI runs.
    _tz = str(_c.get("TIME_ZONE") or "").strip()
    if _tz and _tz.lower() != "auto":
        try:
            _os.environ["TZ"] = _tz
            time.tzset()
        except Exception:
            pass

    global CONFIG_ERRORS
    global RUN_MODE, HEADROOM_GB, REDLINE_GB, MAX_LIBRARY_GB, MAX_HEADROOM_PCT, DELETE_DELAY_DAYS
    global DAILY_RUN_TIME
    global MONITOR_DIRS, MOVIE_EXTENSIONS
    global PROTECTED_COLLECTIONS, GRACE_PERIOD_DAYS, SKIP_UNPLAYED_MOVIES, PROTECT_JELLYFIN_FAVORITES, MAX_IMDB_RATING
    global SCORE_BALANCE, HISTORY_WEIGHT, QUALITY_WEIGHT, NEAR_TIE_PTS, MAX_STALENESS_MONTHS
    global RADARR_OVERSEERR_SECTION_ID, RADARR_OVERSEERR_SECTION_ID_SOURCE, IMDB_RATINGS_URL, IMDB_RATINGS_MAX_AGE_DAYS
    global LOG_RETENTION_DAYS, KEEP_INTERRUPTED_LOGS
    global TAUTULLI_APPDATA, RADARR_APPDATA
    global TAUTULLI_URL, TAUTULLI_API_KEY, PLEX_URL, PLEX_TOKEN
    global RADARR_URL, RADARR_API_KEY
    global USE_PLEX, USE_JELLYFIN, JELLYFIN_URL, JELLYFIN_API_KEY, JELLYFIN_PROTECTED_COLLECTIONS
    global OUTPUT_DIR, CHECK_PATH, LIBRARY_ROOT, LOGFILE, LOGS_DIR, DELETED_LOG
    global IMDB_RATINGS_PATH, CACHE_FILE, PROGRESS_FILE, PENDING_FILE

    CONFIG_ERRORS = []

    # Verbatim copies of the deletion-affecting keys for the plan stamp —
    # the same raw JSON values the web app compares against.
    _PLAN_CONFIG_RAW.clear()
    _PLAN_CONFIG_RAW.update({k: _c.get(k) for k in _PLAN_CONFIG_KEYS})

    if "RUN_MODE"                   in _c: RUN_MODE                   = _c["RUN_MODE"]
    if "HEADROOM_GB"                in _c: HEADROOM_GB                = _coerce_config_number(_c["HEADROOM_GB"], "HEADROOM_GB", min_value=0, default=0)
    if "REDLINE_GB"                 in _c: REDLINE_GB                 = _coerce_config_number(_c["REDLINE_GB"], "REDLINE_GB", allow_none=True, min_value=0, default=None)
    if "MAX_LIBRARY_GB"             in _c: MAX_LIBRARY_GB             = _coerce_config_positive_or_none(_c["MAX_LIBRARY_GB"], "MAX_LIBRARY_GB", default=None)
    # Defensive floor of 1 so a run never deletes the same day, whatever
    # config.json holds (the app rejects a below-1 value before a run starts).
    if "DELETE_DELAY_DAYS"          in _c: DELETE_DELAY_DAYS          = max(1, int(_coerce_config_number(_c["DELETE_DELAY_DAYS"], "DELETE_DELAY_DAYS", min_value=0, max_value=365, default=1)))
    if "DAILY_RUN_TIME" in _c:
        _drt = str(_c["DAILY_RUN_TIME"] or "").strip()
        DAILY_RUN_TIME = _drt if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", _drt) else "00:00"
    if "MAX_HEADROOM_PCT"           in _c: MAX_HEADROOM_PCT           = _coerce_config_number(_c["MAX_HEADROOM_PCT"], "MAX_HEADROOM_PCT", min_value=0.000001, max_value=100, default=15)
    if "GRACE_PERIOD_DAYS"          in _c: GRACE_PERIOD_DAYS          = _coerce_config_number(_c["GRACE_PERIOD_DAYS"], "GRACE_PERIOD_DAYS", min_value=0, default=GRACE_PERIOD_DAYS)
    if "SKIP_UNPLAYED_MOVIES"       in _c: SKIP_UNPLAYED_MOVIES       = _coerce_config_bool(_c["SKIP_UNPLAYED_MOVIES"])
    if "MAX_IMDB_RATING"            in _c: MAX_IMDB_RATING            = _coerce_config_number(_c["MAX_IMDB_RATING"], "MAX_IMDB_RATING", allow_none=True, min_value=0, max_value=10, default=None)
    if "PROTECT_JELLYFIN_FAVORITES" in _c: PROTECT_JELLYFIN_FAVORITES = _coerce_config_bool(_c["PROTECT_JELLYFIN_FAVORITES"])

    # SCORE_BALANCE is the only scoring knob; unknown keys are ignored.
    if "SCORE_BALANCE" in _c: SCORE_BALANCE = _coerce_config_number(_c["SCORE_BALANCE"], "SCORE_BALANCE", min_value=0, max_value=100, default=SCORE_BALANCE)
    HISTORY_WEIGHT, QUALITY_WEIGHT = score_balance_weights(SCORE_BALANCE)
    # Near-tie window in score points; None turns file size optimization off.
    if "NEAR_TIE_PTS" in _c: NEAR_TIE_PTS = _coerce_config_number(_c["NEAR_TIE_PTS"], "NEAR_TIE_PTS", allow_none=True, min_value=0.5, max_value=25, default=2.0)
    if "MAX_STALENESS_MONTHS" in _c: MAX_STALENESS_MONTHS = _coerce_config_number(_c["MAX_STALENESS_MONTHS"], "MAX_STALENESS_MONTHS", min_value=1, max_value=120, default=SCORING["RECENCY_DEFAULT_MONTHS"])
    if "RADARR_OVERSEERR_SECTION_ID" in _c:
        _section = str(_c["RADARR_OVERSEERR_SECTION_ID"]).strip() if _c["RADARR_OVERSEERR_SECTION_ID"] is not None else ""
        RADARR_OVERSEERR_SECTION_ID = None if _section.lower() in ("", "none", "null") else _section
        RADARR_OVERSEERR_SECTION_ID_SOURCE = "disabled" if RADARR_OVERSEERR_SECTION_ID is None else ("auto" if _section.lower() == "auto" else "manual")
    if _c.get("IMDB_RATINGS_URL"):      IMDB_RATINGS_URL      = str(_c["IMDB_RATINGS_URL"]).strip()
    if "IMDB_RATINGS_MAX_AGE_DAYS"  in _c: IMDB_RATINGS_MAX_AGE_DAYS  = _coerce_config_number(_c["IMDB_RATINGS_MAX_AGE_DAYS"], "IMDB_RATINGS_MAX_AGE_DAYS", min_value=0, default=IMDB_RATINGS_MAX_AGE_DAYS)
    if "LOG_RETENTION_DAYS"         in _c: LOG_RETENTION_DAYS         = max(0, parse_int(_c["LOG_RETENTION_DAYS"], LOG_RETENTION_DAYS))
    if "KEEP_INTERRUPTED_LOGS"      in _c: KEEP_INTERRUPTED_LOGS      = _coerce_config_bool(_c["KEEP_INTERRUPTED_LOGS"])
    # Connection fields in config.json are the actual saved values. A blank
    # URL with its credential present falls back to the default address the
    # web app snapshotted at save time (_SERVICE_URL_DEFAULTS); a blank
    # credential means the service is off — never probe a default for it.
    if "TAUTULLI_URL"      in _c: TAUTULLI_URL      = str(_c.get("TAUTULLI_URL") or "").strip()
    if "TAUTULLI_API_KEY"  in _c: TAUTULLI_API_KEY  = str(_c.get("TAUTULLI_API_KEY") or "").strip()
    if "PLEX_URL"          in _c: PLEX_URL          = str(_c.get("PLEX_URL") or "").strip()
    if "PLEX_TOKEN"        in _c: PLEX_TOKEN        = str(_c.get("PLEX_TOKEN") or "").strip()
    if "RADARR_URL"        in _c: RADARR_URL        = str(_c.get("RADARR_URL") or "").strip()
    if "RADARR_API_KEY"    in _c: RADARR_API_KEY    = str(_c.get("RADARR_API_KEY") or "").strip()
    if "USE_PLEX"          in _c: USE_PLEX          = _coerce_config_bool(_c["USE_PLEX"])
    if "USE_JELLYFIN"      in _c: USE_JELLYFIN      = _coerce_config_bool(_c["USE_JELLYFIN"])
    if "JELLYFIN_URL"      in _c: JELLYFIN_URL      = str(_c.get("JELLYFIN_URL") or "").strip()
    if "JELLYFIN_API_KEY"  in _c: JELLYFIN_API_KEY  = str(_c.get("JELLYFIN_API_KEY") or "").strip()
    _url_defaults = _c.get("_SERVICE_URL_DEFAULTS") or {}
    if not TAUTULLI_URL and TAUTULLI_API_KEY:
        TAUTULLI_URL = str(_url_defaults.get("TAUTULLI_URL") or "").strip()
    if not PLEX_URL and PLEX_TOKEN:
        PLEX_URL = str(_url_defaults.get("PLEX_URL") or "").strip()
    if not RADARR_URL and RADARR_API_KEY:
        RADARR_URL = str(_url_defaults.get("RADARR_URL") or "").strip()
    if not JELLYFIN_URL and JELLYFIN_API_KEY:
        JELLYFIN_URL = str(_url_defaults.get("JELLYFIN_URL") or "").strip()
    if "JELLYFIN_PROTECTED_COLLECTIONS" in _c:
        JELLYFIN_PROTECTED_COLLECTIONS = set(_coerce_string_list(_c["JELLYFIN_PROTECTED_COLLECTIONS"], "JELLYFIN_PROTECTED_COLLECTIONS"))

    # Required allow-list of /library subfolders. Empty = manage nothing.
    if "MONITOR_DIRS" in _c:
        MONITOR_DIRS = _monitor_dirs_from_config(_c)
    if "MOVIE_EXTENSIONS"      in _c: MOVIE_EXTENSIONS      = _coerce_movie_extensions(_c["MOVIE_EXTENSIONS"])
    if "PROTECTED_COLLECTIONS" in _c: PROTECTED_COLLECTIONS = set(_coerce_string_list(_c["PROTECTED_COLLECTIONS"], "PROTECTED_COLLECTIONS"))

    # Deployment roots come from the environment, never the saved config —
    # re-read them so a reload can't leave stale values.
    TAUTULLI_APPDATA  = _root_from_env("MEDIAREDUCER_TAUTULLI_APPDATA", "/tautulli")
    RADARR_APPDATA    = _root_from_env("MEDIAREDUCER_RADARR_APPDATA", "/radarr")
    CHECK_PATH        = _root_from_env("MEDIAREDUCER_LIBRARY", "/library")
    LIBRARY_ROOT      = CHECK_PATH

    # OUTPUT_DIR also drives all sub-paths — update them together
    if "OUTPUT_DIR" in _c:
        OUTPUT_DIR        = Path(_c["OUTPUT_DIR"])
        LOGFILE           = OUTPUT_DIR / "lastrun.log"
        LOGS_DIR          = OUTPUT_DIR / "logs"
        DELETED_LOG       = OUTPUT_DIR / "deleted.log"
        IMDB_RATINGS_PATH = OUTPUT_DIR / "title.ratings.tsv"
        CACHE_FILE        = OUTPUT_DIR / "cache.json"
        PROGRESS_FILE     = OUTPUT_DIR / "progress.json"
        PENDING_FILE      = OUTPUT_DIR / "pending_deletions.json"

    _resolve_auto_radarr_overseerr_section_id()


@contextmanager
def _cache_write_lock():
    """Serialize cache.json read-modify-writes ACROSS PROCESSES: a sample-pool
    build runs without the app's run lock, so it can execute concurrently with
    a scan or summary. Every writer takes this flock and writes through a
    pid-unique tmp file; readers need nothing — a replace() is atomic, so they
    always see a complete file."""
    if fcntl is None:
        yield
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE.with_name(CACHE_FILE.name + ".lock"), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _replace_cache_file(data: dict) -> None:
    """Atomic cache.json write via a pid-unique tmp (two engine processes must
    never share a tmp path — interleaved writes to one tmp can tear the file).
    Every write stamps the current code checksum, so the file always identifies
    the code version that produced it."""
    data["code_checksum"] = code_checksum()
    tmp = CACHE_FILE.with_name(f"{CACHE_FILE.name}.{_os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def _cache_base_for_merge() -> dict:
    """Read cache.json as the base for a read-modify-write. When the on-disk
    checksum doesn't match this code, its code-derived sections (movie metadata,
    sample_pool, dashboard_stats) were written by a different version and must be
    rebuilt, not carried forward — so drop them, keeping only the daily-schedule
    date. Every writer starts from this base so no stale section survives a code
    change through the back door."""
    try:
        existing = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            return {}
    except Exception:
        return {}
    if existing.get("code_checksum") != code_checksum():
        return {"last_cleanup_date": existing.get("last_cleanup_date")}
    return existing


def load_cache():
    """
    Load the JSON cache file. If the file is missing, corrupt, or was written by
    a different version of the engine code (checksum mismatch), returns a fresh
    dict (preserving last_cleanup_date so the daily schedule is unaffected).
    """
    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            raise ValueError(f"cache holds {type(cache).__name__}, not an object")
    except FileNotFoundError:
        return {"code_checksum": code_checksum()}
    except Exception as e:
        log(f"WARN cache unreadable ({e}), starting fresh.")
        return {"code_checksum": code_checksum()}

    if cache.get("code_checksum") != code_checksum():
        log(
            "Engine code changed since the cache was written — clearing the "
            "movie metadata, library sample, and stored stats for a fresh "
            "rebuild (daily schedule preserved)."
        )
        # Everything code-derived (metadata, sample_pool, dashboard_stats) is
        # rebuilt; only the schedule date survives.
        return {
            "code_checksum":     code_checksum(),
            "last_cleanup_date": cache.get("last_cleanup_date"),
        }

    return cache


def save_cache(cache):
    """Persist the cache dict to disk (the current code checksum is stamped by
    _replace_cache_file)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with _cache_write_lock():
        # dashboard_stats and sample_pool are only ever written straight to disk
        # (emit_stats / _write_sample_pool_file); re-read them under the lock so a
        # scan's stale in-memory dict cannot clobber a value updated mid-scan.
        # _cache_base_for_merge drops them when the on-disk cache is from older
        # code, so a code change flushes them rather than merging stale copies.
        existing = _cache_base_for_merge()
        for key in ("dashboard_stats", "sample_pool"):
            if isinstance(existing.get(key), dict):
                cache[key] = existing[key]
        _replace_cache_file(cache)


def _mark_age_days(marked_at, now_ts) -> int:
    """Calendar days (local time) between a mark and now. Day-granular on
    purpose: runs happen just after midnight, so second-granular aging would
    quietly stretch a 7-day delay into 8."""
    try:
        a = time.localtime(float(marked_at))
        b = time.localtime(float(now_ts))
        return (_dt.date(b.tm_year, b.tm_mon, b.tm_mday)
                - _dt.date(a.tm_year, a.tm_mon, a.tm_mday)).days
    except (TypeError, ValueError, OverflowError):
        return 0


def _mark_delete_on(marked_at) -> str:
    """The calendar date a mark becomes deletable (marked date + delay)."""
    try:
        t = time.localtime(float(marked_at))
        return str(_dt.date(t.tm_year, t.tm_mon, t.tm_mday)
                   + _dt.timedelta(days=int(DELETE_DELAY_DAYS)))
    except (TypeError, ValueError, OverflowError):
        return ""


def load_pending() -> dict:
    """The marked-for-deletion queue: {path: {title, size_bytes, score,
    marked_at, trigger}}. Missing or unreadable reads as empty — marks are a
    grace-window plan, never a deletion authorization (a mark only deletes
    while the movie is STILL in the run's eligible candidate list)."""
    try:
        data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        return dict(entries) if isinstance(entries, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"WARN: {PENDING_FILE.name} unreadable ({e}); treating the queue as empty.")
        return {}


# Everything that changes WHAT a run would mark or delete. A completed
# Simulate stamps the raw config values of these keys (plus the monitored
# paths) into the plan; the web app ghosts BOTH Live actions — arming
# automatic mode and the manual Live Run button — whenever any of them no
# longer matches, forcing a fresh Simulate. Mirrored in app.py
# (_PLAN_CONFIG_KEYS); keep the two lists identical.
_PLAN_CONFIG_KEYS = (
    "HEADROOM_GB", "REDLINE_GB", "MAX_LIBRARY_GB",
    "GRACE_PERIOD_DAYS", "SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES",
    "MAX_IMDB_RATING", "SCORE_BALANCE", "NEAR_TIE_PTS", "MAX_STALENESS_MONTHS",
    "PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS", "MOVIE_EXTENSIONS",
)
# Raw config values for those keys, captured verbatim at config load so the
# stamp and the app compare like with like (both sides read config.json).
_PLAN_CONFIG_RAW: dict = {}


def save_pending(entries: dict, *, stamp_thresholds: bool = False) -> None:
    """Atomically persist the marked-for-deletion queue.

    A plan-defining write (Simulate, or a daily run marking candidates) stamps
    the deletion-affecting config it was computed under — the web app ghosts
    the user-facing Live actions until a plan exists for the CURRENT config.
    Trim-only writes (redline deletions, upkeep) keep the original stamp so
    they can't accidentally freshen a plan made under different rules."""
    try:
        if stamp_thresholds:
            plan_config = dict(_PLAN_CONFIG_RAW)
            # Which paths the plan was scanned from: a monitored-path change
            # invalidates it outright (only a real Simulate can rebuild it).
            monitor_dirs = sorted(str(d) for d in (MONITOR_DIRS or []))
        else:
            try:
                _old = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
                plan_config = _old.get("plan_config") if isinstance(_old, dict) else None
                monitor_dirs = _old.get("monitor_dirs") if isinstance(_old, dict) else None
            except Exception:
                plan_config = None
                monitor_dirs = None
        payload = {"schema": 1, "entries": entries}
        if isinstance(plan_config, dict):
            payload["plan_config"] = plan_config
        if isinstance(monitor_dirs, list):
            payload["monitor_dirs"] = monitor_dirs
        tmp = PENDING_FILE.with_name(f"{PENDING_FILE.name}.{_os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(PENDING_FILE)
    except Exception as e:
        log(f"WARN: could not write {PENDING_FILE.name} ({e}).")


def write_plan_to_queue(planned, trigger) -> tuple[dict, int, int]:
    """Persist a computed deletion plan as the marked-for-deletion queue.
    planned = [(candidate, size_bytes), ...] in deletion order. Existing marks
    KEEP their original marked_at — a re-Simulate reshuffling the plan never
    resets how long a movie has been marked; only movies newly entering the
    plan start a fresh clock. Marks not in this plan drop off."""
    mark_store = load_pending()
    kept: dict = {}
    new_marks = 0
    now_ts = time.time()
    for cand, size in planned:
        key = str(cand["path"])
        entry = mark_store.get(key)
        if isinstance(entry, dict):
            # Existing mark: preserve how long it has been marked (marked_at) and
            # its original trigger, but refresh the display fields to THIS plan —
            # re-Simulating under a new scoring balance must not leave stale
            # scores/titles in the queue (the deleted-history modal reads these).
            entry["title"] = cand["title"]
            entry["score"] = round(cand["retention_score"], 3)
        else:
            entry = {"title": cand["title"],
                     "score": round(cand["retention_score"], 3),
                     "marked_at": now_ts, "trigger": trigger}
            new_marks += 1
        entry["size_bytes"] = size
        kept[key] = entry
    dropped = len(mark_store) - sum(1 for k in mark_store if k in kept)
    save_pending(kept, stamp_thresholds=True)
    if DELETE_DELAY_DAYS > 0:
        log(f"Marked for deletion: {len(kept)} movie(s) ({new_marks} new"
            f"{f', {dropped} unmarked' if dropped else ''}) — each deletable "
            f"{DELETE_DELAY_DAYS} day(s) after its mark; only eligible marks delete.")
    else:
        log(f"Marked for deletion: {len(kept)} movie(s) ({new_marks} new"
            f"{f', {dropped} unmarked' if dropped else ''}) — eligible now; "
            f"the next daily run deletes them.")
    return kept, new_marks, dropped


def read_last_cleanup_date():
    """Return date string (YYYY-MM-DD) of the last scheduled cleanup, or None."""
    return load_cache().get("last_cleanup_date")


def write_last_cleanup_date():
    """Record today as the last scheduled cleanup date inside the cache file."""
    cache = load_cache()
    cache["last_cleanup_date"] = time.strftime("%Y-%m-%d")
    save_cache(cache)
    log(f"Daily cleanup state saved: {time.strftime('%Y-%m-%d')} -> {CACHE_FILE}")


def debug_startup():
    log_stage("STARTUP")
    log(f"RUN_MODE={RUN_MODE}")
    log(f"CHECK_PATH={CHECK_PATH}")
    log_usage()
    log(f"LIBRARY_ROOT={LIBRARY_ROOT} | mounted={LIBRARY_ROOT.exists()}")
    if MONITOR_DIRS:
        log("MONITORED LIBRARY PATHS (allow-list):")
        for root in monitored_roots():
            log(f"  {root} | exists={root.exists()}")
    else:
        log("MONITORED LIBRARY PATHS: (none set — managing nothing)")

    for _name, _base, _marker in (
        ("tautulli",  TAUTULLI_APPDATA,  "config.ini"),
        ("radarr",    RADARR_APPDATA,    "config.xml"),
    ):
        _found = _find_appdata_file(_base, _marker)
        log(
            f"Appdata {_name}: mounted={Path(_base).exists()} | "
            f"{_marker}={'found at ' + str(_found) if _found else 'NOT FOUND'}"
        )

    _i = get_usage_info()
    log(f"Headroom: {HEADROOM_GB} GB | Redline: {REDLINE_GB} GB | Library cap: {str(MAX_LIBRARY_GB) + ' GB' if MAX_LIBRARY_GB is not None else 'disabled'} | Safety cap: {MAX_HEADROOM_PCT}% | Max usage: {_i['max_gb']:.1f} GB | Filesystem: {bytes_to_gb(_i['total']):.1f} GB | Current: {_i['used_gb']:.1f} GB")
    # Redline is the only trigger that bypasses the deletion delay. Without it, a
    # fast-filling library can run out of space before marks age out. 1 day is
    # the minimum grace everyone has; flag only a longer delay (2+), where that
    # window is meaningfully wider.
    if DELETE_DELAY_DAYS >= 2 and REDLINE_GB is None:
        log(f"NOTE: {DELETE_DELAY_DAYS}-day delay with no Redline floor — a fast-filling library "
            f"could run out of space before marks age out. Redline deletes immediately when space "
            f"is critically low.")
    _tok = ('*' * 8 + PLEX_TOKEN[-4:]) if PLEX_TOKEN else 'NOT SET'
    log(f"Tautulli: {TAUTULLI_URL}")
    log(f"Plex:     {PLEX_URL} | token={_tok}")
    log(f"Last scheduled cleanup: {read_last_cleanup_date() or 'never'} | Cache: {CACHE_FILE}")
    if RADARR_OVERSEERR_SECTION_ID:
        log(f"Radarr:    {RADARR_URL}")
        _section_note = " (auto-detected)" if RADARR_OVERSEERR_SECTION_ID_SOURCE == "auto" else ""
        log(f"Radarr cleanup enabled for section_id={RADARR_OVERSEERR_SECTION_ID}{_section_note}")
    else:
        log("Radarr cleanup disabled (RADARR_OVERSEERR_SECTION_ID is None)")

    # Scoring / filtering / balance settings — surfaced here so a submitted log
    # carries the exact tunables that shaped the run's decisions.
    log("SCORING & ORDERING:")
    log(f"  Score balance: {SCORE_BALANCE}/100 "
        f"(watch+added history {HISTORY_WEIGHT * 100:.0f}% / IMDb quality {QUALITY_WEIGHT * 100:.0f}%)")
    log(f"  Max staleness (recency window): {MAX_STALENESS_MONTHS} months")
    log(f"  File size optimization: {f'{NEAR_TIE_PTS:g}-pt near-tie window' if NEAR_TIE_PTS else 'off'}")
    log(f"  Scoring constants: {json.dumps(SCORING, separators=(',', ':'))}")
    log("ELIGIBILITY FILTERS:")
    log(f"  Minimum age (grace period): {GRACE_PERIOD_DAYS} days")
    log(f"  Skip unplayed movies: {SKIP_UNPLAYED_MOVIES}")
    log(f"  Max IMDb rating cutoff: {MAX_IMDB_RATING if MAX_IMDB_RATING is not None else 'off'}")
    log(f"  Protect Jellyfin favorites: {PROTECT_JELLYFIN_FAVORITES}")
    log(f"  Protected Plex collections: {', '.join(sorted(PROTECTED_COLLECTIONS)) if PROTECTED_COLLECTIONS else 'none'}")
    log(f"  Protected Jellyfin collections: {', '.join(sorted(JELLYFIN_PROTECTED_COLLECTIONS)) if JELLYFIN_PROTECTED_COLLECTIONS else 'none'}")
    log(f"  Eligible extensions: {', '.join(sorted(MOVIE_EXTENSIONS))}")
    log_blank()


# The monitored roots are invariant for the lifetime of the process, but
# is_under_monitored_dir runs once per scanned movie and again per deletion
# candidate — memoize the resolve() syscalls instead of paying roots×movies.
_RESOLVED_MONITORED_ROOTS: tuple | None = None


def _resolved_monitored_roots() -> list:
    global _RESOLVED_MONITORED_ROOTS
    key = tuple(str(d) for d in MONITOR_DIRS)
    if _RESOLVED_MONITORED_ROOTS is None or _RESOLVED_MONITORED_ROOTS[0] != key:
        resolved = []
        for root in monitored_roots():
            try:
                resolved.append(root.resolve())
            except (OSError, RuntimeError):
                continue
        _RESOLVED_MONITORED_ROOTS = (key, resolved)
    return _RESOLVED_MONITORED_ROOTS[1]


def is_under_monitored_dir(path):
    """
    True if `path` (already resolved to a real /library file) falls within an
    allowed subtree. With no MONITOR_DIRS configured, nothing qualifies.
    """
    if path is None:
        return False
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    for root in _resolved_monitored_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def is_safe_to_delete(path):
    """
    Belt-and-suspenders deletion guard: the candidate must still resolve inside
    /library *and* inside a currently monitored subtree, and must not be a
    symlink (symlinked media is never deleted).
    """
    if path is None:
        return False
    try:
        if Path(path).is_symlink():
            return False
    except OSError:
        return False
    try:
        resolved = path.resolve()
        resolved.relative_to(LIBRARY_ROOT.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return is_under_monitored_dir(resolved)


# =========================
# METADATA CACHE
# =========================

# In-memory cache keyed by rating_key → {protected, tmdb_id, imdb_id}.
# Pre-populated from the JSON cache file at the start of build_candidates() so
# fetch_movie_metadata() returns instantly for already-known movies.
# A single Tautulli get_metadata call per new movie captures protection status,
# TMDB ID, and IMDB ID together so those lookups share the same API round-trip.
_metadata_cache: dict = {}


def fetch_movie_metadata(rating_key, title):
    """
    Fetch and cache Tautulli metadata for a movie.
    Returns dict: {protected: bool, tmdb_id: str|None, imdb_id: str|None}
    """
    if rating_key in _metadata_cache:
        return _metadata_cache[rating_key]

    time.sleep(0.05)  # 50ms per movie to avoid hammering Tautulli

    try:
        metadata = tautulli_api("get_metadata", rating_key=rating_key, media_info=0)
    except Exception as e:
        _abort_api_failure(
            f"Tautulli metadata query failed during run; aborting so API-dependent protection/scoring is not guessed. "
            f"title={title} | rating_key={rating_key} | error={e}",
            phase="scanning",
        )

    # Protection: collections is a plain list of strings e.g. ["Protected"]
    collections = metadata.get("collections") or []
    protected = any(
        (isinstance(e, str) and e in PROTECTED_COLLECTIONS) or
        (isinstance(e, dict) and e.get("tag") in PROTECTED_COLLECTIONS)
        for e in collections
    )

    # Extract IDs from guids list e.g. ["imdb://tt123", "tmdb://456"]
    tmdb_id = None
    imdb_id = None
    for guid in (metadata.get("guids") or []):
        if isinstance(guid, str):
            if guid.startswith("tmdb://"):
                tmdb_id = guid.replace("tmdb://", "").strip()
            elif guid.startswith("imdb://"):
                imdb_id = guid.replace("imdb://", "").strip()

    result = {"protected": protected, "tmdb_id": tmdb_id, "imdb_id": imdb_id, "v": 2}
    _metadata_cache[rating_key] = result
    return result


# =========================
# IMDB RATINGS
# =========================

# Download bounds for the ratings archive (~10 MB packed / ~25 MB unpacked in
# reality): hard ceilings so a wrong URL or crafted archive cannot balloon
# into memory. Same limits as the web app's downloader.
_IMDB_GZ_MAX_BYTES = 64 * 1024 * 1024
_IMDB_TSV_MAX_BYTES = 512 * 1024 * 1024


def _bounded_gunzip(gz_data: bytes) -> bytes:
    """Decompress with a hard output cap."""
    if len(gz_data) > _IMDB_GZ_MAX_BYTES:
        raise ValueError("IMDb ratings archive exceeds the size limit.")
    out = gzip.GzipFile(fileobj=io.BytesIO(gz_data)).read(_IMDB_TSV_MAX_BYTES + 1)
    if len(out) > _IMDB_TSV_MAX_BYTES:
        raise ValueError("IMDb ratings archive decompressed beyond the size limit.")
    return out


def _imdb_refresh_days() -> int:
    """The Advanced IMDb refresh interval, clamped to at least one day — the
    single freshness rule shared by runs and sample builds (the web app's
    _imdb_dataset_ready applies the same clamp)."""
    days = parse_int(IMDB_RATINGS_MAX_AGE_DAYS, 7)
    if days < 1:
        log(f"WARNING: IMDB_RATINGS_MAX_AGE_DAYS={IMDB_RATINGS_MAX_AGE_DAYS!r} is invalid; using 1 day.")
        return 1
    return days


def _write_imdb_tsv(tsv_data: bytes) -> None:
    """Atomic tmp+replace write — a mid-write kill must never leave a
    truncated TSV behind, because a short-but-parseable dataset silently
    scores the missing titles as unrated and skews deletion order."""
    tmp = IMDB_RATINGS_PATH.with_name(IMDB_RATINGS_PATH.name + ".tmp")
    tmp.write_bytes(tsv_data)
    tmp.replace(IMDB_RATINGS_PATH)


def _abort_imdb_ratings(message, *, error_code="imdb_ratings_unavailable"):
    """Log a fatal IMDb-ratings error, surface it to the dashboard, and stop.

    Simulation/live runs cannot score movies safely without this dataset, so the
    run stops. The structured progress error (error_code) lets the dashboard pop
    up manual setup steps instead of the user only seeing a dead run.
    """
    log(f"ABORT: {message}")
    log("       Manual fix: download title.ratings.tsv.gz from "
        "https://datasets.imdbws.com/ (IMDb Non-Commercial Datasets) and place "
        "it in the MediaReducer config folder — the next run unpacks it "
        "automatically.")
    # phase is intentionally left unchanged so the failed marker lands on the
    # step that was in progress. Suppressed automatically in quiet Summary mode.
    emit_progress(status="error", error_code=error_code,
                  message="IMDb ratings data is required but could not be obtained "
                          "— see the setup steps on the dashboard.")
    raise SystemExit(1)


def _extract_local_imdb_gz():
    """Fallback for a container that cannot reach the download URL.

    If the user manually placed the IMDb dataset next to the config as
    title.ratings.tsv.gz, decompress it to IMDB_RATINGS_PATH so a run can
    proceed. Returns True only when a usable .tsv was written.
    """
    gz_path = IMDB_RATINGS_PATH.with_name(IMDB_RATINGS_PATH.name + ".gz")
    if not gz_path.exists():
        return False
    try:
        with open(gz_path, "rb") as fh:
            tsv_data = _bounded_gunzip(fh.read(_IMDB_GZ_MAX_BYTES + 1))
        _write_imdb_tsv(tsv_data)
        return True
    except Exception as e:
        log(f"WARNING: found {gz_path} but could not decompress it: {e}")
        return False


_IMDB_RATINGS_MEMO: dict | None = None


def imdb_dataset_needed():
    """Whether a run needs the IMDb ratings dataset at all.

    IMDb only affects a run when it can change the outcome: the scoring dial
    gives IMDb some weight (QUALITY_WEIGHT > 0), or a Max IMDb rating cutoff is
    configured. At 100% watch history with no cutoff, IMDb has zero say — the
    score, the eligibility filters, and the deletion tiebreak all ignore it — so
    the dataset is neither checked nor downloaded, and movies are never excluded
    for lacking an IMDb rating.
    """
    return QUALITY_WEIGHT > 0 or MAX_IMDB_RATING is not None


def _load_imdb_ratings_from_disk() -> dict:
    """Parse the LOCAL IMDb ratings TSV into {tt_id: (rating, votes)} — a pure
    read, never a download. Raises on a missing/unreadable/misshapen file."""
    ratings: dict = {}
    with open(IMDB_RATINGS_PATH, "r", encoding="utf-8") as f:
        header = next(f, "").strip().split("	")
        if len(header) < 3 or header[:3] != ["tconst", "averageRating", "numVotes"]:
            raise ValueError("unexpected TSV header")
        for line in f:
            parts = line.split("	")
            if len(parts) >= 3:
                try:
                    ratings[parts[0]] = (float(parts[1]), int(parts[2].strip()))
                except ValueError:
                    pass
    return ratings


def ensure_imdb_ratings():
    """Ensure the IMDb ratings dataset is present and fresh, then load and return
    it as {tt_id: (rating, votes)} (~1.4M titles). Memoized per process.

    Simulation/live only — debug_info Summary neither downloads nor loads it.
    Downloads IMDB_RATINGS_URL when the local copy is missing or older than
    IMDB_RATINGS_MAX_AGE_DAYS. Scoring cannot be safe without this data, so
    missing/unreadable/empty ratings ABORT the run rather than silently treating
    every movie as unrated. main() resolves it as one of the first checks (a
    broken download fails in seconds, not after the whole library fetch).
    """

    global _IMDB_RATINGS_MEMO
    if _IMDB_RATINGS_MEMO is not None:
        return _IMDB_RATINGS_MEMO

    if RUN_MODE == "debug_info":
        log("IMDB ratings skipped: summary/debug_info mode does not use movie scoring data.")
        return {}

    refresh_days = _imdb_refresh_days()

    IMDB_RATINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Decide whether to (re)download. This honours the Advanced → IMDb refresh
    # setting: if the local TSV is younger than refresh_days, it is reused.
    needs_download = True
    if IMDB_RATINGS_PATH.exists():
        age_days = (time.time() - IMDB_RATINGS_PATH.stat().st_mtime) / 86400
        if age_days < refresh_days:
            log(f"IMDB ratings file is {age_days:.1f} days old (limit={refresh_days}d), reusing.")
            needs_download = False
        else:
            log(f"IMDB ratings file is {age_days:.1f} days old (limit={refresh_days}d), re-downloading.")

    if needs_download:
        log(f"Downloading IMDB ratings dataset from {IMDB_RATINGS_URL} ...")
        try:
            with urllib.request.urlopen(IMDB_RATINGS_URL, timeout=120) as resp:
                gz_data = resp.read(_IMDB_GZ_MAX_BYTES + 1)
            log(f"Download complete ({len(gz_data) / 1_000_000:.1f} MB). Extracting...")
            tsv_data = _bounded_gunzip(gz_data)
            _write_imdb_tsv(tsv_data)
            log(f"IMDB ratings saved to {IMDB_RATINGS_PATH} ({len(tsv_data) / 1_000_000:.1f} MB).")
        except Exception as e:
            log(f"ERROR downloading IMDB ratings: {e}")
            # Before giving up, fall back to a manually-provided dataset: a user
            # whose container cannot reach the URL can drop title.ratings.tsv.gz
            # (or the already-extracted title.ratings.tsv) into the config folder.
            if _extract_local_imdb_gz():
                log("Using manually-provided title.ratings.tsv.gz from the config folder.")
            elif IMDB_RATINGS_PATH.exists():
                # The copy on disk is past the configured refresh interval and
                # the refresh failed. Deleting on stale ratings is unsafe, so
                # this stops the run instead of quietly scoring old data.
                age_days = (time.time() - IMDB_RATINGS_PATH.stat().st_mtime) / 86400
                _abort_imdb_ratings(
                    f"IMDb ratings dataset at {IMDB_RATINGS_PATH} is {age_days:.1f} days old "
                    f"(refresh limit {refresh_days}d) and the refresh download failed. Fix the "
                    "download or place a fresh title.ratings.tsv.gz in the config folder, then "
                    "run again."
                )

    if not IMDB_RATINGS_PATH.exists():
        _abort_imdb_ratings(
            f"IMDb ratings dataset is missing at {IMDB_RATINGS_PATH} and the automatic "
            f"download from {IMDB_RATINGS_URL} failed. Download title.ratings.tsv.gz from IMDb "
            "and place it in the config folder (it is unpacked automatically), then run again."
        )

    # Load TSV: tconst 	 averageRating 	 numVotes
    log("Loading IMDB ratings into memory...")
    try:
        ratings = _load_imdb_ratings_from_disk()
    except Exception as e:
        log(f"ERROR loading IMDB ratings file: {e}")
        _abort_imdb_ratings(
            f"IMDb ratings file at {IMDB_RATINGS_PATH} could not be loaded. "
            "Simulation/live cleanup requires IMDb ratings data."
        )

    if not ratings:
        _abort_imdb_ratings(
            f"IMDb ratings file at {IMDB_RATINGS_PATH} loaded zero usable rows. "
            "Simulation/live cleanup requires IMDb ratings data."
        )

    log(f"Loaded {len(ratings):,} IMDB ratings.")
    _IMDB_RATINGS_MEMO = ratings
    return ratings



# =========================
# PLEX / RADARR
# =========================

def plex_request(path, timeout=15):
    """Make a JSON request to the Plex API. Returns (status, dict) or (None, None) on error."""
    if not PLEX_URL or not PLEX_TOKEN:
        return None, None
    sep = "&" if "?" in path else "?"
    url = f"{PLEX_URL.rstrip('/')}{path}{sep}X-Plex-Token={PLEX_TOKEN}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"WARN Plex API error | path={path} | error={e}")
        return None, None


def _plex_movie_section_ids_direct():
    """Movie library section keys from Plex itself (the same source the Config
    collection scan uses). Tautulli's section IDs can differ from Plex's, which
    would make a collection lookup silently return nothing — so protection asks
    Plex directly. Returns a list, or None if Plex is unreachable."""
    status, data = plex_request("/library/sections")
    if not status or status != 200 or not data:
        return None
    dirs = _as_list((data.get("MediaContainer") or {}).get("Directory"))
    movie_keys = [d.get("key") for d in dirs if d.get("type") == "movie" and d.get("key")]
    return movie_keys or [d.get("key") for d in dirs if d.get("key")]


def _plex_item_paths(item):
    """Resolved /library path strings for a Plex metadata item's file(s)."""
    return _item_resolved_paths(item)


def fetch_protected_paths():
    """Return (paths, rating_keys, imdb_ids, tmdb_ids) for every movie in any
    PROTECTED_COLLECTIONS collection on Plex.

    Protection is matched primarily by resolved /library FILE PATH — the exact
    thing deletion operates on — which is robust to Tautulli-vs-Plex rating_key
    or section-id differences. rating_keys are kept as a secondary match.

    Returns four empty sets when nothing is protected. If Plex cannot answer
    while protected collections are configured, the run aborts instead of
    using stale protection data.
    """
    if not PROTECTED_COLLECTIONS:
        return set(), set(), set(), set()
    if not PLEX_URL or not PLEX_TOKEN:
        log("Plex protection: PLEX_URL/PLEX_TOKEN not set; aborting because protected collections cannot be verified.")
        _abort_api_failure("Plex protected collections are configured, but Plex URL/token are not available.", phase="scanning")

    section_ids = _plex_movie_section_ids_direct()
    if section_ids is None:
        log("WARN Plex protection: could not reach Plex to list sections; aborting because protected collections cannot be verified.")
        _abort_api_failure("Plex protection query failed: could not reach Plex to list sections.", phase="scanning")

    wanted = _protected_name_set(PROTECTED_COLLECTIONS)
    paths, keys, imdb_ids, tmdb_ids, matched = set(), set(), set(), set(), set()
    for section_id in section_ids:
        status, data = plex_request(f"/library/sections/{section_id}/collections")
        if not status or status != 200 or not data:
            _abort_api_failure(f"Plex protection query failed for section {section_id}: status={status}", phase="scanning")
        container = data.get("MediaContainer") or {}
        collections = container.get("Directory") or container.get("Metadata") or []
        collections = _as_list(collections)
        for coll in collections:
            if str(coll.get("title") or "").strip().lower() not in wanted:
                continue
            coll_key = coll.get("ratingKey")
            if not coll_key:
                continue
            matched.add(str(coll.get("title")))
            status2, data2 = plex_request(f"/library/collections/{coll_key}/children")
            if not status2 or status2 != 200 or not data2:
                _abort_api_failure(
                    f"Plex protection query failed while reading collection '{coll.get('title')}' children: status={status2}",
                    phase="scanning",
                )
            members = _as_list((data2.get("MediaContainer") or {}).get("Metadata"))
            for item in members:
                rk = item.get("ratingKey")
                if rk:
                    keys.add(str(rk))
                item_paths = _plex_item_paths(item)
                item_imdbs, item_tmdbs = _item_provider_ids(item)
                imdb_ids |= item_imdbs
                tmdb_ids |= item_tmdbs
                if not item_paths and rk:
                    # Some Plex versions omit Media/Part in the collection listing;
                    # fetch the item directly to get its file path.
                    s3, d3 = plex_request(f"/library/metadata/{rk}")
                    if s3 != 200 or not d3:
                        _abort_api_failure(f"Plex protection metadata query failed for ratingKey={rk}: status={s3}", phase="scanning")
                    md = _as_list((d3.get("MediaContainer") or {}).get("Metadata"))
                    for m in md:
                        item_paths |= _plex_item_paths(m)
                        item_imdbs, item_tmdbs = _item_provider_ids(m)
                        imdb_ids |= item_imdbs
                        tmdb_ids |= item_tmdbs
                paths |= item_paths

    if matched:
        id_bits = []
        if imdb_ids: id_bits.append(f"{len(imdb_ids)} imdb id(s)")
        if tmdb_ids: id_bits.append(f"{len(tmdb_ids)} tmdb id(s)")
        suffix = ", " + ", ".join(id_bits) if id_bits else ""
        log(f"Plex protection: collection(s) {sorted(matched)} -> {len(keys)} movie(s), {len(paths)} resolved path(s){suffix}.")
    # Fail closed on a missing protection, not just a failed query: a renamed
    # or deleted collection would otherwise let the run proceed with the very
    # movies the user marked keep-forever back on the deletion table. Partial
    # misses count too — one renamed collection out of two is still a lapsed
    # protection. Non-deleting modes (the sample build) keep the soft warning.
    _matched_norm = {str(m).strip().lower() for m in matched}
    _missing = sorted({str(n).strip() for n in PROTECTED_COLLECTIONS
                       if str(n).strip().lower() in wanted
                       and str(n).strip().lower() not in _matched_norm})
    if _missing:
        _msg = (f"Plex protected collection(s) {_missing} not found — renamed or deleted? "
                f"Aborted rather than running unprotected.")
        if RUN_MODE in ("debug_sim", "headroom"):
            _abort_api_failure(
                _msg + " Fix or uncheck them under Configuration → Protected collections.",
                phase="scanning")
        log(f"WARN {_msg}")
    return paths, keys, imdb_ids, tmdb_ids


def radarr_lookup_movie(tmdb_id, title):
    """Find a Radarr movie by TMDB ID, aborting on API failures."""
    headers = {"X-Api-Key": RADARR_API_KEY}
    try:
        status, data = http_request(
            "GET",
            f"{RADARR_URL.rstrip('/')}/api/v3/movie?tmdbId={tmdb_id}",
            headers=headers,
        )
    except Exception as e:
        _abort_api_failure(f"Radarr lookup failed during post-delete cleanup | title={title} | tmdb_id={tmdb_id} | error={e}", phase="deleting")

    if status != 200:
        _abort_api_failure(f"Radarr lookup failed during post-delete cleanup | title={title} | tmdb_id={tmdb_id} | status={status}", phase="deleting")
    if not isinstance(data, list) or len(data) == 0:
        log(f"Radarr: movie not found, nothing to clean up | title={title} | tmdb_id={tmdb_id} | status={status}")
        return None
    return data[0]


def radarr_delete(tmdb_id, title):
    """
    Find the movie in Radarr by TMDB ID and delete it without deleting files
    (the file was already removed by this script) and without adding an import
    exclusion (so it can be re-requested and re-grabbed normally).
    Returns True if deleted or not found, False on error.
    """
    headers = {"X-Api-Key": RADARR_API_KEY}

    try:
        status, data = http_request(
            "GET",
            f"{RADARR_URL.rstrip('/')}/api/v3/movie?tmdbId={tmdb_id}",
            headers=headers,
        )
    except Exception as e:
        _abort_api_failure(f"Radarr lookup failed during post-delete cleanup | title={title} | tmdb_id={tmdb_id} | error={e}", phase="deleting")

    if status != 200:
        _abort_api_failure(f"Radarr lookup failed during post-delete cleanup | title={title} | tmdb_id={tmdb_id} | status={status}", phase="deleting")
    if not isinstance(data, list) or len(data) == 0:
        log(f"Radarr: movie not found, nothing to clean up | title={title} | tmdb_id={tmdb_id} | status={status}")
        return True  # not an error — just not in Radarr (e.g. manually added movie)

    radarr_id = data[0].get("id")
    if not radarr_id:
        log(f"Radarr: could not extract movie id | title={title} | tmdb_id={tmdb_id}")
        return False

    try:
        del_status, _ = http_request(
            "DELETE",
            f"{RADARR_URL.rstrip('/')}/api/v3/movie/{radarr_id}?deleteFiles=false&addImportExclusion=false",
            headers=headers,
        )
    except Exception as e:
        _abort_api_failure(f"Radarr delete failed during post-delete cleanup | title={title} | tmdb_id={tmdb_id} | error={e}", phase="deleting")

    if del_status in (200, 204):
        log(f"Radarr: deleted | title={title} | tmdb_id={tmdb_id} | radarr_id={radarr_id}")
        return True

    _abort_api_failure(f"Radarr delete failed during post-delete cleanup | title={title} | tmdb_id={tmdb_id} | status={del_status}", phase="deleting")


def cleanup_radarr(candidate, section1_paths_by_tmdb):
    """
    After a file has been physically deleted, decide whether to remove the movie
    from Radarr. (Overseerr needs no direct call — once Radarr drops the movie,
    Overseerr detects the removal and resets its own request status automatically.)

    Rules:
    - Only runs when RADARR_OVERSEERR_SECTION_ID is set.
    - Only runs when the deleted file was in that section.
    - Checks whether any other file for the same TMDB ID still exists on disk
      within that section (including protected movies and movies not selected
      for deletion). If a surviving copy exists, skips cleanup so Radarr stays
      aware of the movie.
    - Only when this was the last copy does it remove the movie from Radarr.
    """
    if not RADARR_OVERSEERR_SECTION_ID:
        return

    tmdb_id = candidate.get("tmdb_id")
    title = candidate["title"]

    if not tmdb_id:
        log(f"Radarr: skipping cleanup, no TMDB ID available | title={title}")
        return

    radarr_movie = None
    section_matches = str(candidate.get("section_id")) == str(RADARR_OVERSEERR_SECTION_ID)
    if not section_matches:
        if not (RADARR_URL and RADARR_API_KEY):
            _abort_api_failure(f"Radarr cleanup is enabled, but Radarr URL/API key are not available | title={title} | tmdb_id={tmdb_id}", phase="deleting")
        radarr_movie = radarr_lookup_movie(tmdb_id, title)
        if not radarr_movie:
            return
        if not _radarr_movie_matches_deleted_path(radarr_movie, candidate["path"]):
            log(
                f"Radarr: skipping cleanup, candidate is not in configured section "
                f"{RADARR_OVERSEERR_SECTION_ID} and Radarr path did not match deleted folder | "
                f"title={title} | tmdb_id={tmdb_id} | section_id={candidate.get('section_id')} | "
                f"path={candidate['path']} | radarr_path={radarr_movie.get('path') or radarr_movie.get('folderName')}"
            )
            return
        log(
            f"Radarr: candidate section_id={candidate.get('section_id')} did not match configured "
            f"section {RADARR_OVERSEERR_SECTION_ID}, but Radarr owns the deleted folder; continuing cleanup | "
            f"title={title} | tmdb_id={tmdb_id}"
        )

    deleted_path = candidate["path"].resolve()
    all_section1_paths = section1_paths_by_tmdb.get(tmdb_id, set())
    other_paths = all_section1_paths - {deleted_path}
    surviving = [p for p in other_paths if p.exists()]

    # Belt-and-suspenders for multi-file movies: a single Plex item can hold
    # several physical files ("Versions"), which the scan registers as just one
    # path — so section1_paths_by_tmdb may not know about a sibling copy. If the
    # deleted file's own folder still holds another playable file, treat it as a
    # surviving copy and keep Radarr aware rather than risk forgetting a movie
    # that still exists on disk. Only ever makes cleanup MORE conservative.
    if not surviving:
        try:
            for sib in deleted_path.parent.iterdir():
                if (sib != deleted_path and sib.suffix.lower() in MOVIE_EXTENSIONS
                        and sib.is_file()):
                    surviving.append(sib)
                    break
        except OSError:
            pass

    if surviving:
        log(
            f"Radarr: skipping cleanup, {len(surviving)} other copy/copies "
            f"still on disk in section {RADARR_OVERSEERR_SECTION_ID} | "
            f"title={title} | surviving={[str(p) for p in surviving]}"
        )
        return

    log(
        f"Radarr: last copy in section {RADARR_OVERSEERR_SECTION_ID} deleted, "
        f"cleaning up | title={title} | tmdb_id={tmdb_id}"
    )

    if RADARR_URL and RADARR_API_KEY:
        radarr_delete(tmdb_id, title)
    else:
        _abort_api_failure(f"Radarr cleanup is enabled, but Radarr URL/API key are not available | title={title} | tmdb_id={tmdb_id}", phase="deleting")


# =========================
# FILE PATH EXTRACTION
# =========================

def extract_file_path(item, quiet=False):
    """
    Resolve the on-disk file path for a Tautulli media row.
    Tries common field names directly on the row, then on nested media_info,
    then falls back to a live get_metadata API call as a last resort.
    Returns a translated (Unraid) Path or None if no path can be found.

    quiet=True suppresses the "metadata_no_file_path" log line — used by the
    pre-scan path resolver, which resolves paths only to merge cross-source
    duplicates and leaves the real skip logging to the scan.
    """
    possible_keys = ["file", "file_path", "media_file", "location", "path"]

    for key in possible_keys:
        value = item.get(key)
        if value:
            return resolve_under_library(value)

    media_info = item.get("media_info")
    if isinstance(media_info, list):
        for media in media_info:
            for key in possible_keys:
                value = media.get(key)
                if value:
                    return resolve_under_library(value)

    rating_key = item.get("rating_key")
    if not rating_key:
        return None

    try:
        metadata = tautulli_api("get_metadata", rating_key=rating_key, media_info=1)
    except Exception as e:
        _abort_api_failure(
            f"Tautulli file-path metadata query failed during run; aborting instead of skipping a movie with incomplete API data. "
            f"title={item.get('title')} | rating_key={rating_key} | error={e}",
            phase="scanning",
        )

    for key in possible_keys:
        value = metadata.get(key)
        if value:
            return resolve_under_library(value)

    metadata_media_info = metadata.get("media_info")
    if isinstance(metadata_media_info, list):
        for media in metadata_media_info:
            for key in possible_keys:
                value = media.get(key)
                if value:
                    return resolve_under_library(value)
            parts = media.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    for key in possible_keys:
                        value = part.get(key)
                        if value:
                            return resolve_under_library(value)

    if not quiet:
        log(
            f"SKIP metadata_no_file_path | "
            f"title={item.get('title')} | "
            f"rating_key={rating_key} | "
            f"metadata_keys={list(metadata.keys())}"
        )
    return None


# =========================
# TAUTULLI LIBRARY FETCH
# =========================

def get_movie_section_ids():
    """Auto-discover all active movie library section IDs from Tautulli."""
    try:
        libraries = tautulli_api("get_libraries")
    except Exception as e:
        raise RuntimeError(f"Failed to fetch libraries from Tautulli: {e}")

    sections = [
        lib for lib in libraries
        if lib.get("section_type") == "movie" and lib.get("is_active", 1)
    ]

    log("Auto-discovered movie sections: " +
        ", ".join(f"{s['section_name']} (id={s['section_id']})" for s in sections))

    return [s["section_id"] for s in sections]


def get_all_movies_from_tautulli():
    """Fetch every movie from all active movie sections, tagged with _section_id.

    Rows sharing a file path across sections are merged: highest play_count, most
    recent last_played, and RADARR_OVERSEERR_SECTION_ID preserved if either copy
    was in it.
    """
    section_ids = get_movie_section_ids()
    raw_rows = []

    for section_id in section_ids:
        start = 0
        length = 1000

        while True:
            data = tautulli_api(
                "get_library_media_info",
                section_id=section_id,
                section_type="movie",
                start=start,
                length=length,
                order_column="title",
                order_dir="asc",
            )

            rows = data if isinstance(data, list) else (((data or {}).get("data")) or [])

            log(f"Tautulli page: section={section_id} | start={start} | rows={len(rows)}")

            if not rows:
                break

            for row in rows:
                row["_section_id"] = section_id

            raw_rows.extend(rows)

            if len(rows) < length:
                break

            start += length

    log(f"Tautulli returned {len(raw_rows)} total movie rows across all sections.")

    # Deduplicate by raw file key, merging play stats
    seen: dict = {}

    for row in raw_rows:
        file_key = (
            row.get("file") or row.get("file_path") or
            row.get("media_file") or row.get("location") or
            row.get("path") or row.get("rating_key")
        )

        if file_key not in seen:
            seen[file_key] = dict(row)
        else:
            existing = seen[file_key]

            new_plays = parse_int(row.get("play_count"), 0)
            cur_plays = parse_int(existing.get("play_count"), 0)
            if new_plays > cur_plays:
                existing["play_count"] = row["play_count"]

            new_lp = parse_int(row.get("last_played"), 0)
            cur_lp = parse_int(existing.get("last_played"), 0)
            if new_lp > cur_lp:
                existing["last_played"] = row["last_played"]

            # Preserve the Radarr section_id if either copy is in it
            if str(row.get("_section_id")) == str(RADARR_OVERSEERR_SECTION_ID):
                existing["_section_id"] = RADARR_OVERSEERR_SECTION_ID

    merged = list(seen.values())
    log(f"After deduplication: {len(merged)} unique movie entries.")
    return merged


# =========================
# JELLYFIN (native API) — read-only client
# =========================
# Fetch + normalize Jellyfin data into the same row shape as the Tautulli
# path; get_all_movies() merges the two. Play history is per-user in
# Jellyfin, so it is aggregated across every user (play_count summed,
# last_played = most recent LastPlayedDate). Protection comes from Jellyfin
# BoxSets named in JELLYFIN_PROTECTED_COLLECTIONS.

_JELLYFIN_PROTECTED_MATCH_KEYS = set()
_JELLYFIN_PROTECTED_IMDB_IDS = set()
_JELLYFIN_PROTECTED_TMDB_IDS = set()
# Resolved /library match key -> {"imdb","tmdb","title"} for EVERY Jellyfin movie.
# Lets the scan look up what Jellyfin thinks a given on-disk file is, so a Plex
# row can be identity-checked against Jellyfin by path (Tautulli movie rows carry
# no file path at merge time, so path merging must happen per-row in the scan).
_JELLYFIN_IDS_BY_MATCH_KEY = {}


def _jellyfin_request(path, params=None, timeout=30):
    """GET {JELLYFIN_URL}/{path}, authenticated with the API key. Returns JSON."""
    base = JELLYFIN_URL.rstrip("/")
    if not base:
        raise RuntimeError("JELLYFIN_URL is not configured.")
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{base}/{path.lstrip('/')}{query}"
    req = urllib.request.Request(url, headers={
        "Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"',
        "X-Emby-Token": JELLYFIN_API_KEY,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise RuntimeError(f"Jellyfin returned non-JSON for {path} "
                           f"(proxy error page or wrong URL?): {raw[:120]!r}")


def _jellyfin_date_to_epoch(value):
    """Parse a Jellyfin ISO-8601 UTC datetime to a unix epoch int (0 if blank/bad)."""
    if not value:
        return 0
    try:
        core = str(value)[:19]  # YYYY-MM-DDTHH:MM:SS — ignore fractional seconds + Z
        return int(calendar.timegm(time.strptime(core, "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return 0


def _jellyfin_user_ids():
    """All Jellyfin user IDs (play history is per-user and must be aggregated)."""
    users = _jellyfin_request("Users") or []
    return [u.get("Id") for u in users if isinstance(u, dict) and u.get("Id")]


def _jellyfin_item_ids_from_payload(item):
    """IDs that may identify the real movie in Jellyfin collection payloads."""
    ids = set()
    for key in ("Id", "ItemId", "MovieId"):
        value = item.get(key)
        if value:
            ids.add(str(value))
    return ids


def _jellyfin_boxset_children(box_id, user_id):
    """Return movie item IDs and resolved file paths inside a Jellyfin BoxSet.

    BoxSet membership enumeration is inconsistent across Jellyfin versions: the
    admin `/Items?ParentId=` endpoint returns nothing for BoxSets on many builds,
    while the user-scoped `/Users/{uid}/Items?ParentId=` endpoint is reliable. We
    try several forms and UNION whatever each returns — for deletion safety it is
    better to over-protect than to silently miss a member. Some endpoints return
    wrapper/list entries instead of the base movie ID, so resolved paths are
    returned too and matched against the same /library path used for deletion.
    """
    ids = set()
    paths = set()
    imdb_ids = set()
    tmdb_ids = set()
    attempts = []
    fields = "Path,MediaSources,ProviderIds"
    if user_id:
        attempts.append((f"Collections/{box_id}/Items",
                         {"UserId": user_id, "IncludeItemTypes": "Movie", "Fields": fields}))
        attempts.append((f"Users/{user_id}/Items",
                         {"ParentId": box_id, "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": fields}))
        attempts.append((f"Users/{user_id}/Items", {"ParentId": box_id, "Fields": fields}))
    attempts.append((f"Collections/{box_id}/Items",
                     {"IncludeItemTypes": "Movie", "Fields": fields}))
    attempts.append(("Items",
                     {"ParentId": box_id, "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": fields}))
    attempts.append(("Items", {"ParentId": box_id, "Fields": fields}))  # direct-children form some Jellyfin builds require
    successful_queries = 0
    query_errors = []
    _noted_405 = set()
    for path, params in attempts:
        try:
            children = (_jellyfin_request(path, params) or {}).get("Items", [])
        except Exception as e:
            msg = str(e)
            if "405" in msg or "Method Not Allowed" in msg:
                # Collections/{id}/Items is a write-only (POST add / DELETE remove)
                # endpoint on modern Jellyfin, so a GET returns 405. Expected and
                # harmless — the ParentId forms below enumerate the same members.
                # Note it quietly, once per endpoint, so an empty run doesn't look
                # like a failure.
                if path not in _noted_405:
                    log(f"    Jellyfin protection: {path} is not GET-able on this server (405) — using ParentId enumeration.")
                    _noted_405.add(path)
            else:
                log(f"    Jellyfin protection: children query {path} {params} failed: {e}")
            query_errors.append(f"{path} {params}: {e}")
            continue
        successful_queries += 1
        found = set()
        found_paths = set()
        found_imdbs = set()
        found_tmdbs = set()
        for child in children:
            if not isinstance(child, dict):
                continue
            found.update(_jellyfin_item_ids_from_payload(child))
            child_imdbs, child_tmdbs = _item_provider_ids(child)
            found_imdbs.update(child_imdbs)
            found_tmdbs.update(child_tmdbs)
            found_paths |= _item_resolved_paths(child)
        log(f"    Jellyfin protection: {path} box={box_id} -> {len(found)} id(s), {len(found_paths)} path(s), {len(found_imdbs)} imdb id(s), {len(found_tmdbs)} tmdb id(s).")
        ids.update(found)
        paths.update(found_paths)
        imdb_ids.update(found_imdbs)
        tmdb_ids.update(found_tmdbs)
    if successful_queries == 0:
        _abort_api_failure(
            f"Jellyfin protection query failed for BoxSet {box_id}; no child endpoint answered. "
            f"Errors: {'; '.join(query_errors[:3])}",
            phase="scanning",
        )
    return ids, paths, imdb_ids, tmdb_ids


def _jellyfin_protected_items():
    """Movie IDs, paths and provider IDs in any protected Jellyfin collection."""
    wanted = _protected_name_set(JELLYFIN_PROTECTED_COLLECTIONS)
    if not wanted:
        return set(), set(), set(), set()

    # A user id makes both BoxSet listing and child enumeration reliable across
    # Jellyfin versions (admin-scope /Items is flaky for BoxSets).
    try:
        users = list(_jellyfin_user_ids())
    except Exception as e:
        _abort_api_failure(f"Jellyfin protection query failed while listing users: {e}", phase="scanning")
    user_id = users[0] if users else None
    log(f"Jellyfin protection: wanted={sorted(wanted)} | users={len(users)} | using user_id={user_id or 'none'}")

    # List collections — user-scoped first (reliable), admin form as fallback.
    boxsets, used_path = [], None
    listing_errors = []
    listing_attempts = 0
    for path in ([f"Users/{user_id}/Items"] if user_id else []) + ["Items"]:
        try:
            listing_attempts += 1
            boxsets = (_jellyfin_request(path, {
                "IncludeItemTypes": "BoxSet",
                "Recursive": "true",
            }) or {}).get("Items", [])
        except Exception as e:
            log(f"WARN Jellyfin protection: BoxSet listing via {path} failed: {e}")
            listing_errors.append(f"{path}: {e}")
            boxsets = []
        if boxsets:
            used_path = path
            break
    if not boxsets and listing_attempts > 0 and len(listing_errors) >= listing_attempts:
        _abort_api_failure(
            f"Jellyfin protection query failed while listing BoxSets. Errors: {'; '.join(listing_errors[:3])}",
            phase="scanning",
        )
    log(f"Jellyfin protection: found {len(boxsets)} collection(s) via {used_path or 'n/a'}: "
        f"{sorted(str(b.get('Name')) for b in boxsets)[:20]}")

    protected_ids = set()
    protected_paths = set()
    protected_imdb_ids = set()
    protected_tmdb_ids = set()
    matched = set()
    for bs in boxsets:
        if str(bs.get("Name", "")).strip().lower() in wanted and bs.get("Id"):
            log(f"Jellyfin protection: matched collection '{bs.get('Name')}' (id={bs.get('Id')}) — enumerating members:")
            child_ids, child_paths, child_imdbs, child_tmdbs = _jellyfin_boxset_children(bs["Id"], user_id)
            matched.add(bs.get("Name"))
            protected_ids |= child_ids
            protected_paths |= child_paths
            protected_imdb_ids |= child_imdbs
            protected_tmdb_ids |= child_tmdbs
            log(f"Jellyfin protection: collection '{bs.get('Name')}' -> {len(child_ids)} protected id(s), {len(child_paths)} protected path(s), {len(child_imdbs)} imdb id(s), {len(child_tmdbs)} tmdb id(s).")
    if matched:
        log(f"Jellyfin protection: {len(protected_ids)} protected id(s), {len(protected_paths)} protected path(s), {len(protected_imdb_ids)} imdb id(s), {len(protected_tmdb_ids)} tmdb id(s) across {sorted(matched)}.")
    # Fail closed on a missing protection (same rule as the Plex side): a
    # renamed/deleted BoxSet or one hidden from the API key's user must stop
    # a deleting run, not silently strip the protection. Sample builds warn.
    _matched_norm = {str(m).strip().lower() for m in matched}
    _missing = sorted({str(n).strip() for n in JELLYFIN_PROTECTED_COLLECTIONS
                       if str(n).strip().lower() in wanted
                       and str(n).strip().lower() not in _matched_norm})
    if _missing:
        _msg = (f"Jellyfin protected collection(s) {_missing} not found — renamed, deleted, or not "
                f"visible to the API key's user? Aborted rather than running unprotected.")
        if RUN_MODE in ("debug_sim", "headroom"):
            _abort_api_failure(
                _msg + " Fix or uncheck them under Configuration → Protected collections.",
                phase="scanning")
        log(f"WARN {_msg}")
    return protected_ids, protected_paths, protected_imdb_ids, protected_tmdb_ids


def get_all_movies_from_jellyfin():
    """Return all Jellyfin movies as normalized rows matching the Tautulli shape.

    Each row: rating_key, title, year, file, file_size, added_at, last_played,
    play_count, tmdb_id, imdb_id, protected, _section_id, _source='jellyfin'.
    Play data is aggregated across every Jellyfin user (summed plays, most recent
    last-played).
    """
    global _JELLYFIN_PROTECTED_MATCH_KEYS, _JELLYFIN_PROTECTED_IMDB_IDS, _JELLYFIN_PROTECTED_TMDB_IDS
    global _JELLYFIN_IDS_BY_MATCH_KEY
    _JELLYFIN_PROTECTED_MATCH_KEYS = set()
    _JELLYFIN_PROTECTED_IMDB_IDS = set()
    _JELLYFIN_PROTECTED_TMDB_IDS = set()
    _JELLYFIN_IDS_BY_MATCH_KEY = {}

    base = (_jellyfin_request("Items", {
        "IncludeItemTypes": "Movie",
        "Recursive": "true",
        "Fields": "Path,MediaSources,DateCreated,ProviderIds,CommunityRating,RunTimeTicks",
        "EnableUserData": "false",
    }) or {}).get("Items", [])

    rows = {}
    for item in base:
        item_id = item.get("Id")
        if not item_id:
            continue
        path = item.get("Path") or ""
        size = 0
        bitrate_kbps = 0
        resolution = None
        for ms in (item.get("MediaSources") or []):
            if not isinstance(ms, dict):
                continue
            if not path:
                path = ms.get("Path") or ""
            if not size and ms.get("Size"):
                size = ms["Size"]
            # Jellyfin Bitrate is bits/sec; normalize to kbps to match Tautulli rows.
            if not bitrate_kbps and ms.get("Bitrate"):
                bitrate_kbps = parse_int(ms.get("Bitrate"), 0) // 1000
            for stream in (ms.get("MediaStreams") or []):
                if not isinstance(stream, dict):
                    continue
                stype = str(stream.get("Type") or "").lower()
                if stype == "video" and resolution is None:
                    if stream.get("Height"):
                        resolution = str(stream.get("Height"))
        prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
        rows[item_id] = {
            "rating_key":  f"jf:{item_id}",
            "title":       item.get("Name") or "",
            "year":        item.get("ProductionYear") or "",
            "file":        path,
            "file_size":   size,
            "added_at":    _jellyfin_date_to_epoch(item.get("DateCreated")),
            "last_played": 0,
            "play_count":  0,
            "tmdb_id":     (str(prov["tmdb"]).strip() if prov.get("tmdb") else None),
            "imdb_id":     (str(prov["imdb"]).strip() if prov.get("imdb") else None),
            "protected":   False,
            "_section_id": None,
            "_source":     "jellyfin",
            # Retention-score inputs (names match Tautulli row fields).
            "video_resolution": resolution,
            "bitrate":          bitrate_kbps,
            "_jf_users":        0,
            "_jf_favorite":     False,
        }

    log(f"Jellyfin returned {len(rows)} movies.")

    # Aggregate per-user play data (Jellyfin has no cross-user total).
    for uid in _jellyfin_user_ids():
        try:
            items = (_jellyfin_request(f"Users/{uid}/Items", {
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "EnableUserData": "true",
            }) or {}).get("Items", [])
        except Exception as e:
            _abort_api_failure(f"Jellyfin play-history query failed for user {uid}: {e}", phase="scanning")
        for item in items:
            row = rows.get(item.get("Id"))
            if not row:
                continue
            ud = item.get("UserData") or {}
            user_plays = parse_int(ud.get("PlayCount"), 0)
            row["play_count"] = parse_int(row["play_count"], 0) + user_plays
            if user_plays > 0 or ud.get("Played"):
                row["_jf_users"] = parse_int(row.get("_jf_users"), 0) + 1
            if ud.get("IsFavorite"):
                row["_jf_favorite"] = True
            lp = _jellyfin_date_to_epoch(ud.get("LastPlayedDate"))
            if lp > parse_int(row["last_played"], 0):
                row["last_played"] = lp

    # Protection via named BoxSets. Match by item ID and by the resolved
    # /library path because Jellyfin collection endpoints vary by version.
    protected_ids, protected_paths, protected_imdb_ids, protected_tmdb_ids = _jellyfin_protected_items()
    _JELLYFIN_PROTECTED_IMDB_IDS = set(protected_imdb_ids)
    _JELLYFIN_PROTECTED_TMDB_IDS = set(protected_tmdb_ids)
    for path in protected_paths:
        _JELLYFIN_PROTECTED_MATCH_KEYS.update(_match_keys(path))
    for item_id, row in rows.items():
        # Record what Jellyfin thinks this on-disk file is, keyed by every
        # resolvable /library match key, so the scan can identity-check a Plex
        # row against Jellyfin by path (Tautulli rows have no path at merge time).
        if row.get("imdb_id") or row.get("tmdb_id"):
            for k in _match_keys(row.get("file")):
                _JELLYFIN_IDS_BY_MATCH_KEY.setdefault(k, {
                    "imdb":  row.get("imdb_id"),
                    "tmdb":  row.get("tmdb_id"),
                    "title": row.get("title"),
                })
        row_imdb = _norm_id(row.get("imdb_id"))
        row_tmdb = str(row.get("tmdb_id") or "").strip()
        if item_id in protected_ids:
            row["protected"] = True
        elif row_imdb and row_imdb in protected_imdb_ids:
            row["protected"] = True
        elif row_tmdb and row_tmdb in protected_tmdb_ids:
            row["protected"] = True
        else:
            resolved = resolve_under_library(row.get("file"))
            if resolved and str(resolved) in protected_paths:
                row["protected"] = True
        if row["protected"]:
            _JELLYFIN_PROTECTED_MATCH_KEYS.update(_match_keys(row.get("file")))
            resolved = resolve_under_library(row.get("file"))
            if resolved:
                _JELLYFIN_PROTECTED_MATCH_KEYS.update(_match_keys(str(resolved)))

    return list(rows.values())


# =========================
# SOURCE MERGE
# =========================
# Combine the enabled server(s) into one movie list for candidate building.
# Plex-only and Jellyfin-only pass through unchanged. When both are enabled,
# movies are matched by their resolved /library path and their watch/added data
# is merged (oldest added, most-recent play, summed plays, protection unioned).
# Monitored paths still govern what can actually be deleted.

def _match_keys(raw_path):
    """All keys a source path can match on across servers.

    Plex and Jellyfin often mount the same library through different in-container
    paths, symlinks or Unraid user-shares, so a single fully-resolved key can
    differ for the SAME physical file. We therefore match on a SET of keys — the
    /library path, its symlink-resolved form, and a lowercased trailing-segment
    suffix — and treat two rows as the same file when ANY key is shared. Matching
    only ever unions protection and enables the identity check, so an over-match
    errs toward NOT deleting.
    """
    keys = set()
    if not raw_path:
        return keys
    try:
        p = resolve_under_library(raw_path)
    except Exception:
        p = None
    if p is not None:
        keys.add(str(p))                                   # /library path as-built
        try:
            keys.add(str(p.resolve()))                     # symlink-resolved form
        except Exception:
            pass
        try:
            keys.add("lib:" + str(p.relative_to(LIBRARY_ROOT)).lower())  # case-insensitive rel
        except Exception:
            pass
    else:
        # Nothing exists under /library for this path — fall back to the trailing
        # segments so a diagnostic near-miss can still be detected.
        parts = [s for s in str(raw_path).replace("\\", "/").split("/") if s]
        if parts:
            keys.add("suffix:" + "/".join(parts[-3:]).lower())
    return keys


def _merge_added_at(a, b):
    """Oldest (smallest positive) added timestamp wins; 0/unknown is ignored."""
    vals = [v for v in (parse_int(a, 0), parse_int(b, 0)) if v > 0]
    return min(vals) if vals else 0


def _distinct_users_for_row(row):
    """Distinct watchers for a movie: the HIGHER of the Plex and Jellyfin counts,
    never their sum. Tautulli exposes no per-user breakdown, so a played Plex
    movie counts as 1 Plex watcher; Jellyfin carries a real per-user count. A
    movie present on both servers takes whichever source saw more distinct
    watchers (_plex_users / _jf_users are captured on the merged row at merge
    time, before play stats are combined)."""
    if str(row.get("rating_key") or "").startswith("jf:"):
        return parse_int(row.get("_jf_users"), 0)               # Jellyfin-only row
    if row.get("_jf_matched"):
        return max(parse_int(row.get("_plex_users"), 0),        # on both servers
                   parse_int(row.get("_jf_users"), 0))
    # Plex-only row: Tautulli has no per-user data, so a played movie = 1 watcher.
    return 1 if (parse_int(row.get("play_count"), 0) > 0
                 or parse_int(row.get("last_played"), 0) > 0) else 0


def _norm_id(value):
    """Normalize a provider id (imdb tconst / tmdb id) for equality comparison.
    Returns "" for blank/None so a missing id never counts as a conflict."""
    return str(value).strip().lower() if value not in (None, "") else ""


def _tag_jellyfin_metadata(row):
    """Carry a Jellyfin row's embedded protection/ids in _jf_* keys so the shared
    scan loop uses them (Jellyfin has no Tautulli rating_key to fetch)."""
    row["_jf_protected"] = bool(row.get("protected"))
    row["_jf_tmdb_id"]   = row.get("tmdb_id")
    row["_jf_imdb_id"]   = row.get("imdb_id")
    return row


def get_all_movies():
    """Return the movie list from the enabled server(s), merged by /library path.

    Merge rules when both Plex and Jellyfin are enabled and a movie is present on
    both: OLDEST added date, MOST RECENT last-played, SUMMED play counts, and
    protected if EITHER server protects it. Jellyfin-only movies are appended as
    their own candidates. Plex-only setups are unchanged (byte-identical rows).
    """
    plex_rows  = get_all_movies_from_tautulli()  if USE_PLEX     else []
    jelly_rows = get_all_movies_from_jellyfin()  if USE_JELLYFIN else []

    if not jelly_rows:
        return plex_rows
    if not plex_rows:
        log(f"Jellyfin-only source: {len(jelly_rows)} movies.")
        return [_tag_jellyfin_metadata(r) for r in jelly_rows]

    # Tautulli's movie list carries no file path (the path lives behind a
    # per-movie get_metadata call), so a Plex row has nothing to match a
    # Jellyfin row on at this point. Resolve every Plex row's real /library
    # path up front — the same call the scan makes, cached onto the row so the
    # scan reuses it — so the same physical file collapses to ONE entry instead
    # of appearing once per server (which otherwise doubled every count).
    _unresolved = 0
    for i, prow in enumerate(plex_rows, 1):
        if not prow.get("file"):
            p = extract_file_path(prow, quiet=True)
            if p is not None:
                prow["file"] = str(p)
            else:
                _unresolved += 1
        if i % 100 == 0 or i == len(plex_rows):
            # Resolving paths is preparation for the scan, not the scan itself.
            # Report it under the "Reading library" step's indeterminate creep
            # (no scanned/total) so the Scanning step's bar fills exactly once
            # (0→100). Emitting it as denominatored "scanning" progress made the
            # Scanning bar fill here AND again during the candidate scan.
            emit_progress(phase="library", message="Resolving library paths…")
    if _unresolved:
        log(f"Merge: {_unresolved} Plex row(s) had no resolvable path yet — "
            f"the scan resolves and reports those individually.")

    # Index every Jellyfin row under all of its possible match keys.
    jelly_keyed = []            # [(row, keyset)]
    jelly_index = {}            # match key -> jf row (first wins)
    for r in jelly_rows:
        ks = _match_keys(r.get("file"))
        jelly_keyed.append((r, ks))
        for k in ks:
            jelly_index.setdefault(k, r)

    merged = []
    matched_jf = set()          # id() of Jellyfin rows that matched a Plex row
    for prow in plex_rows:
        pks = _match_keys(prow.get("file"))
        jrow = next((jelly_index[k] for k in pks if k in jelly_index), None)
        if jrow is not None:
            matched_jf.add(id(jrow))
            # Capture each source's OWN distinct-user count before merging play
            # stats (play_count/last_played are combined below, which would
            # otherwise hide whether Plex itself saw a play). Distinct users take
            # the higher of the two, never the sum — see _distinct_users_for_row.
            prow["_plex_users"] = 1 if (parse_int(prow.get("play_count"), 0) > 0
                                        or parse_int(prow.get("last_played"), 0) > 0) else 0
            prow["_jf_users"]   = parse_int(jrow.get("_jf_users"), 0)
            prow["added_at"]    = _merge_added_at(prow.get("added_at"), jrow.get("added_at"))
            prow["last_played"] = max(parse_int(prow.get("last_played"), 0), parse_int(jrow.get("last_played"), 0))
            prow["play_count"]  = parse_int(prow.get("play_count"), 0) + parse_int(jrow.get("play_count"), 0)
            prow["_jf_matched"]  = True   # present on both servers → eligible for cross-server identity check
            prow["_jf_protected"] = bool(jrow.get("protected"))
            prow["_jf_favorite"]  = bool(jrow.get("_jf_favorite"))  # else a both-servers favorite loses its protection
            prow["_jf_tmdb_id"]   = jrow.get("tmdb_id")
            prow["_jf_imdb_id"]   = jrow.get("imdb_id")
        merged.append(prow)

    jf_only_rows = [r for (r, ks) in jelly_keyed if id(r) not in matched_jf]
    merged.extend(_tag_jellyfin_metadata(r) for r in jf_only_rows)
    log(f"Merged sources: {len(plex_rows)} Plex + {len(jelly_rows)} Jellyfin "
        f"→ {len(matched_jf)} matched, {len(jf_only_rows)} Jellyfin-only, {len(merged)} total.")

    # ── Merge diagnostics ────────────────────────────────────────────────────
    # A Jellyfin-only movie that shares a filename with a Plex movie SHOULD have
    # matched. Logging both rows' raw paths and key sets makes any remaining path
    # divergence (mount, symlink, casing, wrong metadata) obvious in one run.
    # A Jellyfin-only movie that shares its folder + filename with a Plex movie
    # SHOULD have matched (the same file has the same folder/name on both servers;
    # only the mount root differs). Keying on parent-folder + filename avoids
    # false positives from two different films that merely share a filename.
    def _twin_key(f):
        p = Path(str(f or ""))
        return (p.parent.name.lower(), p.name.lower()) if p.name else None
    plex_by_twin = {}
    for prow in plex_rows:
        tk = _twin_key(prow.get("file"))
        if tk:
            plex_by_twin.setdefault(tk, prow)
    for r in jf_only_rows:
        prow = plex_by_twin.get(_twin_key(r.get("file")))
        if prow is not None:
            # Same physical file, present on both servers, but it failed to merge —
            # so its Plex vs Jellyfin identity was never reconciled. Tag it so the
            # scan skips it (never delete on an unreconciled identity) and flags the
            # run completed-with-errors, mirroring the merged-row identity check.
            r["_unmerged_plex_twin"] = {"title": prow.get("title"), "file": prow.get("file")}
            log("WARN merge near-miss: same filename identified on both servers but "
                "paths did not match — this movie will be SKIPPED (not deleted) and the "
                "run flagged with errors.")
            log(f"    Jellyfin: title={r.get('title')!r} imdb={r.get('imdb_id')} "
                f"tmdb={r.get('tmdb_id')} file={r.get('file')!r}")
            log(f"              keys={sorted(_match_keys(r.get('file')))}")
            log(f"    Plex:     title={prow.get('title')!r} file={prow.get('file')!r}")
            log(f"              keys={sorted(_match_keys(prow.get('file')))}")
    return merged


# =========================
# CANDIDATE BUILDING
# =========================


def _path_is_within(child, parent):
    """True if `child` is `parent` or nested under it (best-effort, no raises)."""
    try:
        child_r = child.resolve(strict=False)
        parent_r = parent.resolve(strict=False)
        return child_r == parent_r or parent_r in child_r.parents
    except Exception:
        return False


def get_library_size_gb():
    """Monitored library size in GB, summed from disk over movie files under the
    monitored roots. None on error; empty MONITOR_DIRS returns 0.0.

    Uses allocated bytes (block counts) where available, so it tracks `du`. Reads
    off disk rather than Tautulli's get_library_media_info, whose cached media-info
    lags reality after a deletion — which would keep the library cap triggering
    after space was freed — and needs neither Plex nor Tautulli. Each physical file
    is counted once (hardlinks/overlapping roots de-duplicated by inode).
    """
    try:
        roots = monitored_roots()
        if not roots:
            log("Library size: no monitored library paths configured.")
            return 0.0

        # Drop any root nested under another monitored root (shortest paths
        # first) so a shared subtree is not walked / counted twice.
        ordered = sorted(set(roots), key=lambda p: len(str(p)))
        top_roots = []
        for r in ordered:
            if not any(_path_is_within(r, parent) for parent in top_roots):
                top_roots.append(r)

        total_bytes = 0
        counted = set()
        missing = []
        root_stats = {}
        for root in top_roots:
            if not root.exists():
                missing.append(str(root))
                continue
            root_bytes = 0
            root_files = 0
            for dirpath, _dirnames, filenames in _os.walk(root, followlinks=False):
                for fn in filenames:
                    if _os.path.splitext(fn)[1].lower() not in MOVIE_EXTENSIONS:
                        continue
                    fp = _os.path.join(dirpath, fn)
                    if _os.path.islink(fp):
                        continue   # symlinked media is out of scope
                    try:
                        st = _os.stat(fp)
                    except OSError:
                        continue
                    key = (st.st_dev, st.st_ino)
                    if key in counted:
                        continue
                    counted.add(key)
                    size_bytes = int(getattr(st, "st_blocks", 0) or 0) * 512
                    if size_bytes <= 0:
                        size_bytes = st.st_size
                    root_bytes += size_bytes
                    root_files += 1
                    total_bytes += size_bytes
            root_stats[str(root)] = (root_files, root_bytes)

        if missing:
            log(f"WARNING: library size — monitored path(s) not found on disk: {missing}")
        for root, (file_count, byte_count) in root_stats.items():
            log(f"Library size root: {root} | movie_files={file_count} | size={bytes_to_gb(byte_count):.1f} GB")
        return bytes_to_gb(total_bytes)

    except Exception as e:
        log(f"WARNING: Could not compute library size from disk: {e}")
        return None


# =========================
# RETENTION SCORING
# =========================
# Additive RetentionScore — HIGHER means "keep". Pure functions over normalized
# movie records so the run, the Score Explorer, and the tests all share one
# formula. Full component table in the module docstring.

def imdb_vote_confidence(votes):
    """Logarithmic confidence in an IMDb rating from its vote count.

    Vote count is CONFIDENCE for the rating, never standalone popularity.
    Curve numbers live in scoring_constants.SCORING (floor, unknown-votes
    value, and the log10 count that earns full confidence).
    """
    if votes is None:
        return SCORING["VOTE_CONF_UNKNOWN"]
    v = parse_int(votes, 0)
    floor = SCORING["VOTE_CONF_FLOOR"]
    if v <= 0:
        return floor
    return min(1.0, floor + (1.0 - floor) * (math.log10(v) / SCORING["VOTE_CONF_FULL_LOG10"]))


def compute_retention_score(rec, now=None):
    """RetentionScore for a normalized movie record — HIGHER = keep.

    Returns (score, breakdown); breakdown values are already weighted by the
    balance weights (which sum to 1.0), so they sum to the 0–100 score. Record
    fields read: total_play_count, last_played_at, added_at,
    distinct_users_watched, imdb_rating, imdb_num_votes.
    """
    now = now or time.time()
    b = {}

    plays = max(parse_int(rec.get("total_play_count"), 0), 0)
    b["usage"] = (SCORING["USAGE_MAX_PTS"]
                  * min(1.0, math.log1p(plays) / math.log1p(SCORING["USAGE_FULL_PLAYS"]))
                  * HISTORY_WEIGHT)

    users = max(parse_int(rec.get("distinct_users_watched"), 0), 0)

    # Recency: use the last watch if there is one, otherwise fall back to when
    # the movie was added — a recently-added-but-unwatched movie still reads as
    # "fresh" (added a year ago scores like watched a year ago). Only recency
    # benefits; frequency and users stay 0 for a never-watched movie. The tier
    # day-thresholds scale to the Max staleness setting, so the same curve
    # fades to 0 over the chosen window (default = the authored 3 years).
    #
    # Distinct watchers slow that decay: each unique user who watched the movie
    # stretches the effective staleness window, so a widely-watched movie's age
    # score (recency tiers + shelf tail) fades slower than a one-person or
    # never-watched one. 0 users leaves the decay unchanged.
    stale_scale = MAX_STALENESS_MONTHS / SCORING["RECENCY_DEFAULT_MONTHS"]
    decay_mult = min(SCORING["USER_DECAY_MAX_MULT"], 1.0 + SCORING["USER_DECAY_PER_USER"] * users)
    eff_scale = stale_scale * decay_mult
    last_played = parse_int(rec.get("last_played_at"), 0)
    recency_at = last_played if last_played > 0 else parse_int(rec.get("added_at"), 0)
    rec_pts = 0.0
    shelf_pts = 0.0
    if recency_at > 0:
        days_since = (now - recency_at) / 86400.0
        for max_days, pts in SCORING["RECENCY_TIERS"]:
            if days_since <= max_days * eff_scale:
                rec_pts = pts
                break
        # Soft shelf: continues the recency curve past its last tier (the
        # staleness cliff). It reads the SAME recency date as the tiers above —
        # last-played, or the added date when never played — so added-date and
        # last-played are one timeline, not two separate inputs.
        cliff_days = SCORING["RECENCY_TIERS"][-1][0] * eff_scale
        span_days = cliff_days * SCORING["SHELF_SPAN_MULT"]
        if days_since > cliff_days and span_days > 0:
            frac = 1.0 - (days_since - cliff_days) / span_days
            shelf_pts = SCORING["SHELF_MAX_PTS"] * max(0.0, min(1.0, frac))
    b["recency"] = rec_pts * HISTORY_WEIGHT

    b["multi_user"] = (min(SCORING["MULTI_USER_PTS"] * users, SCORING["MULTI_USER_MAX_PTS"])
                       * HISTORY_WEIGHT)

    rating = rec.get("imdb_rating")
    if rating is not None:
        conf = imdb_vote_confidence(rec.get("imdb_num_votes"))
        b["imdb"] = min(float(rating) * 10.0 * conf, 100.0) * QUALITY_WEIGHT
    else:
        b["imdb"] = 0.0

    # Soft shelf weight is a tent — 0 at 100% watch history AND at 100% IMDb,
    # peaking mid-blend — so 100% history stays a hard cliff and 100% IMDb stays
    # pure quality; the shelf only augments the blended middle (shelf_pts is the
    # recency-curve tail computed above).
    full_q = SCORING["SHELF_RAMP_FULL_Q"]
    shelf_ramp = min(1.0, QUALITY_WEIGHT / full_q) if full_q > 0 else 1.0
    b["shelf"] = shelf_pts * HISTORY_WEIGHT * shelf_ramp

    return sum(b.values()), b


# ── Score Explorer sample pool ────────────────────────────────────────────────
# cache.json's "sample_pool" key backs the Score Explorer's library-sample
# table: real movies with their merged Plex+Jellyfin data plus eligibility
# facts. It is written ONLY by the quick sample_pool mode below (config saves
# that change monitored paths or API connections, API reconnect, the
# explorer's Refresh button, and an IMDb-hold-release retry) — Simulate/Live
# scans never touch it, so the user's chosen batch survives runs (save_cache
# re-reads the key from disk, and load_cache keeps it across engine-code
# cache clears).
def _sample_pool_entry(title, year, rating, votes, plays, users,
                       last_played, added_at, size_bytes,
                       protected=False, favorite=False) -> dict:
    return {
        "title": str(title or ""),
        "year": parse_int(year, 0) or None,
        "rating": float(rating) if rating is not None else None,
        "votes": parse_int(votes, 0),
        "plays": parse_int(plays, 0),
        "users": parse_int(users, 0),
        "last_played": parse_int(last_played, 0),
        "added_at": parse_int(added_at, 0),
        "size_gb": round(parse_int(size_bytes, 0) / 1e9, 2),
        # Eligibility facts, not verdicts: the Score Explorer applies the
        # (possibly previewed) filter settings to these client-side.
        "protected": bool(protected),
        "favorite": bool(favorite),
    }


def _write_sample_pool_file(movies: list) -> None:
    """Merge the sample pool into cache.json (same read-modify-write shape as
    emit_stats, so nothing else in the cache is disturbed)."""
    payload = {
        "built_at": int(time.time()),
        "movies": movies,
    }
    with _cache_write_lock():
        data = _cache_base_for_merge()
        data["sample_pool"] = payload
        _replace_cache_file(data)


# ── Quick sample pool (RUN_MODE=sample_pool) ─────────────────────────────────
# The Score Explorer shouldn't have to wait for a full Simulate/Live scan to
# get real library data. This mode pulls the plain movie listing straight from
# the connected server APIs, picks a random batch of movies that live under
# the monitored library paths, resolves IMDb ratings best-effort, and
# rebuilds the cache's sample pool — no scoring, no candidate filtering, no
# deletion. The web app runs it after saves that change the monitored paths
# or API connections, on API reconnect, and from the Score Explorer's
# Refresh button (which passes the batch size the user picked).
_QUICK_SAMPLE_TARGET = 10
# Rows without an inline file path need one Tautulli metadata call each to
# resolve their path for the monitored-dirs check. Cap those lookups so a
# library that mostly lives outside the monitored paths can't turn a quick
# sample build into a full-library metadata crawl.
_QUICK_SAMPLE_LOOKUP_CAP = 200


# Exit code for a sample_pool build that stops because the IMDb dataset could
# not be obtained. The web app answers it with a toast (automatic builds) or
# the manual-fix popup (explicit Refresh) and holds automatic sample builds
# until the dataset problem is resolved.
SAMPLE_EXIT_IMDB_UNAVAILABLE = 3
# Exit code for a sample build that scanned fine but found no movies under
# the monitored paths — a path problem, not a connection problem. The web app
# maps it to a message pointing at the Configuration page's paths.
SAMPLE_EXIT_NO_MOVIES = 4


def _ensure_imdb_dataset_for_sample() -> None:
    """Make sure a fresh IMDb ratings TSV is on disk before sampling starts.

    The library sample is scored, so the dataset is required just like in a
    run — and held to the same freshness rule: a copy older than the
    configured refresh interval must be re-downloaded, because scores from
    stale ratings feed the same deletion decisions. A missing file tries the
    manual title.ratings.tsv.gz fallback first, then a download; a failed
    refresh of a stale file falls back to the manual .gz too. Otherwise the
    build exits with SAMPLE_EXIT_IMDB_UNAVAILABLE and writes no pool file.
    """
    refresh_days = _imdb_refresh_days()
    if IMDB_RATINGS_PATH.exists():
        age_days = (time.time() - IMDB_RATINGS_PATH.stat().st_mtime) / 86400
        if age_days < refresh_days:
            return
        log(f"IMDB ratings file is {age_days:.1f} days old (limit={refresh_days}d), re-downloading for the sample.")
    elif _extract_local_imdb_gz():
        return
    try:
        IMDB_RATINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        log(f"Downloading IMDB ratings dataset from {IMDB_RATINGS_URL} ...")
        with urllib.request.urlopen(IMDB_RATINGS_URL, timeout=120) as resp:
            gz_data = resp.read(_IMDB_GZ_MAX_BYTES + 1)
        _write_imdb_tsv(_bounded_gunzip(gz_data))
    except Exception as e:
        log(f"ERROR: could not obtain a fresh IMDb ratings dataset for the sample pool: {e}")
        if _extract_local_imdb_gz():
            log("Using manually-provided title.ratings.tsv.gz from the config folder.")
            return
        log("       Manual fix: download title.ratings.tsv.gz from "
            "https://datasets.imdbws.com/ (IMDb Non-Commercial Datasets) and place "
            "it in the MediaReducer config folder, then press Refresh on the "
            "Filtering & Scoring page.")
        raise SystemExit(SAMPLE_EXIT_IMDB_UNAVAILABLE)


def _load_imdb_ratings_subset(wanted_ids: set) -> dict:
    """Best-effort {tt_id: (rating, votes)} lookup for a small id set.

    Reads only — _ensure_imdb_dataset_for_sample() has already put the TSV on
    disk. A read problem degrades to unrated rows rather than failing a batch
    that was otherwise sampled fine.
    """
    wanted = {str(i).strip() for i in wanted_ids if i}
    if not wanted or not IMDB_RATINGS_PATH.exists():
        return {}
    ratings: dict = {}
    try:
        with open(IMDB_RATINGS_PATH, "r", encoding="utf-8") as f:
            next(f, None)  # header
            for line in f:
                parts = line.split("\t")
                if len(parts) >= 3 and parts[0] in wanted:
                    try:
                        ratings[parts[0]] = (float(parts[1]), int(parts[2].strip()))
                    except ValueError:
                        pass
                    if len(ratings) == len(wanted):
                        break
    except Exception as e:
        log(f"WARN: could not read IMDb ratings for the sample pool: {e}")
    return ratings


def _quick_sample_file_path(row, allow_api_lookup=True) -> tuple:
    """(resolved /library Path or None, used_api_lookup) for a listing row.

    Same resolution order as extract_file_path(), but best-effort: a quick
    sample build skips rows whose path can't be resolved instead of aborting
    the run on an API failure.
    """
    possible_keys = ("file", "file_path", "media_file", "location", "path")

    def _from_containers(containers):
        for c in containers:
            if not isinstance(c, dict):
                continue
            for key in possible_keys:
                if c.get(key):
                    return resolve_under_library(c[key])
        return None

    containers = [row]
    if isinstance(row.get("media_info"), list):
        containers.extend(row["media_info"])
    path = _from_containers(containers)
    if path is not None:
        return path, False

    rating_key = row.get("rating_key")
    if not allow_api_lookup or not rating_key or str(rating_key).startswith("jf:"):
        return None, False
    time.sleep(0.05)  # same pacing as the scan: don't hammer Tautulli
    try:
        metadata = tautulli_api("get_metadata", rating_key=rating_key, media_info=1)
    except Exception as e:
        log(f"WARN: file-path lookup failed for {row.get('title')!r}: {e}")
        return None, True
    containers = [metadata]
    if isinstance(metadata.get("media_info"), list):
        for media in metadata["media_info"]:
            containers.append(media)
            if isinstance(media.get("parts"), list):
                containers.extend(media["parts"])
    return _from_containers(containers), True


def _quick_sample_row_meta(row) -> tuple:
    """(imdb_id, protected) for a listing row without aborting on API failure.

    Jellyfin rows already carry both (id from ProviderIds, protection from
    BoxSets); Tautulli rows need one get_metadata call — answered from the
    metadata cache when possible — which also yields the movie's collections
    for the PROTECTED_COLLECTIONS check.
    """
    protected = bool(row.get("protected")) or bool(row.get("_jf_protected"))
    imdb = row.get("imdb_id") or row.get("_jf_imdb_id")
    imdb = (str(imdb).strip() or None) if imdb else None
    rating_key = row.get("rating_key")
    if row.get("_source") == "jellyfin" or not rating_key:
        return imdb, protected
    cached = _metadata_cache.get(rating_key)
    if cached:
        return imdb or cached.get("imdb_id"), protected or bool(cached.get("protected"))
    time.sleep(0.05)  # same pacing as the scan: don't hammer Tautulli
    try:
        metadata = tautulli_api("get_metadata", rating_key=rating_key, media_info=0)
    except Exception as e:
        log(f"WARN: metadata lookup failed for {row.get('title')!r}: {e}")
        return imdb, protected
    for guid in (metadata.get("guids") or []):
        if isinstance(guid, str) and guid.startswith("imdb://") and not imdb:
            imdb = guid.replace("imdb://", "").strip() or None
    collections = metadata.get("collections") or []
    if any((isinstance(e, str) and e in PROTECTED_COLLECTIONS) or
           (isinstance(e, dict) and e.get("tag") in PROTECTED_COLLECTIONS)
           for e in collections):
        protected = True
    return imdb, protected


def _iter_tautulli_random_rows(page_len: int = 25):
    """Yield Tautulli movie rows from RANDOM pages of each movie section.

    get_library_media_info is a heavyweight query for Tautulli, so the sampler
    never pulls the full listing: it reads each section's total row count
    (length=1), shuffles the page offsets, and fetches small pages until the
    caller has enough movies. A 10-movie batch typically costs one count query
    and one 25-row page per section.
    """
    section_pages = []
    for section_id in get_movie_section_ids():
        data = tautulli_api(
            "get_library_media_info",
            section_id=section_id, section_type="movie",
            start=0, length=1, order_column="title", order_dir="asc",
        )
        total = parse_int(data.get("recordsFiltered"), 0) if isinstance(data, dict) else 0
        if total <= 0:
            # Older Tautulli without a usable count: fall back to one big page.
            total = page_len * 8
        for start in range(0, total, page_len):
            section_pages.append((section_id, start))
    random.shuffle(section_pages)
    for section_id, start in section_pages:
        data = tautulli_api(
            "get_library_media_info",
            section_id=section_id, section_type="movie",
            start=start, length=page_len, order_column="title", order_dir="asc",
        )
        rows = data if isinstance(data, list) else (((data or {}).get("data")) or [])
        random.shuffle(rows)
        for row in rows:
            if isinstance(row, dict):
                row["_section_id"] = section_id
                yield row


def _jellyfin_light_rows() -> list:
    """All Jellyfin movies as light rows: one Items call, NO per-user play
    aggregation and NO protection lookups — those are filled in afterwards for
    just the sampled movies by _jellyfin_enrich_sampled()."""
    base = (_jellyfin_request("Items", {
        "IncludeItemTypes": "Movie",
        "Recursive": "true",
        "Fields": "Path,MediaSources,DateCreated,ProviderIds",
        "EnableUserData": "false",
    }) or {}).get("Items", [])
    rows = []
    for item in base:
        item_id = item.get("Id")
        if not item_id:
            continue
        path = item.get("Path") or ""
        size = 0
        for ms in (item.get("MediaSources") or []):
            if not isinstance(ms, dict):
                continue
            if not path:
                path = ms.get("Path") or ""
            if not size and ms.get("Size"):
                size = ms["Size"]
        prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
        rows.append({
            "rating_key":  f"jf:{item_id}",
            "title":       item.get("Name") or "",
            "year":        item.get("ProductionYear") or "",
            "file":        path,
            "file_size":   size,
            "added_at":    _jellyfin_date_to_epoch(item.get("DateCreated")),
            "last_played": 0,
            "play_count":  0,
            "tmdb_id":     (str(prov["tmdb"]).strip() if prov.get("tmdb") else None),
            "imdb_id":     (str(prov["imdb"]).strip() if prov.get("imdb") else None),
            "protected":   False,
            "_source":     "jellyfin",
            "_jf_users":   0,
            "_jf_favorite": False,
        })
    return rows


def _jellyfin_enrich_sampled(rows: list) -> None:
    """Per-user play stats, favorites, and BoxSet protection for ONLY the
    sampled Jellyfin rows (one Ids=... query per user instead of aggregating
    the entire library). Best-effort: a failed lookup leaves the row unplayed."""
    by_id = {str(r["rating_key"])[3:]: r for r in rows
             if str(r.get("rating_key") or "").startswith("jf:")}
    if not by_id:
        return
    ids_param = ",".join(by_id.keys())
    try:
        user_ids = _jellyfin_user_ids()
    except Exception as e:
        log(f"WARN: Jellyfin user listing failed for the sample: {e}")
        user_ids = []
    for uid in user_ids:
        try:
            items = (_jellyfin_request(f"Users/{uid}/Items", {
                "Ids": ids_param,
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "EnableUserData": "true",
            }) or {}).get("Items", [])
        except Exception as e:
            log(f"WARN: Jellyfin play-history lookup failed for user {uid}: {e}")
            continue
        for item in items:
            row = by_id.get(str(item.get("Id")))
            if not row:
                continue
            ud = item.get("UserData") or {}
            user_plays = parse_int(ud.get("PlayCount"), 0)
            row["play_count"] = parse_int(row.get("play_count"), 0) + user_plays
            if user_plays > 0 or ud.get("Played"):
                row["_jf_users"] = parse_int(row.get("_jf_users"), 0) + 1
            if ud.get("IsFavorite"):
                row["_jf_favorite"] = True
            lp = _jellyfin_date_to_epoch(ud.get("LastPlayedDate"))
            if lp > parse_int(row.get("last_played"), 0):
                row["last_played"] = lp
    if JELLYFIN_PROTECTED_COLLECTIONS:
        try:
            protected_ids, protected_paths, protected_imdb, protected_tmdb = _jellyfin_protected_items()
        except Exception as e:
            log(f"WARN: Jellyfin protected-collection lookup failed for the sample: {e}")
            return
        for item_id, row in by_id.items():
            if (item_id in protected_ids
                    or (_norm_id(row.get("imdb_id")) and _norm_id(row.get("imdb_id")) in protected_imdb)
                    or (str(row.get("tmdb_id") or "").strip() and str(row.get("tmdb_id") or "").strip() in protected_tmdb)):
                row["protected"] = True


def build_quick_sample_pool(target: int = 0) -> None:
    if target <= 0:
        target = _QUICK_SAMPLE_TARGET
    target = min(target, 100)
    if not validate_connections():
        raise SystemExit(1)
    # The sample only draws from monitored paths — with none configured there
    # is nothing to sample, so leave the pool untouched (blank until the user
    # adds a monitored library path).
    if not MONITOR_DIRS:
        log("Sample pool not written: no monitored library paths are configured.")
        raise SystemExit(1)
    # The dataset check runs before any API sampling so a broken download
    # fails the build fast instead of after the whole batch was collected. At
    # 100% watch history with no rating cutoff the sample never NEEDS IMDb, so
    # nothing is downloaded — but if the dataset is already on disk (an earlier
    # download, or a manually-placed .gz) the sample is still annotated with
    # ratings from it, whatever its age: ratings carry zero score weight at
    # this balance, and having them means moving the dial toward IMDb previews
    # real data instead of flagging every movie as "no IMDb data".
    _sample_use_imdb = _sample_needs_imdb = imdb_dataset_needed()
    if _sample_needs_imdb:
        _ensure_imdb_dataset_for_sample()
    elif IMDB_RATINGS_PATH.exists() or _extract_local_imdb_gz():
        _sample_use_imdb = True
        log("IMDb download skipped (scoring is 100% watch history), but the dataset "
            "is already on disk — annotating the sample with ratings/votes from it.")
    else:
        log("IMDb dataset skipped for the sample: scoring is 100% watch history "
            "with no rating cutoff and no dataset is on disk, so the sample is "
            "built without ratings/votes.")
    # Reuse cached rating_key → imdb_id lookups from past runs so already-known
    # movies skip the per-movie get_metadata call.
    for rk, entry in (load_cache().get("movies") or {}).items():
        if isinstance(entry, dict) and entry.get("v") == 2:
            _metadata_cache.setdefault(rk, {
                "protected": entry.get("protected", False),
                "tmdb_id":   entry.get("tmdb_id"),
                "imdb_id":   entry.get("imdb_id"),
                "v": 2,
            })

    # Collect rows, keeping only movies that resolve to a file under a
    # monitored path, until the batch is full. Single-server setups use light
    # sources (random Tautulli pages / one Jellyfin listing); only a dual
    # Plex+Jellyfin setup needs the full merged listing so cross-server play
    # data stays combined.
    picked = []
    seen_keys = set()
    picked_by_rpath = {}       # resolved path key -> index in `picked`
    scanned = 0
    api_lookups = 0
    _sample_jelly_index = {}   # match key -> Jellyfin row (dual-source merge only)
    _sample_plex_by_ty = {}    # (title, year) -> [Tautulli rows] (dual-source twin lookup)

    def _fold_jelly(row, path) -> None:
        """Fold a matching Jellyfin row's data into a Plex row (mirrors the
        authoritative get_all_movies() merge): combined play stats, the higher
        distinct-user count, and Jellyfin's protection / provider ids — so a
        both-servers movie carries the SAME merged facts the real scan would
        give it. No-op for a Jellyfin row (nothing to fold into) or a Plex row
        with no Jellyfin twin. Uses the pre-built index, so no extra API calls."""
        if not _sample_jelly_index or str(row.get("rating_key") or "").startswith("jf:"):
            return
        jr = next((_sample_jelly_index[k] for k in _match_keys(str(path)) if k in _sample_jelly_index), None)
        if jr is None:
            return
        row["_plex_users"]   = 1 if (parse_int(row.get("play_count"), 0) > 0 or parse_int(row.get("last_played"), 0) > 0) else 0
        row["_jf_users"]     = parse_int(jr.get("_jf_users"), 0)
        row["play_count"]    = parse_int(row.get("play_count"), 0) + parse_int(jr.get("play_count"), 0)
        row["last_played"]   = max(parse_int(row.get("last_played"), 0), parse_int(jr.get("last_played"), 0))
        row["added_at"]      = _merge_added_at(row.get("added_at"), jr.get("added_at"))
        # Same file on both servers: keep whichever size is known (a Tautulli
        # listing row can lack file_size where Jellyfin's MediaSources has it).
        row["file_size"]     = parse_int(row.get("file_size"), 0) or parse_int(jr.get("file_size"), 0)
        row["_jf_matched"]   = True
        row["_jf_protected"] = bool(jr.get("protected"))
        row["_jf_favorite"]  = bool(jr.get("_jf_favorite"))
        row["_jf_tmdb_id"]   = jr.get("tmdb_id")
        row["_jf_imdb_id"]   = jr.get("imdb_id")

    def _ty_key(row):
        """(title, year) match key for the Plex-twin reverse lookup."""
        return (str(row.get("title") or "").strip().lower(), parse_int(row.get("year"), 0))

    def _plex_twin_for(jf_row, jf_path):
        """The not-yet-considered Tautulli row for the SAME file as a picked
        Jellyfin row, or None. Candidates come from a title/year index (cheap);
        each is CONFIRMED by resolving its real path (API-capped) — title/year
        alone can collide across editions, the path cannot. A confirmed twin is
        claimed in seen_keys so the main loop skips it later. Without this, a
        Jellyfin row picked before its Plex twin was scanned (the loop stops at
        the target, so most Plex rows never are) would keep Jellyfin-only stats
        — plays=0 for a Plex-watched movie."""
        nonlocal api_lookups
        for cand in _sample_plex_by_ty.get(_ty_key(jf_row), ()):
            raw = str(cand.get("file") or cand.get("rating_key") or "")
            if raw and raw in seen_keys:
                continue   # already considered (and evidently didn't resolve here)
            p, used = _quick_sample_file_path(
                cand, allow_api_lookup=api_lookups < _QUICK_SAMPLE_LOOKUP_CAP)
            if used:
                api_lookups += 1
            if p is not None and str(p) == str(jf_path):
                for k in (str(cand.get("file") or ""), str(cand.get("rating_key") or "")):
                    if k:
                        seen_keys.add(k)
                return cand
        return None

    def _consider(row) -> None:
        nonlocal scanned, api_lookups
        scanned += 1
        is_jf = str(row.get("rating_key") or "").startswith("jf:")
        key = str(row.get("file") or row.get("rating_key") or "")
        if key:
            if key in seen_keys:
                return
            # Memoize before any filtering so a rejected row (outside the
            # monitored paths, unresolvable) can't re-spend API lookups when
            # its duplicates come around on later shuffled pages.
            seen_keys.add(key)
        path, used_api = _quick_sample_file_path(
            row, allow_api_lookup=api_lookups < _QUICK_SAMPLE_LOOKUP_CAP)
        if used_api:
            api_lookups += 1
        if path is None or not is_under_monitored_dir(path):
            return
        # Dedup by RESOLVED path: with both servers sampled from light rows, a
        # movie present on both would otherwise appear twice (a Plex row and a
        # Jellyfin row for the same file).
        resolved_key = "rp:" + str(path)
        if resolved_key in seen_keys:
            # A twin (same physical file on the other server) was already
            # handled. If THIS is the Plex twin and a Jellyfin twin is the one
            # picked, swap the fully-folded Plex row in: it is the authoritative
            # merge (Plex play data + Jellyfin protection/ids), whereas the
            # Jellyfin-only pick would report plays=0 for a Plex-watched movie
            # and miss Plex-side protection. Order-independent result.
            prev_i = picked_by_rpath.get(resolved_key)
            if (not is_jf and prev_i is not None
                    and str(picked[prev_i].get("rating_key") or "").startswith("jf:")):
                try:
                    if path.is_symlink():
                        return
                except OSError:
                    return
                _fold_jelly(row, path)
                picked[prev_i] = row
            return
        seen_keys.add(resolved_key)
        try:
            if path.is_symlink():
                return   # symlinked media is out of scope, same as the scan
        except OSError:
            return
        # Dual-source: a movie on both servers must enter the pool as its
        # PLEX row with the Jellyfin twin folded in — that is the merge the
        # real scan produces. When the pick is the Jellyfin row, reverse-look
        # its Plex twin up now (path-confirmed) and pick that instead.
        if is_jf and _sample_plex_by_ty:
            twin = _plex_twin_for(row, path)
            if twin is not None:
                row = twin
        _fold_jelly(row, path)
        picked_by_rpath[resolved_key] = len(picked)
        picked.append(row)

    if USE_PLEX and USE_JELLYFIN:
        # Sample from LIGHT rows and resolve Plex paths per-row (capped) — NOT
        # get_all_movies(), which pre-resolves every Plex path (thousands of
        # metadata calls) to merge the whole library and would hang a quick
        # sample. Jellyfin rows carry paths + play data inline, so index them
        # once and fold a match into a sampled Plex row (see _consider) — the
        # sample keeps cross-source data combined without the full-library crawl.
        jelly_rows = get_all_movies_from_jellyfin()
        for _jr in jelly_rows:
            for _k in _match_keys(_jr.get("file")):
                _sample_jelly_index.setdefault(_k, _jr)
        plex_rows = get_all_movies_from_tautulli()
        for _pr in plex_rows:
            _sample_plex_by_ty.setdefault(_ty_key(_pr), []).append(_pr)
        rows = plex_rows + [_tag_jellyfin_metadata(r) for r in jelly_rows]
        random.shuffle(rows)
        for row in rows:
            if len(picked) >= target:
                break
            _consider(row)
    elif USE_PLEX:
        for row in _iter_tautulli_random_rows():
            if len(picked) >= target:
                break
            _consider(row)
    else:
        rows = _jellyfin_light_rows()
        random.shuffle(rows)
        for row in rows:
            if len(picked) >= target:
                break
            _consider(row)
        _jellyfin_enrich_sampled(picked)

    if not picked:
        log("Sample pool not written: no movies found under the monitored library paths.")
        raise SystemExit(SAMPLE_EXIT_NO_MOVIES)
    log(f"Building quick sample pool: {len(picked)} of target {target} movies "
        f"(scanned {scanned} rows, {api_lookups} path lookups).")

    imdb_by_index = {}
    protected_by_index = {}
    for i, row in enumerate(picked):
        imdb_id, protected = _quick_sample_row_meta(row)
        if imdb_id:
            imdb_by_index[i] = imdb_id
        protected_by_index[i] = protected
    ratings = _load_imdb_ratings_subset(set(imdb_by_index.values())) if _sample_use_imdb else {}

    movies = []
    for i, row in enumerate(picked):
        rating, votes = ratings.get(imdb_by_index.get(i), (None, None))
        play_count = parse_int(row.get("play_count"), 0)
        last_played = parse_int(row.get("last_played"), 0)
        # Distinct users: higher of the Plex and Jellyfin counts, never the sum.
        users = _distinct_users_for_row(row)
        movies.append(_sample_pool_entry(
            row.get("title"), row.get("year"), rating, votes,
            play_count, users, last_played, row.get("added_at"),
            row.get("file_size"),
            protected=protected_by_index.get(i, False),
            favorite=bool(row.get("_jf_favorite"))))

    _write_sample_pool_file(movies)
    log(f"Score Explorer sample pool written: {len(movies)} movies (quick API sample).")


# Near-tie window in retention-score points — the "File size optimization"
# setting (None = off). The Score Explorer's popNextDeletion() mirrors the
# logic below; change one side only and the preview diverges from the run.
NEAR_TIE_PTS = 2.0


def _pop_from_tie_group(tie_group: list, remaining_bytes):
    """Pick from the boundary tie group: if any single member covers the
    remaining target, the lowest-scoring one that covers it goes; otherwise
    the largest tied file goes first."""
    best_i = None
    best_key = None
    for i, c in enumerate(tie_group):
        size = parse_int(c.get("file_size"), 0)
        if size >= remaining_bytes:
            key = (c["retention_score"], size, str(c.get("title") or ""))
            if best_key is None or key < best_key:
                best_key = key
                best_i = i
    if best_i is not None:
        chosen = tie_group.pop(best_i)
        log(f"File size optimization: {chosen['title']} "
            f"({bytes_to_gb(parse_int(chosen.get('file_size'), 0)):.1f} GB, score "
            f"{chosen['retention_score']:.1f}) covers the remaining "
            f"{bytes_to_gb(remaining_bytes):.1f} GB — picked from the tied group.")
        return chosen
    best_i = 0
    best_key = None
    for i, c in enumerate(tie_group):
        key = (-parse_int(c.get("file_size"), 0), c["retention_score"], str(c.get("title") or ""))
        if best_key is None or key < best_key:
            best_key = key
            best_i = i
    return tie_group.pop(best_i)


def _pop_next_deletion(pending: list, tie_group: list, remaining_bytes):
    """Pick the next movie to delete and remove it from its list.

    Strict score order (`pending` is already sorted) until the movies
    near-tied with the head (scores within NEAR_TIE_PTS) hold MORE space
    than what is left to free — only some of that group needs to go, so it
    moves into `tie_group` and _pop_from_tie_group picks the cheapest path
    to the target. With the optimization off, or no positive target, strict
    score order throughout.
    """
    if tie_group:
        if not remaining_bytes or remaining_bytes <= 0:
            return tie_group.pop(0)
        return _pop_from_tie_group(tie_group, remaining_bytes)
    if NEAR_TIE_PTS and remaining_bytes and remaining_bytes > 0 and len(pending) > 1:
        head_score = pending[0]["retention_score"]
        j = 0
        total = 0
        while j < len(pending) and pending[j]["retention_score"] - head_score <= NEAR_TIE_PTS:
            total += parse_int(pending[j].get("file_size"), 0)
            j += 1
        if j >= 2 and total > remaining_bytes:
            tie_group.extend(pending[:j])
            del pending[:j]
            log(f"File size optimization: the remaining {bytes_to_gb(remaining_bytes):.1f} GB "
                f"target falls inside a group of {j} near-tied movies (scores "
                f"{head_score:.1f}–{tie_group[-1]['retention_score']:.1f}) — picking inside the "
                f"group so the target costs the fewest movies.")
            return _pop_from_tie_group(tie_group, remaining_bytes)
    return pending.pop(0)


def score_candidate(c, now=None):
    """Compute and attach retention_score to a candidate dict
    (candidates use file_size/play_count/... names; map to record fields)."""
    rec = {
        "total_play_count":       c.get("play_count"),
        "last_played_at":         c.get("last_played"),
        "added_at":               c.get("added_at"),
        "distinct_users_watched": c.get("distinct_users"),
        "imdb_rating":            c.get("imdb_rating"),
        "imdb_num_votes":         c.get("imdb_votes"),
    }
    score, _breakdown = compute_retention_score(rec, now=now)
    c["retention_score"] = round(score, 4)
    return c


def build_candidates():
    """
    Fetch all movies and return:
      - candidates: sorted list of deletion candidates
      - section1_paths_by_tmdb: {tmdb_id: set of resolved Paths} for ALL
        on-disk movies in RADARR_OVERSEERR_SECTION_ID (including protected
        movies and movies not selected for deletion), used at delete time to
        check whether a surviving copy exists before triggering Radarr
        cleanup.

    Deletion order is the RetentionScore ASCENDING (see module docstring for
    the full formula): the lowest-value movie is deleted first. Exact score
    ties: never-watched first, then (only when IMDb is in use) lowest IMDb
    rating first, oldest added, larger files first, then title.

    Hard exclusions applied before scoring (any failure excludes the movie —
    these are never weighted):
      - Member of a protected Plex collection or Jellyfin BoxSet.
      - Favorited by any Jellyfin user when PROTECT_JELLYFIN_FAVORITES is on.
      - Added within the GRACE_PERIOD_DAYS grace period.
      - Played never, when SKIP_UNPLAYED_MOVIES is enabled.
      - Rated above MAX_IMDB_RATING on IMDb, when that cutoff is set.
      - No IMDb rating/votes found, when IMDb is in use (any IMDb weight or
        a rating cutoff) — not enough data to judge the movie. At 100% watch
        history unrated movies stay eligible.
      - No resolvable file path, missing on disk, bad extension, or outside
        every MONITOR_DIRS root.
      - Plex and Jellyfin disagree on the file's identity (IMDb/TMDb conflict).
    """
    candidates = []
    section1_paths_by_tmdb: dict = {}  # tmdb_id -> set of resolved Paths
    identity_mismatches: list = []     # movies the two servers identify differently
    _mismatch_skip_paths: set = set()  # resolved paths a Plex row flagged as a conflict
    stats = {
        "no_file_path": 0,
        "bad_extension": 0,
        "missing_on_disk": 0,
        "symlink": 0,
        "outside_monitored_dirs": 0,
        "protected": 0,
        "identity_mismatch": 0,
        "jellyfin_favorite": 0,
        "recently_added": 0,
        "unplayed": 0,
        "high_rated": 0,
        "no_imdb_data": 0,
        "eligible": 0,
    }
    # ── Persistent metadata cache ─────────────────────────────────────────────
    # Loads cached rating_key → {protected, tmdb_id, imdb_id} entries so we
    # can skip the slow per-movie get_metadata API calls on subsequent runs.
    cache = load_cache()
    config_hash = compute_config_hash()
    cached_movies = cache.setdefault("movies", {})

    config_changed = cache.get("config_hash") != config_hash

    if config_changed:
        # A full re-fetch only happens when the metadata/protection SOURCE
        # changes (a connection, which servers are used, or protected
        # collections). Threshold, scoring, scheduling, Radarr and monitored-path
        # changes do not land here, so they keep the cache and re-hit no APIs.
        # last_cleanup_date is preserved so a config save never triggers an extra
        # scheduled cleanup on the same day. The manual Config → Advanced Clear
        # Cache button is the explicit full-wipe path.
        preserved_last_cleanup_date = cache.get("last_cleanup_date")
        log("Metadata source/protection config changed — clearing movie metadata cache for a full refresh (daily cleanup state preserved).")
        cached_movies.clear()
        cache["config_hash"] = config_hash
        if preserved_last_cleanup_date:
            cache["last_cleanup_date"] = preserved_last_cleanup_date

    if not MONITOR_DIRS:
        save_cache(cache)
        log("No monitored library paths configured — skipping movie scan. Add at least one path under Config → Movie Libraries.")
        return [], {}, stats, 0

    # Pre-populate the in-memory metadata cache from the persistent cache so
    # fetch_movie_metadata() returns immediately for already-known movies.
    # Entries from the pre-v2 cache schema are treated as misses and refetched
    # once so every entry carries the current fields.
    _v1_entries = [rk for rk, e in cached_movies.items() if e.get("v") != 2]
    if _v1_entries:
        log(f"Cache: {len(_v1_entries)} entr(ies) use an old schema — refetching those movies once.")
        for rk in _v1_entries:
            cached_movies.pop(rk, None)
    for rk, entry in cached_movies.items():
        _metadata_cache[rk] = {
            "protected": entry.get("protected", False),
            "tmdb_id":   entry.get("tmdb_id"),
            "imdb_id":   entry.get("imdb_id"),
            "v": 2,
        }

    # ── Live Protected collection check via Plex API ──────────────────────────
    # Fetch the current protected set directly from Plex. Protection matches by
    # resolved /library PATH (authoritative — the same thing deletion uses) plus
    # rating_key, so it works on the very first run after a config change and
    # can't be defeated by a Tautulli vs Plex rating_key/section-id mismatch.
    # Plex-only concern: None means Plex is not in use (Jellyfin protection is
    # resolved per-row from BoxSets instead).
    if USE_PLEX:
        plex_protected_paths, plex_protected_keys, plex_protected_imdb_ids, plex_protected_tmdb_ids = fetch_protected_paths()
    else:
        plex_protected_paths, plex_protected_keys, plex_protected_imdb_ids, plex_protected_tmdb_ids = None, None, None, None
    # ─────────────────────────────────────────────────────────────────────────

    try:
        movies = get_all_movies()
    except Exception as e:
        _abort_api_failure(f"Media library API query failed during run: {e}", phase="scanning")
    total_movies = len(movies)

    # Remove stale cache entries (Plex movies no longer present). The metadata
    # cache is Plex-only (keyed by Tautulli rating_key), so Jellyfin rows
    # (rating_key "jf:…") are excluded from cache accounting.
    current_keys = {str(rk) for item in movies
                    if (rk := item.get("rating_key")) and not str(rk).startswith("jf:")}
    stale_keys   = set(cached_movies.keys()) - current_keys
    if stale_keys and not current_keys and cached_movies:
        # A scan that returned zero Plex movies against a non-empty cache is
        # far more likely a transient empty API response than a genuinely
        # emptied library. Keep the cache; a later healthy scan prunes it.
        log(f"Cache: scan returned 0 Plex movies but the cache holds {len(cached_movies)}; keeping cache.")
        stale_keys = set()
    if stale_keys:
        log(f"Cache: pruning {len(stale_keys)} stale entries (no longer in Plex).")
        for k in stale_keys:
            cached_movies.pop(k, None)
            _metadata_cache.pop(k, None)

    new_count = len(current_keys - set(cached_movies.keys()))
    hit_count = len(current_keys) - new_count
    log(f"Cache: {hit_count} hits, {new_count} new movie(s) need metadata fetch.")
    if new_count > 0:
        log(f"Fetching metadata for {new_count} new movie(s) at ~50ms each "
            f"(~{round(new_count * 0.05)}s). Cached movies are instant.")

    log_stage("SCAN")
    log(f"Processing {total_movies} unique movie entries.")

    # Load IMDB ratings dataset (downloads if stale/missing, cached locally).
    # In simulation and Live runs this is required data; if it cannot be found,
    # downloaded, or loaded, ensure_imdb_ratings() logs ABORT and exits.
    # Note: IMDB ratings are looked up fresh from the TSV on every run —
    # only the tt ID is stored in the movie cache, not the rating value itself.
    # At 100% watch history with no rating cutoff, IMDb has no say at all, so the
    # dataset is skipped entirely (no check, no download, no per-movie lookups).
    if imdb_dataset_needed():
        imdb_ratings = ensure_imdb_ratings()
    else:
        imdb_ratings = {}
        log("IMDb dataset skipped: scoring is 100% watch history and no Max IMDb "
            "rating cutoff is set, so IMDb ratings cannot affect this run.")
    log(f"Retention scoring active — balance={SCORE_BALANCE:.0f} "
        f"(history {HISTORY_WEIGHT:.0%} / imdb {QUALITY_WEIGHT:.0%}) | "
        f"grace={GRACE_PERIOD_DAYS}d | "
        + (f"file size optimization on ({NEAR_TIE_PTS:g}-pt near-tie window): optimized picks "
           f"inside the tied group where the space target lands; same-movie copies lowest-quality first"
           if NEAR_TIE_PTS else
           "file size optimization off: strict score order throughout"))
    if SKIP_UNPLAYED_MOVIES:
        log("Unplayed filter active: movies with no play history are skipped.")
    if MAX_IMDB_RATING is not None:
        log(f"Rating cutoff active: movies rated above {float(MAX_IMDB_RATING):.1f} on IMDb are never deleted.")
    if PROTECT_JELLYFIN_FAVORITES:
        if USE_JELLYFIN:
            log("Jellyfin favorites protection active: favorited movies are never deleted.")
        else:
            # Configured but inert — say so instead of the reassuring line
            # above, which read as active protection in a Plex-only run.
            log("WARN: Protect Jellyfin favorites is enabled but Jellyfin is not selected — "
                "it protects nothing this run.")
    if JELLYFIN_PROTECTED_COLLECTIONS and not USE_JELLYFIN:
        log(f"WARN: Jellyfin protected collection(s) {sorted(JELLYFIN_PROTECTED_COLLECTIONS)} are "
            f"configured but Jellyfin is not selected — they protect nothing this run.")

    log_blank()

    total_movies = len(movies)
    emit_progress(phase="scanning", scanned=0, total=total_movies, eligible=0,
                  message="Scanning and scoring movies…")
    for movie_idx, item in enumerate(movies, 1):
        title = item.get("title", "UNKNOWN_TITLE")
        rating_key = item.get("rating_key")
        section_id = item.get("_section_id")

        file_path = extract_file_path(item)

        if not file_path:
            stats["no_file_path"] += 1
            log(f"SKIP no_file_path | title={title} | keys={list(item.keys())}")
            continue

        if file_path.suffix.lower() not in MOVIE_EXTENSIONS:
            stats["bad_extension"] += 1
            log(f"SKIP bad_extension | title={title} | path={file_path}")
            continue

        if not file_path.exists():
            stats["missing_on_disk"] += 1
            log(f"SKIP missing_on_disk | title={title} | translated_path={file_path}")
            continue

        # Symlinked media is out of scope: deleting the link would strand the
        # target and freed-space accounting would be wrong. Skip outright.
        if file_path.is_symlink():
            stats["symlink"] += 1
            log(f"SKIP symlink | title={title} | path={file_path}")
            continue

        outside_monitored = not is_under_monitored_dir(file_path)

        # Metadata: Plex/Tautulli rows fetch from Tautulli (cached); Jellyfin
        # rows carry their metadata inline. Protection and ids are the UNION of
        # both sources, so a movie protected on either server is protected.
        is_plex_row = bool(rating_key) and not str(rating_key).startswith("jf:")
        if is_plex_row:
            meta = fetch_movie_metadata(rating_key, title)
        else:
            meta = {"protected": False, "tmdb_id": None, "imdb_id": None}
        tmdb_id = meta["tmdb_id"] or item.get("_jf_tmdb_id")
        imdb_id = meta.get("imdb_id") or item.get("_jf_imdb_id")
        # Plex protection: when Plex was reachable this run, membership is
        # AUTHORITATIVE and independent of cache state. Match by resolved /library
        # PATH first (robust — the same thing deletion uses, immune to
        # Tautulli-vs-Plex rating_key/section-id differences), then by rating_key.
        # The path check also protects a Jellyfin-only row whose file happens to
        # sit in a Plex protected collection. Fall back to the cached/Tautulli flag
        # only when no Plex protected-collection scan is configured.
        resolved_path = str(file_path)
        if plex_protected_paths is not None:
            # bool(): the or-chain otherwise returns its last operand, so a
            # movie with tmdb_id=None leaked protected='' (empty string) into
            # the cache instead of False. Falsy either way, but the cache
            # should hold real booleans.
            plex_protected = bool(
                (resolved_path in plex_protected_paths) or (
                    is_plex_row and str(rating_key) in plex_protected_keys
                ) or (
                    _norm_id(imdb_id) and _norm_id(imdb_id) in plex_protected_imdb_ids
                ) or (
                    str(tmdb_id or "").strip() and str(tmdb_id or "").strip() in plex_protected_tmdb_ids
                )
            )
            if is_plex_row:
                if rating_key in _metadata_cache:
                    _metadata_cache[rating_key]["protected"] = plex_protected
                if rating_key in cached_movies:
                    cached_movies[rating_key]["protected"] = plex_protected
        else:
            plex_protected = bool(meta["protected"])
        jf_protected = bool(item.get("_jf_protected"))
        if USE_JELLYFIN and not jf_protected:
            item_match_keys = _match_keys(str(file_path))
            if item_match_keys & _JELLYFIN_PROTECTED_MATCH_KEYS:
                jf_protected = True
            elif _norm_id(imdb_id) and _norm_id(imdb_id) in _JELLYFIN_PROTECTED_IMDB_IDS:
                jf_protected = True
            elif str(tmdb_id or "").strip() and str(tmdb_id or "").strip() in _JELLYFIN_PROTECTED_TMDB_IDS:
                jf_protected = True
        protected = plex_protected or jf_protected

        if movie_idx % 100 == 0 or movie_idx == total_movies:
            _skipped = stats['no_file_path'] + stats['bad_extension'] + stats['missing_on_disk'] + stats['outside_monitored_dirs'] + stats['symlink']
            log(f"Progress: {movie_idx}/{total_movies} movies scanned | eligible={stats['eligible']} | protected={stats['protected']} | skipped={_skipped}")
            emit_progress(phase="scanning", scanned=movie_idx, total=total_movies,
                          eligible=stats['eligible'], protected=stats['protected'],
                          skipped=_skipped, message="Scanning and scoring movies…")

        # Register this path in the section1 map BEFORE the protection check.
        # A protected movie is still a surviving copy — deleting a different
        # copy shouldn't trigger Radarr cleanup if a protected one exists.
        if str(section_id) == str(RADARR_OVERSEERR_SECTION_ID) and tmdb_id:
            section1_paths_by_tmdb.setdefault(tmdb_id, set()).add(file_path.resolve())

        if outside_monitored:
            stats["outside_monitored_dirs"] += 1
            log(f"SKIP outside_monitored_dirs | title={title} | path={file_path}")
            continue

        if protected:
            stats["protected"] += 1
            why = []
            if plex_protected: why.append("plex")
            if jf_protected: why.append("jellyfin")
            log(f"SKIP protected_collection | title={title} | via={'+'.join(why) or 'cache'} | path={file_path}")
            continue

        # A Jellyfin row that shares folder+filename with a Plex movie but did
        # not merge has an unreconciled Plex↔Jellyfin identity. Never delete on
        # one: count it with the identity mismatches so the run completes with
        # errors and the summary names the file.
        twin = item.get("_unmerged_plex_twin")
        if twin:
            stats["identity_mismatch"] += 1
            identity_mismatches.append({
                "title":         f"{title} (Jellyfin) ↔ {twin.get('title')} (Plex, unmerged paths)",
                "path":          resolved_path,
                "plex_imdb":     "—",
                "jellyfin_imdb": _norm_id(imdb_id) or "—",
                "plex_tmdb":     "—",
                "jellyfin_tmdb": str(tmdb_id or "") or "—",
            })
            log(f"SKIP identity_mismatch (unmerged Plex twin) | jellyfin_title={title!r} | "
                f"plex_title={twin.get('title')!r} | path={file_path}")
            continue

        # ── Cross-server identity check (path-based) ──────────────────────────
        # Tautulli movie rows carry no file path at merge time, so Plex↔Jellyfin
        # reconciliation happens HERE, per row, once the real /library path is
        # resolved. When a file is present on BOTH servers but they disagree on
        # its provider IDs, its identity — and the IMDb rating we'd score it on —
        # can't be trusted, so we skip it and flag the run completed-with-errors.
        # A missing id on either side is NOT a conflict (can't compare). Plex rows
        # are processed before Jellyfin rows, so a conflicting Plex row flags the
        # shared path and its Jellyfin twin is skipped when reached.
        if USE_PLEX and USE_JELLYFIN:
            if is_plex_row:
                jf_id = None
                for k in _match_keys(resolved_path):
                    if k in _JELLYFIN_IDS_BY_MATCH_KEY:
                        jf_id = _JELLYFIN_IDS_BY_MATCH_KEY[k]
                        break
                if jf_id:
                    p_imdb, j_imdb = _norm_id(meta.get("imdb_id")), _norm_id(jf_id.get("imdb"))
                    p_tmdb, j_tmdb = _norm_id(meta.get("tmdb_id")), _norm_id(jf_id.get("tmdb"))
                    if (p_imdb and j_imdb and p_imdb != j_imdb) or (p_tmdb and j_tmdb and p_tmdb != j_tmdb):
                        stats["identity_mismatch"] += 1
                        _mismatch_skip_paths.add(resolved_path)
                        try:
                            _mismatch_skip_paths.add(str(file_path.resolve()))
                        except Exception:
                            pass
                        detail = {
                            "title":         f"{title} (Plex) ↔ {jf_id.get('title')} (Jellyfin)",
                            "path":          resolved_path,
                            "plex_imdb":     meta.get("imdb_id") or "—",
                            "jellyfin_imdb": jf_id.get("imdb") or "—",
                            "plex_tmdb":     meta.get("tmdb_id") or "—",
                            "jellyfin_tmdb": jf_id.get("tmdb") or "—",
                        }
                        identity_mismatches.append(detail)
                        log(f"SKIP identity_mismatch | plex_title={title!r} != jellyfin_title={jf_id.get('title')!r} | "
                            f"plex(imdb={detail['plex_imdb']},tmdb={detail['plex_tmdb']}) != "
                            f"jellyfin(imdb={detail['jellyfin_imdb']},tmdb={detail['jellyfin_tmdb']}) | "
                            f"path={file_path}")
                        continue
            else:
                # Jellyfin row whose Plex twin already flagged this exact file as a
                # mismatch — skip the duplicate without double-counting.
                _resolved2 = None
                try:
                    _resolved2 = str(file_path.resolve())
                except Exception:
                    pass
                if resolved_path in _mismatch_skip_paths or (_resolved2 and _resolved2 in _mismatch_skip_paths):
                    log(f"SKIP identity_mismatch (Jellyfin twin) | jellyfin_title={title!r} | path={file_path}")
                    continue

        # Jellyfin favorites — optional HARD protection override, off by default.
        # Never a scoring signal: favorites are not cleanly shared between
        # Plex/Tautulli and Jellyfin.
        if PROTECT_JELLYFIN_FAVORITES and item.get("_jf_favorite"):
            stats["jellyfin_favorite"] += 1
            log(f"SKIP jellyfin_favorite | title={title} | path={file_path}")
            continue

        # IMDB lookup — entry is (rating, votes) tuple or None.
        imdb_entry  = imdb_ratings.get(imdb_id) if imdb_id else None
        imdb_rating = imdb_entry[0] if imdb_entry else None
        imdb_votes  = imdb_entry[1] if imdb_entry else None

        # No IMDb rating/votes found (unresolved id, or absent from the
        # dataset): half the scoring evidence is missing, so the movie is
        # excluded outright — not enough data to make a deletion decision. Only
        # applies when IMDb is actually in use; at 100% watch history with no
        # cutoff, a missing rating is irrelevant and the movie stays eligible.
        if imdb_dataset_needed() and (imdb_rating is None or not imdb_votes):
            stats["no_imdb_data"] += 1
            log(f"SKIP no_imdb_data | title={title} | imdb_id={imdb_id or '(unresolved)'} | path={file_path}")
            continue

        # Optional rating cutoff (MAX_IMDB_RATING): movies rated ABOVE the
        # cutoff are protected outright (hard rule, not a score).
        if MAX_IMDB_RATING is not None and float(imdb_rating) > float(MAX_IMDB_RATING):
            stats["high_rated"] += 1
            log(f"SKIP high_rated | title={title} | imdb={imdb_rating} > cutoff {float(MAX_IMDB_RATING):.1f}")
            continue

        added_at = parse_int(item.get("added_at"), 0)

        # Grace period: movies added within GRACE_PERIOD_DAYS are excluded
        # outright (hard rule, not a score).
        if GRACE_PERIOD_DAYS and added_at > 0 and (time.time() - added_at) < GRACE_PERIOD_DAYS * 86400:
            stats["recently_added"] += 1
            log(f"SKIP recently_added | title={title} | added={format_epoch(added_at)} | grace={GRACE_PERIOD_DAYS}d")
            continue

        play_count = parse_int(item.get("play_count"), 0)
        last_played = parse_int(item.get("last_played"), 0)
        if SKIP_UNPLAYED_MOVIES and play_count <= 0 and last_played <= 0:
            stats["unplayed"] += 1
            log(f"SKIP unplayed | title={title} | plays={play_count} | last_played=never")
            continue

        try:
            file_size = file_path.stat().st_size
        except FileNotFoundError:
            stats["missing_on_disk"] += 1
            log(f"SKIP disappeared_during_scan | title={title} | path={file_path}")
            continue

        # Gather every retention-score input on the normalized record now;
        # scoring runs after path-dedup so merged play stats are final.
        release_year = parse_int(item.get("year"), 0)

        # Distinct users: the higher of the Plex and Jellyfin counts, never the
        # sum (a movie on both servers takes whichever saw more watchers).
        distinct_users = _distinct_users_for_row(item)

        stats["eligible"] += 1

        candidates.append({
            "path": file_path,
            "title": title,
            "rating_key": rating_key,
            "section_id": section_id,
            "source": "plex" if is_plex_row else "jellyfin",
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
            "imdb_rating": imdb_rating,
            "imdb_votes": imdb_votes,
            "release_year": release_year,
            "play_count": play_count,
            "last_played": last_played,
            "added_at": added_at,
            "distinct_users": distinct_users,
            "resolution": item.get("video_resolution"),
            "bitrate": parse_int(item.get("bitrate"), 0),
            "file_size": file_size,
        })

    # Second-pass dedup on resolved file path (catches cases where the raw file
    # key dedup above couldn't match, e.g. rating_key fallback with different keys)
    path_seen: dict = {}
    deduped: list = []

    for c in candidates:
        key = c["path"].resolve()
        if key not in path_seen:
            path_seen[key] = len(deduped)
            deduped.append(c)
        else:
            existing = deduped[path_seen[key]]
            # Cross-server rows describe DIFFERENT plays of the same file, so
            # plays SUM (the documented merge design). Rows from a server whose
            # plays are already folded in (same-server duplicates, e.g. one
            # file listed in two sections) describe the SAME plays, so max()
            # avoids double-counting them.
            merged_sources = existing.setdefault("_merged_sources", {existing.get("source")})
            if c.get("source") not in merged_sources:
                merged_plays = parse_int(existing.get("play_count"), 0) + parse_int(c.get("play_count"), 0)
                merged_sources.add(c.get("source"))
            else:
                merged_plays = max(existing["play_count"], c["play_count"])
            merged_lp = max(existing["last_played"], c["last_played"])
            if merged_plays != existing["play_count"] or merged_lp != existing["last_played"]:
                existing["play_count"] = merged_plays
                existing["last_played"] = merged_lp
                log(
                    f"DEDUP merged duplicate path | title={existing['title']} | "
                    f"merged_plays={merged_plays} | merged_last_played={format_epoch(merged_lp)}"
                )
            # Take the best retention-score inputs from either server's row —
            # rescoring happens after this dedup pass, so merges are cheap.
            existing["distinct_users"] = max(parse_int(existing.get("distinct_users"), 0),
                                             parse_int(c.get("distinct_users"), 0))
            for field in ("resolution",):
                if not existing.get(field) and c.get(field):
                    existing[field] = c[field]
            if parse_int(c.get("bitrate"), 0) > parse_int(existing.get("bitrate"), 0):
                existing["bitrate"] = c["bitrate"]
            # Preserve the Radarr section_id and TMDB ID across merges
            if str(c.get("section_id")) == str(RADARR_OVERSEERR_SECTION_ID):
                existing["section_id"] = RADARR_OVERSEERR_SECTION_ID
            if c.get("tmdb_id") and not existing.get("tmdb_id"):
                existing["tmdb_id"] = c["tmdb_id"]
            if c.get("imdb_id") and not existing.get("imdb_id"):
                existing["imdb_id"] = c["imdb_id"]
                if existing.get("imdb_rating") is None and c.get("imdb_rating") is not None:
                    existing["imdb_rating"] = c["imdb_rating"]
                    existing["imdb_votes"] = c.get("imdb_votes")

    duplicates_removed = len(candidates) - len(deduped)
    if duplicates_removed:
        log(f"Path dedup removed {duplicates_removed} duplicate candidate(s).")

    candidates = deduped
    stats["duplicates_merged"] = duplicates_removed

    # ── Persist updated metadata cache ───────────────────────────────────────
    # Write any newly fetched entries back to the JSON cache so the next run
    # can skip the API calls for these movies.
    for rk, meta in _metadata_cache.items():
        cached_movies[str(rk)] = {
            "protected": meta.get("protected", False),
            "tmdb_id":   meta.get("tmdb_id"),
            "imdb_id":   meta.get("imdb_id"),
            "v": 2,
        }
    cache["movies"] = cached_movies
    save_cache(cache)
    log(f"Cache saved: {len(cached_movies)} movie entries -> {CACHE_FILE}")
    # ─────────────────────────────────────────────────────────────────────────

    log_stage("ELIGIBLE CANDIDATES")
    log(
        "Candidate stats: "
        f"eligible={stats['eligible']} | "
        f"protected={stats['protected']} | "
        f"identity_mismatch={stats['identity_mismatch']} | "
        f"symlink={stats['symlink']} | "
        f"jellyfin_favorite={stats['jellyfin_favorite']} | "
        f"recently_added={stats['recently_added']} | "
        f"unplayed={stats['unplayed']} | "
        f"no_imdb_data={stats['no_imdb_data']} | "
        f"duplicates_merged={duplicates_removed} | "
        f"no_file_path={stats['no_file_path']} | "
        f"bad_extension={stats['bad_extension']} | "
        f"missing_on_disk={stats['missing_on_disk']} | "
        f"outside_monitored_dirs={stats['outside_monitored_dirs']}"
    )

    score_and_rank_candidates(candidates)

    stats["identity_mismatch_details"] = identity_mismatches
    return candidates, section1_paths_by_tmdb, stats, total_movies


def score_and_rank_candidates(candidates):
    """Score every candidate and sort the list into deletion order, in place —
    the one place deletion ordering is defined."""
    _score_now = time.time()
    for c in candidates:
        score_candidate(c, now=_score_now)

    # DeletionPriority: RetentionScore ASCENDING — strict score order (the
    # near-tie window only reorders at deletion time, in _pop_next_deletion).
    def _resolution_height(value):
        """Vertical resolution as an integer for quality comparison; unknown
        values rank lowest (0) and defer to the bitrate/size fallbacks."""
        s = str(value or "").strip().lower().rstrip("pi")
        if not s:
            return 0
        aliases = {"4k": 2160, "8k": 4320, "uhd": 2160, "hd": 720, "sd": 480}
        if s in aliases:
            return aliases[s]
        try:
            return max(0, int(float(s)))
        except (TypeError, ValueError):
            return 0

    def _duplicate_copy_rank(c):
        """Delete-first order among near-tied same-movie copies: lowest
        quality first (resolution → bitrate → file size), then the copy
        outside the Radarr-monitored section, then oldest added, then path."""
        in_radarr_section = 1 if (
            RADARR_OVERSEERR_SECTION_ID
            and str(c.get("section_id")) == str(RADARR_OVERSEERR_SECTION_ID)
        ) else 0
        return (
            _resolution_height(c.get("resolution")),
            parse_int(c.get("bitrate"), 0),
            parse_int(c.get("file_size"), 0),
            in_radarr_section,
            parse_int(c.get("added_at"), 0),
            str(c.get("path")),
        )

    # The IMDb-rating tiebreak only applies when IMDb has a say (the dial gives
    # it weight, or a cutoff is set). At 100% watch history it is disabled so a
    # score tie — the large unwatched-and-stale pile — orders purely oldest to
    # newest, with IMDb having zero influence anywhere in the run.
    _imdb_tiebreak = imdb_dataset_needed()

    def _deletion_sort_key(c):
        """RetentionScore ascending; exact ties break never-watched first, then
        (only when IMDb is in use) lowest IMDb rating first — when the score
        can't separate two movies, shed the weaker-rated one — then oldest added
        (added_at 0 = unknown, treated as oldest), larger files first, title."""
        watched = parse_int(c.get("play_count"), 0) > 0 or parse_int(c.get("last_played"), 0) > 0
        if _imdb_tiebreak:
            rating = c.get("imdb_rating")
            # Missing rating ranks highest so it is kept — never delete on absent data.
            rating_key = float(rating) if rating is not None else 10.0
        else:
            rating_key = 0.0  # IMDb disabled: constant leaves ordering to added_at
        return (
            c["retention_score"],
            1 if watched else 0,
            rating_key,
            parse_int(c.get("added_at"), 0),
            -parse_int(c.get("file_size"), 0),
            str(c.get("title") or ""),
        )

    candidates.sort(key=_deletion_sort_key)

    # ── Same-movie duplicate preference ──────────────────────────────────────
    # Copies of the SAME movie (same TMDB, or IMDb when TMDB is missing) with
    # near-tied scores swap among their own slots so the lowest-quality copy
    # deletes first (_duplicate_copy_rank); every other candidate keeps its
    # position, and a genuinely better-scoring copy still outlives its twin.
    dup_groups: dict = {}
    for i, c in enumerate(candidates):
        movie_key = str(c.get("tmdb_id") or "").strip() or ("imdb:" + str(c.get("imdb_id") or "").strip())
        if movie_key in ("", "imdb:"):
            continue
        dup_groups.setdefault(movie_key, []).append(i)
    for movie_key, all_slots in dup_groups.items():
        if len(all_slots) < 2:
            continue
        # Slots are in score order; copies join the reorder only while they
        # stay within the window of the lowest-scored (first) copy.
        window = NEAR_TIE_PTS or 0.0
        base_score = candidates[all_slots[0]]["retention_score"]
        slots = [i for i in all_slots
                 if candidates[i]["retention_score"] - base_score <= window]
        if len(slots) < 2:
            continue
        members = sorted((candidates[i] for i in slots), key=_duplicate_copy_rank)
        for slot, member in zip(slots, members):
            candidates[slot] = member
        log(
            f"Duplicate copies reordered (lowest quality deletes first) | "
            f"movie={members[0].get('title')} | copies={len(slots)} | "
            f"order={[(str(m.get('resolution') or '?') + ' ' + format(m.get('file_size', 0)/1e9, '.1f') + 'GB') for m in members]}"
        )

    if candidates:
        log(f"All {len(candidates)} candidates sorted by deletion priority (lowest RetentionScore first):")
        for i, c in enumerate(candidates, 1):
            log(
                f"  #{i} title={c['title']} | "
                f"retention={c['retention_score']:.1f} | "
                f"imdb={c['imdb_rating']} | votes={c['imdb_votes']} | year={c['release_year']} | "
                f"plays={c['play_count']} | users={c.get('distinct_users', 0)} | "
                f"last_played={format_epoch(c['last_played'])} | "
                f"size={bytes_to_gb(c['file_size']):.2f} GB | "
                f"added={format_epoch(c['added_at'])} | "
                f"path={c['path']}"
            )


# =========================
# DELETION
# =========================

def remove_empty_movie_folder(file_path):
    movie_dir = file_path.parent
    try:
        resolved_dir = movie_dir.resolve()
    except (OSError, RuntimeError):
        return
    # Never remove the library root itself or any monitored root folder.
    if resolved_dir == LIBRARY_ROOT.resolve():
        return
    for root in monitored_roots():
        if resolved_dir == root.resolve():
            return
    try:
        if movie_dir.exists() and not any(movie_dir.iterdir()):
            if RUN_MODE.startswith("debug_"):
                log(f"DRY RUN: Would remove empty directory: {movie_dir}")
            else:
                movie_dir.rmdir()
                log(f"Removed empty directory: {movie_dir}")
    except OSError as e:
        # A file can appear (or the dir vanish) between the emptiness check and
        # rmdir. Folder tidying is best-effort — it must never abort a cleanup
        # run mid-loop, which would skip the summary and log archive.
        log(f"WARNING: Could not remove directory {movie_dir}: {e}")


def delete_candidate(candidate, section1_paths_by_tmdb):
    path = candidate["path"]

    log(
        f"Selected for deletion: {candidate['title']} | "
        f"score={round(candidate['retention_score'], 3)} | "
        f"imdb={candidate['imdb_rating']} | votes={candidate['imdb_votes']} | year={candidate['release_year']} | "
        f"plays={candidate['play_count']} | "
        f"last_played={format_epoch(candidate['last_played'])} | "
        f"size={bytes_to_gb(candidate['file_size']):.2f} GB | "
        f"added={format_epoch(candidate['added_at'])} | "
        f"path={path}"
    )

    if not is_safe_to_delete(path):
        log(f"ABORT safety_check_failed: path is not under a known safe prefix | path={path}")
        return

    if RUN_MODE.startswith("debug_"):
        log(f"DRY RUN: Would delete file: {path}")
        remove_empty_movie_folder(path)
        return

    delete_size = candidate.get("file_size")
    try:
        delete_size = int(delete_size)
    except (TypeError, ValueError):
        delete_size = None
    if delete_size is None or delete_size < 0:
        try:
            delete_size = path.stat().st_size
        except OSError:
            delete_size = None

    # Critical section: a Stop (SIGTERM) arriving between unlink() and the
    # deleted.log append is deferred until the record is written — otherwise
    # the stopped run's last deletion would be missing from the history.
    global _IN_DELETE_CRITICAL, _RUN_DELETED_FILES
    _IN_DELETE_CRITICAL = True
    try:
        try:
            path.unlink()
        except FileNotFoundError:
            log(f"WARN: file already gone when attempting deletion (skipping): {path}")
            return
        except OSError as e:
            log(f"ERROR: could not delete {path}: {e}")
            return
        _RUN_DELETED_FILES = True
        log(f"Deleted file: {path}")
        log_deleted(candidate["title"], path, delete_size,
                    score=candidate.get("retention_score"),
                    plays=candidate.get("play_count"),
                    last_played=candidate.get("last_played"))
    finally:
        _IN_DELETE_CRITICAL = False
        if _SIGTERM_DEFERRED:
            raise SystemExit(143)
    remove_empty_movie_folder(path)

    # Radarr cleanup — only in live mode, only for the section
    # defined by RADARR_OVERSEERR_SECTION_ID, and only when this was the last
    # surviving copy of this TMDB ID in that section.
    cleanup_radarr(candidate, section1_paths_by_tmdb)


# =========================
# MAIN
# =========================

def _revalidate_pending_marks(limits_breached: bool) -> None:
    """15-minute upkeep of the marked-for-deletion queue, run inside the quiet
    Summary: drop marks whose files are gone or that joined a protected
    collection, and clear the whole queue once space limits are satisfied.
    Display upkeep only — an actual deletion always re-derives eligibility
    from a full scan, so a stale mark can never delete a protected movie.
    Favorites and filter-rule changes reconcile on the next daily run."""
    store = load_pending()
    if not store:
        return
    if not limits_breached:
        log(f"Space limits satisfied — clearing {len(store)} marked-for-deletion entrie(s).")
        save_pending({})
        return
    changed = False
    for key in list(store):
        try:
            missing = not Path(key).exists()
        except OSError:
            missing = False
        if missing:
            log(f"Unmarked (file gone): {store[key].get('title') or key}")
            store.pop(key)
            changed = True
    try:
        plex_paths, _k, _i, _t = fetch_protected_paths()
        _jids, jf_paths, _ji, _jt = _jellyfin_protected_items()
        protected = {str(p) for p in set(plex_paths) | set(jf_paths)}
        for key in list(store):
            if key in protected:
                log(f"Unmarked (protected now): {store[key].get('title') or key}")
                store.pop(key)
                changed = True
    except (SystemExit, Exception) as e:
        # Can't verify protection right now — keep the marks (harmless: marks
        # never authorize a deletion on their own).
        log(f"Mark upkeep: protection could not be verified ({e}); keeping marks.")
    if changed:
        save_pending(store)


def _space_threshold_errors(usage_info=None, *, enforce_headroom_safety=True):
    """Return blocking Space Thresholds config errors.

    debug_sim may deliberately exceed the headroom safety cap to preview what
    would be targeted. Live enforces the cap and aborts before deleting.
    """
    errors = list(dict.fromkeys(CONFIG_ERRORS))

    def _is_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    if not _is_number(HEADROOM_GB) or HEADROOM_GB < 0:
        errors.append("HEADROOM_GB must be zero or greater.")

    if not _is_number(MAX_HEADROOM_PCT) or MAX_HEADROOM_PCT <= 0:
        errors.append("MAX_HEADROOM_PCT must be greater than zero.")

    if REDLINE_GB is not None:
        if not _is_number(REDLINE_GB) or REDLINE_GB < 0:
            errors.append("REDLINE_GB must be zero or greater, or None to disable it.")
        elif _is_number(HEADROOM_GB) and REDLINE_GB > HEADROOM_GB:
            errors.append(f"REDLINE_GB ({REDLINE_GB}) cannot be higher than HEADROOM_GB ({HEADROOM_GB}).")

    if MAX_LIBRARY_GB is not None:
        if not _is_number(MAX_LIBRARY_GB) or MAX_LIBRARY_GB <= 0:
            errors.append("MAX_LIBRARY_GB must be greater than zero, or None to disable it.")

    _total_gb = None
    _max_headroom_gb = None
    if usage_info and _is_number(HEADROOM_GB) and _is_number(MAX_HEADROOM_PCT) and MAX_HEADROOM_PCT > 0:
        try:
            _total_gb = bytes_to_gb(usage_info["total"])
            _max_headroom_gb = round(_total_gb * MAX_HEADROOM_PCT / 100, 1)
            if enforce_headroom_safety and HEADROOM_GB > _max_headroom_gb:
                errors.append(
                    f"HEADROOM_GB={HEADROOM_GB} GB exceeds the safety cap of "
                    f"{MAX_HEADROOM_PCT}% of total filesystem capacity "
                    f"({_total_gb:.1f} GB × {MAX_HEADROOM_PCT}% = {_max_headroom_gb:.1f} GB). "
                    f"Lower HEADROOM_GB or — with caution — raise MAX_HEADROOM_PCT."
                )
        except Exception:
            pass

    return list(dict.fromkeys(errors)), _total_gb, _max_headroom_gb


def log_identity_mismatches(build_stats):
    """Print the Plex/Jellyfin identity-mismatch block. Called immediately before
    the run summary so skipped files are visible right where the outcome is read.
    No-op when there were no mismatches."""
    details = build_stats.get("identity_mismatch_details") or []
    if not details:
        return
    log_blank()
    log("!" * 55)
    log(f"  COMPLETED WITH ERRORS — {len(details)} file(s) skipped (identity mismatch)")
    log("!" * 55)
    log("  These files sit at the same path but Plex and Jellyfin identify them")
    log("  as different movies, so their rating can't be trusted. They were NOT")
    log("  deleted. Re-identify the movie on whichever server is wrong, then re-run.")
    for d in details:
        log(f"  • {d['title']}")
        log(f"      path:     {d['path']}")
        log(f"      Plex:     imdb={d['plex_imdb']}  tmdb={d['plex_tmdb']}")
        log(f"      Jellyfin: imdb={d['jellyfin_imdb']}  tmdb={d['jellyfin_tmdb']}")
    log("!" * 55)


def log_run_summary(*, is_sim, trigger, to_free_gb, used_gb, free_before_gb,
                    final_gb, final_free_gb, freed_bytes, removed_count,
                    skipped_under_limit, effective_library_gb, max_gb,
                    build_stats, total_scanned, library_cap_hit=False):
    """Write the end-of-run summary block for both Simulate and Live runs.

    One implementation, two label sets, so the two modes can never drift apart:
    Simulate uses "(est.)" wording, a wider label column, a projected
    library-after line, and the not-enough-candidates warnings; Live adds the
    "Target freed" line.
    """
    log_identity_mismatches(build_stats)
    pad = 20 if is_sim else 18
    def row(label, value):
        log(f"  {label:<{pad}}{value}")

    est = " (est.)" if is_sim else ""
    log_blank()
    log("=" * 55)
    log(f"  {'DRY RUN' if is_sim else 'CLEANUP'} SUMMARY  [{trigger.upper()}]")
    log("=" * 55)
    row("Trigger:", trigger)
    if not is_sim:
        row("Target freed:", f"{to_free_gb:.1f} GB")
    row("Disk before:", f"{used_gb:.1f} GB used  |  {free_before_gb:.1f} GB free")
    row(f"Disk after{est}:", f"{final_gb:.1f} GB used  |  {final_free_gb:.1f} GB free")
    row(f"Space freed{est}:", f"{bytes_to_gb(freed_bytes):.2f} GB")
    row("Headroom limit:", f"{max_gb:.1f} GB ({HEADROOM_GB} GB)")
    if effective_library_gb is not None:
        row("Library before:", f"{effective_library_gb:.1f} GB{(' | cap: ' + str(MAX_LIBRARY_GB) + ' GB') if MAX_LIBRARY_GB else ''}")
        if is_sim:
            log(f"  Library after (est.): {effective_library_gb - bytes_to_gb(freed_bytes):.1f} GB")
    log("-" * 55)
    path_issues = (build_stats["no_file_path"] + build_stats["bad_extension"]
                   + build_stats["missing_on_disk"] + build_stats["outside_monitored_dirs"])
    row("Movies scanned:", total_scanned)
    row("Protected:", f"{build_stats['protected']}  (in Protected collection)")
    if build_stats.get("jellyfin_favorite"):
        row("JF favorites:", f"{build_stats['jellyfin_favorite']}  (favorited by a Jellyfin user — protected)")
    if build_stats.get("identity_mismatch"):
        row("Identity mismatch:", f"{build_stats['identity_mismatch']}  (Plex/Jellyfin disagree — skipped, not deleted)")
    row("Recently added:", f"{build_stats['recently_added']}  (added within {GRACE_PERIOD_DAYS}-day grace period)")
    if SKIP_UNPLAYED_MOVIES or build_stats.get("unplayed"):
        row("Unplayed:", f"{build_stats.get('unplayed', 0)}  (no play history)")
    if MAX_IMDB_RATING is not None or build_stats.get("high_rated"):
        row("High-rated:", f"{build_stats.get('high_rated', 0)}  (IMDb above {float(MAX_IMDB_RATING):.1f} cutoff — protected)" if MAX_IMDB_RATING is not None else f"{build_stats.get('high_rated', 0)}")
    if build_stats.get("no_imdb_data"):
        row("No IMDb data:", f"{build_stats.get('no_imdb_data', 0)}  (no rating/votes found — not enough data to judge, skipped)")
    row("Path/disk issues:", f"{path_issues}  (missing, bad extension, unmapped path)")
    row("Duplicates merged:", build_stats.get('duplicates_merged', 0))
    row("Eligible:", build_stats['eligible'])
    log("-" * 55)
    row("Would delete:" if is_sim else "Deleted:", removed_count)
    # The "limit reached" note only applies when candidates were actually left
    # untouched because the target was met first. When every candidate is acted
    # on (e.g. the cap is far below the library and can't be reached), the count
    # is 0 and the note would falsely imply the limit was hit.
    row("Not needed:", f"{skipped_under_limit}  (limit reached before exhausting candidates)"
        if skipped_under_limit > 0 else "0")
    log("=" * 55)

    if is_sim:
        if final_gb >= max_gb:
            log(
                f"  WARNING: Even deleting all eligible candidates only reaches "
                f"{final_gb:.1f} GB, still above headroom limit of {max_gb:.1f} GB."
            )
        if library_cap_hit and effective_library_gb is not None:
            _lib_after = effective_library_gb - bytes_to_gb(freed_bytes)
            if _lib_after > MAX_LIBRARY_GB:
                log(
                    f"  WARNING: Even deleting all eligible candidates only reduces "
                    f"library to {_lib_after:.1f} GB, still above cap of {MAX_LIBRARY_GB} GB."
                )


def main():
    # Load saved config from JSON file (Docker / web UI mode).
    _load_config_from_file()
    # Allow the web UI to trigger a specific run mode without changing the saved config.
    if _MODE_OVERRIDE:
        global RUN_MODE
        RUN_MODE = _MODE_OVERRIDE

    global LOGFILE, _QUIET_PROGRESS
    if RUN_MODE in ("debug_info", "sample_pool"):
        # Quiet background refreshes (storage summary, Score Explorer sample):
        # no progress events, and the log is discarded — lastrun.log stays the
        # last real run's log and no scratch log files accumulate. Failures
        # still surface through the subprocess exit code and the UI messages.
        LOGFILE = Path(_os.devnull)
        _QUIET_PROGRESS = True

    run_start = time.strftime("%Y-%m-%d_%H-%M-%S")
    reset_log()

    # Archive this run's log exactly once at process exit — completed, failed,
    # or (opt-in) interrupted. Registered here, in the engine's main thread, so
    # SIGTERM from a web-app "Stop" unwinds cleanly and still archives.
    global _RUN_START, _RUN_ARCHIVABLE
    _RUN_START = run_start
    _RUN_ARCHIVABLE = RUN_MODE in ARCHIVABLE_RUN_MODES
    if _RUN_ARCHIVABLE:
        try:
            signal.signal(signal.SIGTERM, _handle_sigterm)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported — atexit still covers normal exits
        atexit.register(_finalize_run)

    debug_startup()

    # Quick sample-pool rebuild: API-only, writes the cache's sample pool and exits.
    # Handled before the executable-mode gate — it is web-app-triggered only
    # (MEDIAREDUCER_MODE_OVERRIDE) and never scans disks or deletes anything.
    if RUN_MODE == "sample_pool":
        build_quick_sample_pool(parse_int(_SAMPLE_TARGET, 0))
        return

    # Safety gate: only the recognized run modes may proceed. Any other value —
    # including the Docker default "paused", a blank string, or a typo — must NOT
    # fall through to the live-deletion path below. This guarantees that a direct
    # or cron invocation with RUN_MODE not set to an executable mode is a no-op,
    # mirroring the scheduler's own paused check in app.py.
    if RUN_MODE not in EXECUTABLE_RUN_MODES:
        log(f"RUN_MODE={RUN_MODE!r}: paused / not an executable mode — no scan or cleanup performed.")
        emit_progress(schema=1, status="done", phase="done", mode=RUN_MODE,
                      scanned=0, total=0, eligible=0, deleted=0, bytes_freed=0,
                      target_bytes=0, trigger="", current_title="",
                      message="Paused — no scan or cleanup performed.",
                      started_at=time.time())
        return

    emit_progress(schema=1, status="running", phase="checking", mode=RUN_MODE,
                  scanned=0, total=0, eligible=0, protected=0, skipped=0,
                  deleted=0, bytes_freed=0, target_bytes=0, trigger="",
                  current_title="", message="Checking connections…",
                  completed_with_errors=False,
                  started_at=time.time())

    if not validate_connections():
        emit_progress(status="error", phase="checking",
                      message=("Connection check failed: " + _CONNECTION_VALIDATION_ERRORS[0]
                               + " Fix it in Configuration → Connections.")
                      if _CONNECTION_VALIDATION_ERRORS
                      else "Connection check failed — see the log for details.")
        return

    verify_runtime_api_health()

    _is_sim   = RUN_MODE == "debug_sim"
    _is_info  = RUN_MODE == "debug_info"
    _cap_active = RUN_MODE in ("debug_sim", "headroom") and MAX_LIBRARY_GB is not None

    usage_info = get_usage_info()
    used_gb = usage_info["used_gb"]
    max_gb = usage_info["max_gb"]
    free_gb = round(bytes_to_gb(usage_info["free"]), 1)

    # Validate Space Thresholds. Summary/debug_info is allowed to continue so it
    # can show readiness errors; simulation and Live abort because scoring
    # and deletion decisions depend on these thresholds being sane.
    _threshold_errors, _total_gb, _max_headroom_gb = _space_threshold_errors(
        usage_info, enforce_headroom_safety=not _is_sim
    )
    if (
        _is_sim
        and _max_headroom_gb is not None
        and isinstance(HEADROOM_GB, (int, float))
        and HEADROOM_GB > _max_headroom_gb
    ):
        log(
            f"WARNING: HEADROOM_GB={HEADROOM_GB} GB exceeds the safety cap of "
            f"{MAX_HEADROOM_PCT}% of total filesystem capacity "
            f"({_total_gb:.1f} GB × {MAX_HEADROOM_PCT}% = {_max_headroom_gb:.1f} GB). "
            f"Lower HEADROOM_GB or — with caution — raise MAX_HEADROOM_PCT."
        )
        log("WARNING: RUN_MODE='debug_sim' — simulation will continue, but Live")
        log("WARNING: would REFUSE TO RUN until HEADROOM_GB is corrected.")
        log_blank()

    if _threshold_errors:
        for _err in _threshold_errors:
            log(f"CONFIG ERROR: {_err}")
        if not _is_info:
            log("ABORT: Fix Space Thresholds before running simulation or live cleanup.")
            # Terminal progress emit: the engine exits 0 here, so without this
            # the dashboard's progress panel would stay on "running" forever.
            emit_progress(status="error", phase="checking",
                          message="Space Thresholds are invalid — fix them in Configuration, "
                                  "then run again. See the detailed log for the exact errors.")
            return
        log("WARNING: Summary mode will continue, but Simulate and Live are blocked until Space Thresholds are fixed.")
        log_blank()

    # IMDb ratings are required for scoring, so resolve the dataset among the
    # FIRST checks: a missing file with a broken download aborts here — within
    # the download timeout — instead of after the whole library fetch, and the
    # dashboard's manual-setup popup appears right away. Skipped when the run
    # cannot use IMDb (100% watch history, no rating cutoff) so it neither
    # checks nor downloads the dataset.
    if not _is_info and imdb_dataset_needed():
        emit_progress(phase="checking", message="Checking the IMDb ratings dataset…")
        ensure_imdb_ratings()

    if _total_gb is None:
        _total_gb = bytes_to_gb(usage_info["total"])
    if _max_headroom_gb is None:
        try:
            _max_headroom_gb = round(_total_gb * MAX_HEADROOM_PCT / 100, 1)
        except Exception:
            _max_headroom_gb = 0

    # Library size — read directly from disk so it reflects deletions
    # immediately. Tautulli's cached media-info size can lag reality for a long
    # time after a file is removed, which would keep the cap triggering after
    # space was already freed. This needs neither Plex nor Tautulli reachable.
    emit_progress(phase="library", message="Reading library size…")
    # In Summary/debug_info, still surface the Tautulli movie section IDs (via
    # get_movie_section_ids, which logs them) so the user can see them — e.g. to
    # set the Radarr section ID — without starting a scan. Guarded so an
    # unreachable Tautulli never aborts the size read below.
    if _is_info:
        try:
            get_movie_section_ids()
        except Exception as e:
            log(f"Movie section IDs unavailable (Tautulli query failed): {e}")
    log("Computing library size from disk...")
    library_gb = get_library_size_gb()
    if library_gb is not None:
        if MAX_LIBRARY_GB is not None:
            delta = library_gb - MAX_LIBRARY_GB
            status = f"OVER cap by {delta:.1f} GB" if delta > 0 else f"under cap by {abs(delta):.1f} GB"
            log(f"Library size: {library_gb:.1f} GB | cap: {MAX_LIBRARY_GB} GB | {status}")
        else:
            log(f"Library size: {library_gb:.1f} GB | cap: disabled")
    else:
        log("Library size: unavailable (disk read failed)")

    # Refresh dashboard stats (runs in every mode, including the quiet Summary).
    # library_gb is only written when known, so a disk-read failure keeps the
    # last good value instead of blanking the dashboard.
    total_gb = round(bytes_to_gb(usage_info["total"]), 1)
    _stats = {
        "library_cap_gb": MAX_LIBRARY_GB,
        "disk": {
            "used_gb": used_gb,
            "total_gb": total_gb,
            "free_gb": free_gb,
            "pct_used": round(used_gb / total_gb * 100, 1) if total_gb else 0,
        },
    }
    if library_gb is not None:
        _stats["library_gb"] = round(library_gb, 1)
    emit_stats(**_stats)

    over_limit = used_gb >= max_gb
    redline_hit = REDLINE_GB is not None and free_gb <= REDLINE_GB
    library_cap_hit = (_cap_active and MAX_LIBRARY_GB is not None
                       and library_gb is not None and library_gb > MAX_LIBRARY_GB)
    # Only Redline is an emergency trigger. The Library Size Cap shares the
    # headroom's once-per-day window (and the deletion delay).
    immediate_trigger = redline_hit
    # Dashboard Live Run button: prune every breached target now — the delay
    # and the daily window pace automatic runs, not a deliberate button press.
    _manual_live = _MANUAL_RUN and not _is_sim and not _is_info

    # ── Info mode: show status and exit ─────────────────────────────────────
    if _is_info:
        # Summary should warn when the library is currently over the configured cap,
        # even though no deletion trigger is active in info mode.
        _lib_over = (library_gb is not None and MAX_LIBRARY_GB is not None
                     and library_gb > MAX_LIBRARY_GB)

        log_blank()
        log("=" * 55)
        log("  STATUS (info mode — no scan performed)")
        log("=" * 55)
        log(f"  Filesystem:   {used_gb:.1f} GB used / {_total_gb:.1f} GB total")
        log(f"  Headroom:     limit {max_gb:.1f} GB  |  {'OVER by ' + str(round(used_gb - max_gb, 1)) + ' GB' if over_limit else 'OK (' + str(free_gb) + ' GB free)'}")
        log(f"  Redline:      {REDLINE_GB} GB  |  {'HIT — only ' + str(free_gb) + ' GB free' if redline_hit else 'OK'}")
        log("  IMDb ratings: skipped in summary mode")
        if library_gb is not None:
            if MAX_LIBRARY_GB is None:
                lib_status = "cap disabled"
            elif _lib_over:
                _mode_note = "active" if _cap_active else "enable Library Size Cap to activate"
                lib_status = f"OVER by {library_gb - MAX_LIBRARY_GB:.1f} GB  ({_mode_note})"
            else:
                lib_status = f"OK — {MAX_LIBRARY_GB - library_gb:.1f} GB under cap of {MAX_LIBRARY_GB} GB"
            log(f"  Library:      {library_gb:.1f} GB  |  {lib_status}")
        else:
            _lib_over = False
        if redline_hit or over_limit or _lib_over:
            triggers = []
            if redline_hit: triggers.append("REDLINE")
            if _lib_over:
                triggers.append("LIBRARY CAP" if _cap_active else "LIBRARY CAP (inactive in current mode)")
            if over_limit: triggers.append("HEADROOM (daily)")
            log(f"  Would trigger: {' + '.join(triggers)}")
        else:
            log("  Would trigger: nothing — all limits satisfied")
        log("=" * 55)

        # ── Readiness check for Live ──────────────────────────────────
        # We already know validate_connections() passed (we'd have exited if not).
        _r_issues   = []   # blocking — script refuses to run in Live
        _r_warnings = []   # non-blocking — script runs but may behave unexpectedly

        # Library mount — empty MONITOR_DIRS is a safe no-op, but not ready for live cleanup.
        if not LIBRARY_ROOT.exists():
            _r_issues.append(
                "/library is not mounted — check the Plex library Docker volume"
            )
        elif not any(LIBRARY_ROOT.iterdir()):
            _r_issues.append(
                "/library is mounted but empty — verify the Plex library volume path"
            )
        elif not MONITOR_DIRS:
            _r_issues.append(
                "No Movie Library Paths configured — add at least one monitored folder "
                f"under {LIBRARY_ROOT} before enabling live cleanup"
            )
        else:
            _roots    = monitored_roots()
            _existing = [r for r in _roots if r.exists()]
            if not _existing:
                _r_issues.append(
                    f"None of the configured Movie Library Paths exist under {LIBRARY_ROOT} — "
                    "verify the folder names"
                )
            elif len(_existing) < len(_roots):
                _r_warnings.append(
                    f"{len(_roots) - len(_existing)} of {len(_roots)} Movie Library "
                    f"Path(s) not found under {LIBRARY_ROOT}"
                )

        # Space Thresholds
        for _err in _threshold_errors:
            if _err not in _r_issues:
                _r_issues.append(_err)

        # Filtering warnings
        if not PROTECTED_COLLECTIONS:
            _r_warnings.append(
                "PROTECTED_COLLECTIONS is empty — no Plex collection protection active"
            )

        # Optional Library Size Cap readiness warning. It only blocks Live when
        # the cap is enabled but the disk size read failed.
        _r_cap_issues = []
        if MAX_LIBRARY_GB is not None and library_gb is None:
            _r_cap_issues.append(
                "Library size could not be read from disk — cap cannot be verified"
            )

        def _show_ready(mode, extra_issues=None):
            all_issues = _r_issues + (extra_issues or [])
            log_blank()
            log(f'  Ready to run as RUN_MODE = "{mode}"?')
            log(f"  {'─' * 51}")
            log("  Connections:  ✓ validated")
            if all_issues:
                for issue in all_issues:
                    log(f"  ✗ {issue}")
            if _r_warnings:
                for warn in _r_warnings:
                    log(f"  ⚠ {warn}")
            if not all_issues:
                if not _r_warnings:
                    log(f'  → Ready. Set RUN_MODE = "{mode}" to go live.')
                else:
                    log('  → Ready, but review the warning(s) above first.')
            else:
                log(f"  → {len(all_issues)} issue(s) must be fixed before going live.")

        _show_ready("headroom", extra_issues=_r_cap_issues)

        log_blank()
        log("=" * 55)
        log_blank()
        _info_triggers = []
        if redline_hit: _info_triggers.append("Redline")
        if _lib_over:   _info_triggers.append("Library Size Cap")
        if over_limit:  _info_triggers.append("Headroom")
        _info_msg = ("Summary — would trigger: " + ", ".join(_info_triggers)) if _info_triggers \
                    else "Summary — all limits satisfied, nothing would run."
        _revalidate_pending_marks(limits_breached=bool(_info_triggers))
        emit_progress(status="done", phase="done", message=_info_msg)
        return

    # ── Decide whether to run ────────────────────────────────────────────────

    # Fail-closed cap-floor check against TODAY's library size. The cap was
    # validated when Live was armed, but the library can grow afterwards
    # (files copied in) — and a Live run may delete at most MAX_HEADROOM_PCT%
    # of the library. Mirrors the app's arm-time rule; sim may preview past
    # it, Live refuses.
    if (library_cap_hit and isinstance(MAX_HEADROOM_PCT, (int, float))
            and 0 < MAX_HEADROOM_PCT <= 100):
        # Compare UNROUNDED: rounding the floor for display first turned it
        # into 0.0 on small libraries and waved the deletion through.
        _cap_floor_gb = library_gb * (100 - MAX_HEADROOM_PCT) / 100
        if MAX_LIBRARY_GB < _cap_floor_gb:
            _floor_msg = (
                f"Library Size Cap {MAX_LIBRARY_GB} GB is below the safety floor "
                f"({library_gb:g} GB library × {100 - MAX_HEADROOM_PCT:g}% = {_cap_floor_gb:g} GB) — "
                f"reaching the cap would delete more than {MAX_HEADROOM_PCT:g}% of the library."
            )
            if _is_sim:
                log(f"WARNING: {_floor_msg}")
                log("WARNING: RUN_MODE='debug_sim' — simulation will continue, but Live would REFUSE TO RUN.")
                log_blank()
            else:
                log(f"ABORT: {_floor_msg}")
                emit_progress(status="error", phase="checking",
                              message="Library Size Cap is below the safety floor — raise the cap "
                                      "or the safety percentage, then run again.")
                return

    # Compute how many bytes need to be freed to satisfy all active conditions.
    _headroom_deficit_gb = max(0.0, used_gb - max_gb)
    # Redline fires on FREE space, so its deficit is measured in free terms:
    # free just enough to bring free space back to the REDLINE_GB floor. An
    # emergency clears the breach only — it does NOT top up to the headroom
    # target; the once-per-day headroom run does that (honoring the deletion
    # delay). Measuring against free rather than used matters on filesystems
    # with a root reserve (ext4's ~5%, btrfs metadata), where used-based math
    # can read 0 while free is genuinely below the floor — which would free
    # nothing while space keeps draining.
    _redline_deficit_gb  = max(0.0, REDLINE_GB - free_gb) if redline_hit else 0.0
    _library_deficit_gb  = max(0.0, library_gb - MAX_LIBRARY_GB) if library_cap_hit else 0.0
    # The emergency (redline) run restores the free-space floor only; the
    # library cap is a daily target that also honors the deletion delay, so
    # its deficit never rides along on an emergency run. Simulate previews
    # the full combined plan.
    if _is_sim or _manual_live:
        to_free_gb = max(_headroom_deficit_gb, _redline_deficit_gb, _library_deficit_gb)
    elif immediate_trigger:
        to_free_gb = _redline_deficit_gb
    else:
        to_free_gb = max(_headroom_deficit_gb, _library_deficit_gb)
    to_free_bytes = int(to_free_gb * 1_000_000_000)  # decimal GB → bytes, consistent with bytes_to_gb()

    # Build trigger label used in logs and summary headers.
    _triggers = []
    if redline_hit and (_is_sim or _manual_live or immediate_trigger):
        _triggers.append("REDLINE")
    if library_cap_hit and (_is_sim or _manual_live or not immediate_trigger):
        _triggers.append("LIBRARY CAP")
    if over_limit and (_manual_live or not immediate_trigger) and "scheduled daily" not in _triggers:
        _triggers.insert(0, "HEADROOM" if _manual_live else "scheduled daily")
    if not _triggers:
        _triggers.append("scheduled daily")
    trigger = " + ".join(_triggers)

    daily_breach = over_limit or library_cap_hit

    if _manual_live:
        # Manual Live Run: the user pressed the button, so prune to every
        # breached target NOW — the deletion delay and the once-per-day window
        # pace automatic runs only. Does not write the daily state file, so
        # the scheduler's window is unaffected.
        if not (daily_breach or redline_hit):
            log(
                f"MANUAL LIVE RUN: usage is {used_gb:.1f} GB ({free_gb:.1f} GB free), "
                f"within all space limits. Nothing to do."
            )
            emit_progress(status="done", phase="done",
                          message="Nothing to do — space limits are satisfied.")
            return
        log(
            f"MANUAL LIVE RUN [{trigger}]: over space limits ({used_gb:.1f} GB used, "
            f"{free_gb:.1f} GB free). Deleting now — the deletion delay and daily "
            f"schedule apply to automatic runs only."
        )
        log(f"Target: free at least {to_free_gb:.1f} GB.")
        log_blank()

    elif immediate_trigger:
        # Redline runs on every cron tick, bypassing the daily schedule AND
        # the deletion delay — waiting defeats an emergency floor. Does not
        # write the daily state file so the headroom/cap window is unaffected.
        log(
            f"REDLINE: only {free_gb:.1f} GB free, below emergency threshold of "
            f"{REDLINE_GB} GB. Running cleanup immediately."
        )
        log(f"Target: free at least {to_free_gb:.1f} GB.")
        log_blank()

    else:
        # Headroom and Library Size Cap share the once-per-day window;
        # debug_sim bypasses the schedule.
        if _is_sim:
            if not daily_breach:
                log(
                    f"DRY RUN: Usage is {used_gb:.1f} GB ({free_gb:.1f} GB free), "
                    f"below limit of {max_gb:.1f} GB and under the cap. "
                    f"Nothing to do (ignoring daily schedule)."
                )
                emit_progress(status="done", phase="done",
                              message="Dry run — space limits are satisfied, nothing to simulate.")
                return
            log(
                f"DRY RUN [{trigger}]: over space limits "
                f"({used_gb:.1f} GB used, {free_gb:.1f} GB free). "
                f"Simulating cleanup (ignoring daily schedule)."
            )
            if DELETE_DELAY_DAYS > 0:
                log(
                    f"Deletion delay: {DELETE_DELAY_DAYS} day(s) — a live run MARKS new "
                    f"candidates and only deletes marks older than {DELETE_DELAY_DAYS} day(s)."
                )
            log_blank()

        else:
            # Live mode — enforce the once-per-day window.
            today = time.strftime("%Y-%m-%d")
            last_run = read_last_cleanup_date()
            is_new_day = last_run != today

            if not is_new_day:
                if daily_breach:
                    log(
                        f"Over space limits [{trigger}] ({used_gb:.1f} GB used, "
                        f"{free_gb:.1f} GB free) but the daily window is already used "
                        f"today ({today}). Skipping until tomorrow."
                    )
                    emit_progress(status="done", phase="done",
                                  message="Already ran today — waiting until tomorrow.")
                else:
                    log(
                        f"Usage is {used_gb:.1f} GB ({free_gb:.1f} GB free), "
                        f"within all space limits. Nothing to do."
                    )
                    emit_progress(status="done", phase="done",
                                  message="Nothing to do — space limits are satisfied.")
                return

            if not daily_breach:
                # The daily window is only consumed by an actual cleanup —
                # a within-limits tick must not burn it, or a breach later the
                # same day would be skipped until tomorrow ("triggers once per
                # calendar day WHEN a limit is breached").
                log(
                    f"Usage is {used_gb:.1f} GB ({free_gb:.1f} GB free), "
                    f"within all space limits. Nothing to do today."
                )
                emit_progress(status="done", phase="done",
                              message="Nothing to do — space limits are satisfied.")
                return

            if time.strftime("%H:%M") < DAILY_RUN_TIME:
                # An eligible day still waits for the scheduled time of day.
                # The window is only consumed by the cleanup that actually
                # runs, so this wait can never cost the day its run.
                log(
                    f"Over space limits [{trigger}] ({used_gb:.1f} GB used, "
                    f"{free_gb:.1f} GB free) — waiting for today's scheduled "
                    f"run time ({DAILY_RUN_TIME})."
                )
                emit_progress(status="done", phase="done",
                              message=f"Waiting for today's scheduled run time ({DAILY_RUN_TIME}).")
                return

            write_last_cleanup_date()

            log(
                f"Over space limits [{trigger}] ({used_gb:.1f} GB used, "
                f"{free_gb:.1f} GB free). Running scheduled daily cleanup."
            )
            log_blank()

    # ── Build candidates and run cleanup ────────────────────────────────────

    candidates, section1_paths_by_tmdb, build_stats, total_scanned = build_candidates()

    # The library size that drives the cap comes straight from disk in
    # get_library_size_gb() (library_gb above), so it reflects the true current
    # on-disk total — including files the media server has not catalogued. Using
    # the one disk figure everywhere keeps the dashboard number, the trigger,
    # and the deletion target in agreement.
    effective_library_gb = library_gb

    if not candidates:
        log("No eligible movie files found.")
        _mm = build_stats.get("identity_mismatch", 0)
        if _mm:
            log_identity_mismatches(build_stats)
            emit_progress(status="done", phase="done", scanned=total_scanned,
                          completed_with_errors=True,
                          message=f"Completed with errors — {_mm} file(s) skipped (Plex/Jellyfin identity mismatch); no other eligible movies.")
        else:
            emit_progress(status="done", phase="done", scanned=total_scanned,
                          message="No eligible movies to remove.")
        return

    log_stage("SIMULATION" if _is_sim else "DELETIONS")

    if _is_sim:
        simulated_used = usage_info["used"]
        simulated_freed_bytes = 0
        simulated_count = 0
        _sim_planned: list = []   # (candidate, size) — becomes the marked queue when a delay is set

        log(f"DRY RUN [{trigger}]: Simulating deletions — target: free {to_free_gb:.1f} GB.")
        emit_progress(phase="simulating", trigger=trigger, target_bytes=to_free_bytes,
                      deleted=0, bytes_freed=0, current_title="",
                      message="Simulating cleanup — no files touched…")

        pending = list(candidates)
        tie_group: list = []
        while pending or tie_group:
            # Target check at the TOP, exactly like the live loop below — a
            # bottom-of-loop check would pop (and report) one extra movie
            # whenever the target is already met, diverging from a real run.
            if simulated_freed_bytes >= to_free_bytes:
                break
            candidate = _pop_next_deletion(pending, tie_group, to_free_bytes - simulated_freed_bytes)
            try:
                file_size = candidate["path"].stat().st_size
            except OSError as e:
                # Missing, permission-denied, or a hiccuping mount — skip the
                # file rather than abort the whole preview.
                log(f"DRY RUN: Skipping unreadable file during simulation ({e}): {candidate['path']}")
                continue

            before_gb = round(bytes_to_gb(simulated_used), 1)
            simulated_used -= file_size
            simulated_freed_bytes += file_size
            simulated_count += 1
            after_gb = round(bytes_to_gb(simulated_used), 1)

            log(
                f"DRY RUN DELETE #{simulated_count}: "
                f"{candidate['title']} | "
                f"score={round(candidate['retention_score'], 3)} | "
                f"imdb={candidate['imdb_rating']} | votes={candidate['imdb_votes']} | year={candidate['release_year']} | "
                f"plays={candidate['play_count']} | "
                f"last_played={format_epoch(candidate['last_played'])} | "
                f"size={bytes_to_gb(file_size):.2f} GB | "
                f"added={format_epoch(candidate['added_at'])} | "
                f"used {before_gb:.1f} GB -> {after_gb:.1f} GB | "
                f"path={candidate['path']}"
            )
            emit_progress(phase="simulating", deleted=simulated_count,
                          bytes_freed=simulated_freed_bytes, target_bytes=to_free_bytes,
                          current_title=candidate["title"])

            _sim_planned.append((candidate, file_size))
            remove_empty_movie_folder(candidate["path"])

        # The simulation IS the marking step: it writes its plan to the
        # marked-for-deletion queue (keeping existing marks' clocks) so the
        # user can review what deletes and when — BEFORE arming Live. It
        # never deletes and never consumes the daily window; stale marks not
        # in this plan drop off. With delay 0 the marks are simply eligible
        # immediately: the next daily run deletes them.
        log_blank()
        write_plan_to_queue(_sim_planned, trigger)

        final_gb = round(bytes_to_gb(simulated_used), 1)
        final_free_gb = round(bytes_to_gb(usage_info["total"]) - final_gb, 1)
        log_run_summary(
            is_sim=True, trigger=trigger, to_free_gb=to_free_gb,
            used_gb=used_gb, free_before_gb=round(bytes_to_gb(usage_info["free"]), 1),
            final_gb=final_gb, final_free_gb=final_free_gb,
            freed_bytes=simulated_freed_bytes, removed_count=simulated_count,
            skipped_under_limit=len(candidates) - simulated_count,
            effective_library_gb=effective_library_gb, max_gb=max_gb,
            build_stats=build_stats, total_scanned=total_scanned,
            library_cap_hit=library_cap_hit,
        )

        log_blank()
        _mm = build_stats.get("identity_mismatch", 0)
        if DELETE_DELAY_DAYS > 0:
            _sim_msg = (f"Dry run — marked {simulated_count} movie(s) for deletion "
                        f"({DELETE_DELAY_DAYS}-day delay), ~{bytes_to_gb(simulated_freed_bytes):.1f} GB.")
        else:
            _sim_msg = (f"Dry run — marked {simulated_count} movie(s), "
                        f"~{bytes_to_gb(simulated_freed_bytes):.1f} GB — deletes at the next daily run.")
        if _mm:
            _sim_msg += f" Completed with errors — {_mm} file(s) skipped (Plex/Jellyfin identity mismatch)."
        emit_progress(status="done", phase="done", deleted=simulated_count,
                      bytes_freed=simulated_freed_bytes, target_bytes=to_free_bytes,
                      current_title="", completed_with_errors=bool(_mm),
                      message=_sim_msg)
        return

    # Live mode
    deleted_count = 0
    bytes_freed = 0
    marked_count = 0
    # planned_bytes gates the loop against to_free_bytes. It sums APPARENT file
    # sizes (st_size), while a library-cap target derives from get_library_size_gb
    # (ALLOCATED, st_blocks — it tracks `du`). The two bases differ only by
    # per-file block rounding, so the cap may over/undershoot by a few KB per
    # file; the next daily run re-measures against the real library size and
    # corrects it. Both bases are intentional (apparent = the per-file size users
    # see in Plex/logs; allocated = real disk usage) — don't "unify" them.
    planned_bytes = 0   # deleted + newly-planned marked bytes: the target gate
    # Deletion delay: daily runs MARK candidates first and delete a mark only
    # once it has aged past the delay. Redline emergency runs and manual Live
    # Runs delete immediately — waiting defeats an emergency floor, and the
    # delay paces automatic runs, not a deliberate button press.
    use_delay = DELETE_DELAY_DAYS > 0 and not immediate_trigger and not _manual_live
    mark_store = load_pending()
    mark_store_dirty = False
    kept_marks: dict = {}
    now_ts = time.time()
    emit_progress(phase="deleting", trigger=trigger, target_bytes=to_free_bytes,
                  deleted=0, bytes_freed=0, current_title="",
                  message="Freeing space…")

    # A redline emergency deletes straight down THIS run's freshly-scored
    # candidate list — every movie is re-scored under the current monitored
    # paths, filters, and scoring at the moment the redline fires, so the order
    # always reflects the latest settings, never a stale snapshot from when a
    # movie was marked. That order is lowest-value first, which is exactly the
    # already-marked movies (they were marked because they score lowest), so an
    # emergency clears the marked queue in order before touching anything
    # unmarked — and follows the updated order if you have since changed paths,
    # filters, or scoring. Strict order (no near-tie file-size optimization)
    # keeps that sequence intact; paced daily runs and manual Live Runs keep the
    # optimizer.
    _emergency = immediate_trigger and not _manual_live
    pending = list(candidates)
    tie_group = []
    while pending or tie_group:
        if planned_bytes >= to_free_bytes:
            break
        candidate = pending.pop(0) if _emergency \
            else _pop_next_deletion(pending, tie_group, to_free_bytes - planned_bytes)
        # A transient filesystem error mid-loop (mount hiccup, permission
        # change, file vanishing between exists() and stat()) must not abort
        # the run after some files are already gone — that would skip the
        # summary, Radarr cleanup, and the terminal progress update.
        try:
            log_usage()
        except OSError as e:
            log(f"WARN: storage check failed mid-run ({e}); continuing.")
        emit_progress(phase="deleting", current_title=candidate["title"],
                      deleted=deleted_count, bytes_freed=bytes_freed, target_bytes=to_free_bytes)
        try:
            size_before = candidate["path"].stat().st_size if candidate["path"].exists() else 0
        except OSError as e:
            log(f"WARN: could not stat {candidate['path']} ({e}); skipping this candidate.")
            continue
        key = str(candidate["path"])
        if use_delay:
            entry = mark_store.get(key)
            age_days = (_mark_age_days(entry.get("marked_at", now_ts), now_ts)
                        if isinstance(entry, dict) else 0)
            if not isinstance(entry, dict) or age_days < DELETE_DELAY_DAYS:
                # Mark (or keep the existing mark) instead of deleting.
                if not isinstance(entry, dict):
                    entry = {"title": candidate["title"],
                             "score": round(candidate["retention_score"], 3),
                             "marked_at": now_ts, "trigger": trigger}
                    log(f"MARKED for deletion (deletes on {_mark_delete_on(now_ts)} "
                        f"unless protected or the rules change): {candidate['title']} | path={key}")
                else:
                    log(f"Still marked (day {age_days}/{DELETE_DELAY_DAYS}, "
                        f"deletes on {_mark_delete_on(entry.get('marked_at'))}): {candidate['title']}")
                entry["size_bytes"] = size_before
                kept_marks[key] = entry
                marked_count += 1
                planned_bytes += size_before
                continue
            log(f"Mark aged {age_days} day(s) (delay {DELETE_DELAY_DAYS}) — deleting: {candidate['title']}")
        delete_candidate(candidate, section1_paths_by_tmdb)
        if not candidate["path"].exists():
            deleted_count += 1
            bytes_freed += size_before
            planned_bytes += size_before
            if mark_store.pop(key, None) is not None:
                mark_store_dirty = True
        elif use_delay:
            # The file survived delete_candidate (permission error, safety
            # abort, mount hiccup). Carry the EXISTING mark forward with its
            # original marked_at so the retry next run doesn't reset the delay
            # clock — otherwise a transient error silently re-arms the full wait.
            existing = mark_store.get(key)
            entry = existing if isinstance(existing, dict) else {
                "title": candidate["title"],
                "score": round(candidate["retention_score"], 3),
                "marked_at": now_ts, "trigger": trigger}
            entry["size_bytes"] = size_before
            kept_marks[key] = entry
            log(f"WARN: deletion did not remove {candidate['title']}; "
                f"keeping its existing mark (will retry next run).")

    # Reconcile the marked-for-deletion queue with THIS run's plan.
    if use_delay:
        # kept_marks is the current plan; everything else (protected since,
        # rules changed, no longer within the target, or just deleted) drops.
        dropped = [k for k in mark_store if k not in kept_marks]
        if dropped:
            log(f"Unmarked {len(dropped)} movie(s) no longer in the deletion plan.")
        save_pending(kept_marks, stamp_thresholds=True)
        if marked_count:
            log_blank()
            log(f"Marked for deletion: {marked_count} movie(s), "
                f"{bytes_to_gb(planned_bytes - bytes_freed):.1f} GB — deletes after "
                f"{DELETE_DELAY_DAYS} day(s) unless protected or the rules change.")
    elif _manual_live or not immediate_trigger:
        # Delay disabled, or a manual Live Run that pruned to every breached
        # target: everything planned was deleted outright, so the queue is
        # done — whether entries remain (stale from an earlier delay) or were
        # popped by this run's deletions (mark_store_dirty).
        if mark_store or mark_store_dirty:
            save_pending({})
    elif mark_store_dirty:
        # Redline runs never reshape the daily plan — they only drop entries
        # whose files they deleted.
        save_pending(mark_store)

    final_info = get_usage_info()
    final_gb = final_info["used_gb"]
    final_free_gb = round(bytes_to_gb(final_info["free"]), 1)
    log_run_summary(
        is_sim=False, trigger=trigger, to_free_gb=to_free_gb,
        used_gb=used_gb, free_before_gb=free_gb,
        final_gb=final_gb, final_free_gb=final_free_gb,
        freed_bytes=bytes_freed, removed_count=deleted_count,
        skipped_under_limit=len(candidates) - deleted_count - marked_count,
        effective_library_gb=effective_library_gb, max_gb=max_gb,
        build_stats=build_stats, total_scanned=total_scanned,
    )
    log_blank()
    _mm = build_stats.get("identity_mismatch", 0)
    if marked_count:
        _live_msg = (f"Removed {deleted_count}, marked {marked_count} for deletion "
                     f"({DELETE_DELAY_DAYS}-day delay), freed {bytes_to_gb(bytes_freed):.1f} GB.")
    else:
        _live_msg = f"Removed {deleted_count} movie(s), freed {bytes_to_gb(bytes_freed):.1f} GB."
    if _mm:
        _live_msg += f" Completed with errors — {_mm} file(s) skipped (Plex/Jellyfin identity mismatch)."
    emit_progress(status="done", phase="done", deleted=deleted_count, marked=marked_count,
                  bytes_freed=bytes_freed, target_bytes=to_free_bytes, current_title="",
                  completed_with_errors=bool(_mm), message=_live_msg)

    # Note: the daily state file was already written at the start of this run
    # (before cleanup) so any mid-day limit breach is correctly blocked.


if __name__ == "__main__":
    # _finalize_run() archives this run's log; idempotent (guarded by
    # _RUN_FINALIZED) and also registered via atexit. Calling it in a finally
    # block guarantees the archive on completion, early return, or exception,
    # rather than relying on atexit hooks that some exit paths skip.
    try:
        main()
    finally:
        _finalize_run()
