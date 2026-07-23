"""
MediaReducer web server (Flask). Serves the dashboard and config UI, launches
engine.py as a subprocess for Simulate/Live/Summary runs, drives an APScheduler
tick that performs automatic daily Live deletion, gates Live on plan-currency
(a completed Simulate under the current config), and builds a sanitized debug
report. Run state (_run_active/_run_process) is in-memory and resets on restart;
RUN_MODE is forced Paused on every startup.
"""

import configparser
import copy
import gzip
try:
    import fcntl
except ImportError:          # non-POSIX dev box — engine.py degrades the same way
    fcntl = None
import ipaddress
import hashlib
import io
import json
import math
import os
import sys
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from apscheduler.schedulers.background import BackgroundScheduler

from scoring_constants import SCORING
from flask import Flask, jsonify, render_template, request, send_from_directory, has_request_context

import db  # shared SQLite persistence (metadata cache, library snapshot, queue, meta)

# ── Paths ─────────────────────────────────────────────────────────────────────

APP_DIR          = Path(__file__).parent


def _load_dotenv() -> None:
    """For bare-metal runs, load a .env file (next to app.py, or MEDIAREDUCER_ENV_FILE)
    so MEDIAREDUCER_* vars live in one place. A real env var always wins, so Docker is
    unaffected. Must run before any root/config constant is read; the engine subprocess
    inherits the result via os.environ.copy()."""
    env_path = Path(os.environ.get("MEDIAREDUCER_ENV_FILE") or (APP_DIR / ".env"))
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
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv()

SCRIPT_PATH      = APP_DIR / "engine.py"
DEFAULT_CFG_PATH = APP_DIR / "default_config.json"
CONFIG_PATH      = Path(os.environ.get("MEDIAREDUCER_CONFIG", "/config/config.json"))


def _root_from_env(var: str, default: str) -> str:
    """Deployment root, relocatable for bare-metal installs. The engine inherits
    this process's environment and reads the SAME vars, so the two always agree.
    Deploy-time infrastructure — never a UI or config setting."""
    return os.environ.get(var, default).rstrip("/") or default


# The library mount (deletion boundary) and the three appdata mounts default to
# their Docker paths; set the env vars to run outside Docker.
FILESYSTEM_CHECK_PATH = _root_from_env("MEDIAREDUCER_LIBRARY", "/library")
TAUTULLI_APPDATA_DIR  = _root_from_env("MEDIAREDUCER_TAUTULLI_APPDATA", "/tautulli")
RADARR_APPDATA_DIR    = _root_from_env("MEDIAREDUCER_RADARR_APPDATA", "/radarr")
JELLYFIN_APPDATA_DIR  = _root_from_env("MEDIAREDUCER_JELLYFIN_APPDATA", "/jellyfin")

# Single background clock. Each tick runs an automatic Live cleanup when Live is
# enabled, else a quiet Summary/debug_info refresh so dashboard disk/library
# numbers never go stale. Paused while any run/Summary is in flight and restarted
# from zero when it finishes. Coarse because a cleanup tick can trigger a full
# deletion pass.
SCHEDULE_INTERVAL_MINUTES = 15
CONNECTION_CONFIG_FIELDS = (
    "TAUTULLI_URL", "TAUTULLI_API_KEY", "PLEX_URL", "PLEX_TOKEN",
    "RADARR_URL", "RADARR_API_KEY", "JELLYFIN_URL", "JELLYFIN_API_KEY",
)
CONNECTION_ONBOARDING_SEEN_KEY = "_CONNECTIONS_ONBOARDING_SEEN"
CONNECTION_EVER_CONFIGURED_KEY = "_CONNECTIONS_EVER_CONFIGURED"
WELCOME_GUIDE_SEEN_KEY = "_WELCOME_GUIDE_SEEN"
RADARR_SECTION_CACHE_KEYS = (
    "_RADARR_DETECTED_SECTION_ID",
    "_RADARR_DETECTED_SECTION_NAME",
    "_RADARR_DETECTED_SECTION_METHOD",
    "_RADARR_DETECTED_SECTION_METHOD_LABEL",
)
RADARR_SECTION_METHOD_LABELS = {
    "path-prefix": "path prefix",
    "root-prefix": "root folder path",
    "folder-name": "library folder name",
}

app = Flask(__name__)


@app.template_filter("commafy")
def _tpl_commafy(value, digits=1):
    """Thousands-separated number, rounded to `digits` decimals with trailing zeros
    dropped — mirrors JS toLocaleString(maximumFractionDigits: digits)."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    s = f"{num:,.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


@app.template_filter("group_int")
def _tpl_group_int(value):
    """Thousands-separate the integer part of an already-formatted numeric
    string, keeping its fractional digits exactly ('3.20' -> '3.20',
    '1234.5' -> '1,234.5'). Non-numeric input is returned unchanged."""
    s = str(value).strip()
    neg = s.startswith("-")
    int_part, dot, frac = (s[1:] if neg else s).partition(".")
    if not int_part.isdigit():
        return value
    return ("-" if neg else "") + f"{int(int_part):,}" + (("." + frac) if dot else "")

# ── Drive-by request protection ──────────────────────────────────────────────
# No login (LAN tool), so two browser attack paths stay open: cross-origin POSTs
# from any site the user visits (a simple request needs no preflight, and /api/run
# with an empty body starts a cleanup), and DNS rebinding (an attacker domain
# resolving to this LAN IP, making its page same-origin). Two cheap checks close both:
#   1. Every mutating request must carry the X-MediaReducer header. base.html's
#      fetch wrapper adds it; a cross-origin page cannot without a CORS preflight
#      this server never approves.
#   2. The Host header must look local: IP literal, localhost, a dot-less LAN
#      hostname, or *.local (mDNS). Reverse-proxy names go in
#      MEDIAREDUCER_TRUSTED_HOSTS (comma-separated).
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_header_allowed(host_header: str | None) -> bool:
    raw = str(host_header or "").strip().lower().rstrip(".")
    if not raw:
        return False
    try:
        ipaddress.ip_address(raw)
        return True  # bare IP literal, including unbracketed IPv6 like ::1
    except ValueError:
        pass
    try:
        host = (urlparse("//" + raw).hostname or "").rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    if "." not in host and ":" not in host:
        return True  # single-label LAN hostname (e.g. "tower")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    trusted = os.environ.get("MEDIAREDUCER_TRUSTED_HOSTS", "")
    return host in {h.strip().lower().rstrip(".") for h in trusted.split(",") if h.strip()}


@app.before_request
def _reject_cross_origin_requests():
    if not _host_header_allowed(request.host):
        return jsonify({
            "ok": False,
            "error": "Rejected: unrecognized Host header. Access MediaReducer by IP or local "
                     "hostname, or add this name to MEDIAREDUCER_TRUSTED_HOSTS.",
        }), 403
    if request.method in _MUTATING_METHODS and request.headers.get("X-MediaReducer") != "1":
        return jsonify({
            "ok": False,
            "error": "Rejected: missing X-MediaReducer header. If you are scripting against "
                     "the API, send \"X-MediaReducer: 1\" with every write request.",
        }), 403
    # Remember this request's LAN host so background/startup probes (no request
    # context) resolve service URL defaults to the SAME address the Config page
    # shows, instead of an appdata-detected host that can wrongly report down.
    _request_lan_host()
    return None


# Serializes config.json read-modify-write cycles and the atomic save. RLock so
# helpers can hold it across load_config() + save_config() without deadlocking on
# save_config's own acquisition.
_config_io_lock = threading.RLock()

# Connection health is probed once on startup so the Config page can show
# connection problems without waiting for a Check for Errors click. Keyed by the
# connection-relevant config values so edited/saved settings don't reuse stale results.
_connection_health_cache_lock = threading.Lock()
_connection_health_cache: dict = {"signature": None, "health": None, "checked_at": None}

# Common time zones shown first in the config dropdown; the full IANA list is
# appended after these.
_COMMON_TIME_ZONES = [
    "UTC",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Singapore",
    "Australia/Sydney",
]

def _time_zone_options() -> list[str]:
    try:
        all_zones = sorted(available_timezones())
    except Exception:
        all_zones = []
    seen = set()
    options = []
    for zone in _COMMON_TIME_ZONES + all_zones:
        if zone and zone not in seen:
            seen.add(zone)
            options.append(zone)
    return options

# Application version — surfaced in the debug report so bug reports name the
# build. Bump on release.
APP_VERSION = "1.0.0-beta.1"

# Host clock, captured before any TIME_ZONE override is applied so switching the
# setting back to auto can restore it.
_HOST_TZ = os.environ.get("TZ")
_HOST_TZ_NAME = (_HOST_TZ
                 or getattr(datetime.now().astimezone().tzinfo, "key", None)
                 or datetime.now().astimezone().tzname() or "UTC")

def _server_time_zone_name() -> str:
    """Best-effort IANA-ish name for the zone the process clock follows —
    the configured TIME_ZONE once applied, else the container clock."""
    env_tz = str(os.environ.get("TZ") or "").strip()
    if env_tz:
        return env_tz
    local = datetime.now().astimezone()
    return getattr(local.tzinfo, "key", None) or local.tzname() or "UTC"

def _host_time_zone_name() -> str:
    """Zone the container clock follows when TIME_ZONE is auto."""
    return _HOST_TZ_NAME


# ── Config helpers ────────────────────────────────────────────────────────────

def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _normalize_library_path(value: str | None) -> str | None:
    """Return a normalised path under the library root."""
    root = FILESYSTEM_CHECK_PATH
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
        # Unknown absolute path outside the library root: its root can't be
        # inferred, so keep only the library folder name.
        suffix = raw.rstrip("/").split("/")[-1]
    else:
        suffix = raw.lstrip("/")
    suffix = suffix.strip("/")
    return f"{root}/{suffix}" if suffix else None


def _normalize_library_paths(cfg: dict) -> dict:
    """Reduce MONITOR_DIRS to a clean, deduplicated list of library subfolders."""
    monitor_dirs = []
    for raw in cfg.get("MONITOR_DIRS", []) or []:
        normalized = _normalize_library_path(str(raw).strip())
        if normalized and normalized not in monitor_dirs:
            monitor_dirs.append(normalized)
    cfg["MONITOR_DIRS"] = monitor_dirs
    return cfg


def _monitoring_summary_signature(cfg: dict | None) -> dict:
    """Values that change the dashboard library-size scan."""
    normalized = dict(cfg or {})
    _normalize_library_paths(normalized)
    monitor_dirs = [
        str(path).strip().replace("\\", "/").rstrip("/")
        for path in (normalized.get("MONITOR_DIRS") or [])
        if str(path).strip()
    ]
    extensions = [
        str(ext).strip().lower()
        for ext in (normalized.get("MOVIE_EXTENSIONS") or [])
        if str(ext).strip()
    ]
    return {
        "monitor_dirs": monitor_dirs,
        "movie_extensions": sorted(set(extensions)),
    }


def _should_refresh_summary_after_config_save(saved_cfg: dict, new_cfg: dict) -> bool:
    """Queue storage stats only when saved config changes what gets measured."""
    if _monitoring_summary_signature(saved_cfg) != _monitoring_summary_signature(new_cfg):
        return True
    stats = library_stats()
    has_monitor_dirs = bool(_monitoring_summary_signature(new_cfg)["monitor_dirs"])
    return has_monitor_dirs and stats.get("library_gb") is None


def _normalize_retention_scoring(cfg: dict) -> None:
    """Clamp the retention-score fields to their valid ranges."""
    try:
        cfg["GRACE_PERIOD_DAYS"] = max(0, int(float(cfg.get("GRACE_PERIOD_DAYS", 30))))
    except (TypeError, ValueError):
        cfg["GRACE_PERIOD_DAYS"] = 30
    try:
        cfg["SCORE_BALANCE"] = max(0, min(100, round(float(cfg.get("SCORE_BALANCE", 50)))))
    except (TypeError, ValueError):
        cfg["SCORE_BALANCE"] = 50
    cfg["MAX_IMDB_RATING"] = _clamp_max_imdb_rating(cfg.get("MAX_IMDB_RATING"))
    cfg["NEAR_TIE_PTS"] = _clamp_near_tie_pts(cfg.get("NEAR_TIE_PTS", 2))
    cfg["MAX_STALENESS_MONTHS"] = _clamp_staleness_months(cfg.get("MAX_STALENESS_MONTHS", 36))


def _is_blank(value) -> bool:
    """None or a whitespace-only string — the GUI's 'disabled' spelling."""
    return value is None or (isinstance(value, str) and not value.strip())


def _clamp_max_imdb_rating(value):
    """Nullable rating cutoff: None/blank = disabled, else clamp to 0.1–10.
    A value of 0 or below also reads as disabled — a cutoff of 0 matches
    nothing, so it means the same as "no cutoff" rather than being an error."""
    if _is_blank(value):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return round(min(10.0, f), 1)


def _clamp_near_tie_pts(value):
    """Nullable near-tie window in retention-score points: None/blank =
    file size optimization off, else clamp to 0.5–25."""
    if _is_blank(value):
        return None
    try:
        return round(max(0.5, min(25.0, float(value))), 1)
    except (TypeError, ValueError):
        return None


def _clamp_staleness_months(value):
    """Max staleness window in months (the recency curve fades to 0 over it):
    a whole number clamped to 1–120, default 36."""
    try:
        return int(max(1, min(120, round(float(value)))))
    except (TypeError, ValueError):
        return 36


# Hand-edit guard: config.json is only ever written by the GUI (which validates
# everything), so a value that breaks a GUI rule means someone edited the file by
# hand. load_config() refreshes this list on every read; while it is non-empty the
# app locks out runs and every config-mutating endpoint until the invalid values
# are reset (/api/config/reset-invalid) or MediaReducer is reset.
_CONFIG_FILE_ISSUES: list = []

# Reset target for invalid keys, and the effective value of keys missing from the
# file (the cross-field check compares what a run would actually use).
with open(DEFAULT_CFG_PATH) as _f:
    _CONFIG_DEFAULTS: dict = json.load(_f)


def _config_num(value):
    """Finite float for validation, else None. Bools are not numbers here, and JSON
    can smuggle in inf/nan (1e999) which no GUI rule accepts."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# Numeric GUI rules: key, skip-mode for the disabled spelling (None = value
# required when the key is present, "null" = literal null allowed, "blank" =
# null or blank string allowed), acceptance test, message.
_CONFIG_NUM_RULES = (
    ("HEADROOM_GB", None, lambda n: n >= 0, "must be a number of GB, zero or greater"),
    ("REDLINE_GB", "null", lambda n: n > 0, "must be a number of GB above zero, or null"),
    ("MAX_HEADROOM_PCT", None, lambda n: 0 < n <= 100, "must be a percentage above 0 and at most 100"),
    ("MAX_LIBRARY_GB", "null", lambda n: n > 0, "must be a number of GB above zero, or null"),
    ("GRACE_PERIOD_DAYS", None, lambda n: n >= 0 and float(n).is_integer(), "must be a whole number of days, zero or greater"),
    # Floor of 1 (a marked movie is never deleted the same day), so 0 has no valid
    # meaning — the GUI never writes it. A hand-edited 0 locks out like any other
    # out-of-range edit rather than being silently reinterpreted.
    ("DELETE_DELAY_DAYS", None, lambda n: 1 <= n <= 365 and float(n).is_integer(), "must be a whole number of days from 1 to 365"),
    ("LOG_RETENTION_DAYS", None, lambda n: n >= 0, "must be a number of days, zero or greater"),
    ("IMDB_RATINGS_MAX_AGE_DAYS", None, lambda n: n >= 1, "must be a number of days, one or greater"),
    ("SCORE_BALANCE", None, lambda n: 0 <= n <= 100, "must be a number from 0 to 100"),
    # 0 is accepted as a disabled spelling (a cutoff of 0 matches nothing, so the
    # clamp reads it as None) — the GUI writes null, not 0.
    ("MAX_IMDB_RATING", "blank", lambda n: 0 <= n <= 10, "must be a number from 0 to 10, or null"),
    ("NEAR_TIE_PTS", "blank", lambda n: 0.5 <= n <= 25, "must be a number from 0.5 to 25 points, or null"),
    ("MAX_STALENESS_MONTHS", None, lambda n: 1 <= n <= 120, "must be a number of months from 1 to 120"),
)


def _config_file_issues(saved: dict) -> list[dict]:
    """Validate the raw config.json content against the same rules the GUI
    enforces on save. Only keys present in the file are checked — missing keys
    fall back to defaults. Returns [{key, message}, ...]."""
    issues: list[dict] = []

    def bad(key, message):
        issues.append({"key": key, "message": message})

    for key, skip, ok, message in _CONFIG_NUM_RULES:
        if key not in saved:
            continue
        v = saved[key]
        if (skip == "null" and v is None) or (skip == "blank" and _is_blank(v)):
            continue
        n = _config_num(v)
        if n is None or not ok(n):
            bad(key, message)
    # Cross-field check on the EFFECTIVE values (defaults fill missing keys, so
    # deleting HEADROOM_GB can't smuggle in an oversized redline). Skipped while
    # either field is itself flagged: its range message already covers it, and
    # comparing garbage would double-flag.
    flagged = {i["key"] for i in issues}
    if not ({"REDLINE_GB", "HEADROOM_GB", "MAX_LIBRARY_GB"} & flagged):
        redline = _config_num(saved.get("REDLINE_GB", _CONFIG_DEFAULTS.get("REDLINE_GB")))
        headroom = _config_num(saved.get("HEADROOM_GB", _CONFIG_DEFAULTS.get("HEADROOM_GB")))
        cap = _config_num(saved.get("MAX_LIBRARY_GB", _CONFIG_DEFAULTS.get("MAX_LIBRARY_GB")))
        # The ceiling applies whenever the mode is off, STRICTLY: a Redline at
        # or above the headroom value (0 included) is redline-only mode spelled
        # wrong — REDLINE_ONLY_MODE is the supported way to run that.
        if (not bool(saved.get("REDLINE_ONLY_MODE", _CONFIG_DEFAULTS.get("REDLINE_ONLY_MODE")))
                and redline is not None and headroom is not None and redline >= headroom):
            bad("REDLINE_GB", "must be lower than HEADROOM_GB (untick Headroom for redline-only mode)")
        # REDLINE_ONLY_MODE = the GUI's Headroom checkbox unticked (the headroom
        # trigger is off). Valid as long as SOMETHING else drives cleanup — a Redline
        # floor and/or a Library Size Cap (either or both) — with the headroom value at
        # 0. WITHOUT the flag, HEADROOM_GB 0 just means the headroom trigger is off and
        # Redline and/or the cap may still be armed alone.
        has_dirs = bool(saved.get("MONITOR_DIRS", _CONFIG_DEFAULTS.get("MONITOR_DIRS")) or [])
        rl_mode = bool(saved.get("REDLINE_ONLY_MODE", _CONFIG_DEFAULTS.get("REDLINE_ONLY_MODE")))
        if rl_mode and has_dirs:
            if redline is None and cap is None:
                bad("REDLINE_ONLY_MODE", "with the headroom trigger off, arm a Redline floor "
                    "and/or a Library Size Cap")
            if headroom not in (None, 0):
                bad("HEADROOM_GB", "must be 0 when the headroom trigger is off (REDLINE_ONLY_MODE)")
    for key in ("SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES", "USE_PLEX",
                "USE_JELLYFIN", "KEEP_INTERRUPTED_LOGS", "DEBUG_MODE",
                "REDLINE_ONLY_MODE"):
        if key in saved and not isinstance(saved[key], bool):
            bad(key, "must be true or false")
    if "RUN_MODE" in saved and saved["RUN_MODE"] not in ("paused", "headroom"):
        bad("RUN_MODE", 'must be "paused" or "headroom"')
    # Debug mode and Live are mutually exclusive: Debug mode is a no-delete
    # diagnostic state, so it can only be turned on while Scheduler Mode is
    # Paused, and it can't be on while the mode is Live.
    if saved.get("DEBUG_MODE") and saved.get("RUN_MODE", "paused") != "paused":
        bad("DEBUG_MODE", "can only be on while Scheduler Mode is Paused — "
                          "Debug mode does not run cleanup deletions")
    if "IMDB_RATINGS_URL" in saved:
        try:
            _validate_imdb_url(saved["IMDB_RATINGS_URL"])
        except ValueError:
            bad("IMDB_RATINGS_URL", "must be an http(s) URL")
    if "MONITOR_DIRS" in saved:
        v = saved["MONITOR_DIRS"]
        if not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v):
            bad("MONITOR_DIRS", "must be a list of path strings")
    if "MOVIE_EXTENSIONS" in saved:
        v = saved["MOVIE_EXTENSIONS"]
        if not isinstance(v, list) or not v or any(
                not isinstance(x, str) or not x.strip().startswith(".") for x in v):
            bad("MOVIE_EXTENSIONS", 'must be a list of extensions like ".mkv"')
    for key in ("TAUTULLI_URL", "PLEX_URL", "RADARR_URL", "JELLYFIN_URL"):
        if key in saved:
            v = saved[key]
            if not isinstance(v, str):
                bad(key, "must be a URL string (may be blank)")
            elif v.strip() and not urlparse(_normalize_service_url(v)).netloc:
                bad(key, "must be a valid URL, or blank")
    for key in ("TAUTULLI_API_KEY", "PLEX_TOKEN", "RADARR_API_KEY", "JELLYFIN_API_KEY"):
        if key in saved and not isinstance(saved[key], str):
            bad(key, "must be a string (may be blank)")
    for key in ("PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS"):
        if key in saved:
            v = saved[key]
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                bad(key, "must be a list of collection names")
    if "RADARR_OVERSEERR_SECTION_ID" in saved:
        v = saved["RADARR_OVERSEERR_SECTION_ID"]
        if v is not None and not isinstance(v, (str, int)):
            bad("RADARR_OVERSEERR_SECTION_ID", 'must be a section ID, "auto", or null')
    if "OUTPUT_DIR" in saved and (not isinstance(saved["OUTPUT_DIR"], str) or not saved["OUTPUT_DIR"].strip()):
        bad("OUTPUT_DIR", "must be a directory path")
    if "TIME_ZONE" in saved:
        try:
            _validate_time_zone(saved["TIME_ZONE"])
        except ValueError:
            bad("TIME_ZONE", 'must be an IANA time zone or "auto"')
    if "DISPLAY_TIME_FORMAT" in saved:
        try:
            _validate_display_time_format(saved["DISPLAY_TIME_FORMAT"])
        except ValueError:
            bad("DISPLAY_TIME_FORMAT", 'must be "12h" or "24h"')
    if "DAILY_RUN_TIME" in saved:
        try:
            _validate_daily_run_time(saved["DAILY_RUN_TIME"])
        except ValueError:
            bad("DAILY_RUN_TIME", "must be a 24-hour HH:MM time")
    return issues



def _redline_only_mode_cfg(cfg: dict | None = None) -> bool:
    """True when Redline is the ONLY deletion trigger: the Headroom checkbox is
    unticked (REDLINE_ONLY_MODE), a Redline floor is set, AND no Library Size Cap is
    armed. Simulate maintains a standing preview of the deletion order and is always
    required before Live; the deletion delay does not apply. Unticking Headroom with a
    Library Size Cap is a DIFFERENT state — the cap runs on the daily schedule with the
    delay, so it is not redline-only (this returns False). A ticked Headroom at 0 GB is
    likewise not the mode — a normal config whose headroom trigger is off."""
    c = cfg if cfg is not None else load_config()
    return (bool(c.get("REDLINE_ONLY_MODE")) and c.get("REDLINE_GB") is not None
            and c.get("MAX_LIBRARY_GB") is None)


def _read_saved_config_file() -> dict | None:
    """Raw config.json content: {} when the file is missing (fresh install),
    None when it is unparseable or not a JSON object (corrupt → lockout)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError(f"config.json holds {type(saved).__name__}, not an object")
        return saved
    except Exception as e:
        print(f"WARNING: could not read {CONFIG_PATH} ({e}) — using defaults until it is fixed or re-saved.",
              file=sys.stderr)
        return None


_config_memo_lock = threading.Lock()
_config_memo = {"key": None, "cfg": None, "issues": []}


def _build_config() -> tuple[dict, list]:
    """Parse + validate + normalize config.json into the runtime cfg. Returns
    (cfg, issues); the caller memoizes and sets the _CONFIG_FILE_ISSUES global."""
    with open(DEFAULT_CFG_PATH) as f:
        cfg = json.load(f)
    # A corrupt/non-object config.json must not take every route down (including
    # the Config page needed to fix it): fall back to defaults, but record the
    # problem so runs and config edits lock until it's reset or fixed. save_config
    # is atomic, so this is only reachable via hand edits or disk trouble. Values
    # that break a GUI validation rule lock the same way.
    saved = _read_saved_config_file()
    if saved is None:
        issues = [{"key": "config.json", "message": "is not valid JSON"}]
    else:
        issues = _config_file_issues(saved)
        cfg.update(saved)
    cfg["SKIP_UNPLAYED_MOVIES"] = _coerce_bool(cfg.get("SKIP_UNPLAYED_MOVIES"))
    cfg["PROTECT_JELLYFIN_FAVORITES"] = _coerce_bool(cfg.get("PROTECT_JELLYFIN_FAVORITES"))
    cfg["USE_PLEX"] = _coerce_bool(cfg.get("USE_PLEX"))
    cfg["USE_JELLYFIN"] = _coerce_bool(cfg.get("USE_JELLYFIN"))
    _normalize_retention_scoring(cfg)
    _normalize_library_paths(cfg)
    cfg["CHECK_PATH"] = FILESYSTEM_CHECK_PATH
    cfg["TAUTULLI_APPDATA"] = TAUTULLI_APPDATA_DIR
    cfg["RADARR_APPDATA"] = RADARR_APPDATA_DIR
    cfg["JELLYFIN_APPDATA"] = JELLYFIN_APPDATA_DIR
    return cfg, issues


def load_config() -> dict:
    """The runtime config, memoized by config.json's (mtime, size): a single
    /api/status poll asks for it hundreds of times (every timestamp render calls
    it for the display format), and the file only changes on save. Each call
    returns a fresh copy so callers can mutate it freely; the parse + GUI-rule
    validation runs once per file version, not once per consult."""
    global _CONFIG_FILE_ISSUES
    try:
        st = CONFIG_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None   # missing file (fresh install) — a stable, memoizable state
    with _config_memo_lock:
        if _config_memo["key"] == key and _config_memo["cfg"] is not None:
            _CONFIG_FILE_ISSUES = _config_memo["issues"]
            return copy.deepcopy(_config_memo["cfg"])
    cfg, issues = _build_config()
    with _config_memo_lock:
        _config_memo.update({"key": key, "cfg": cfg, "issues": issues})
    _CONFIG_FILE_ISSUES = issues
    return copy.deepcopy(cfg)


def _invalid_config_response():
    """409 for config-mutating endpoints while config.json holds invalid hand edits:
    nothing may change (including API credentials) until the values are reset or
    MediaReducer is reset."""
    load_config()  # refresh _CONFIG_FILE_ISSUES from disk
    if _CONFIG_FILE_ISSUES:
        return jsonify({
            "ok": False,
            "invalid_config": _CONFIG_FILE_ISSUES,
            "error": "config.json contains invalid values — reset them on the "
                     "Configuration page (or reset MediaReducer) first.",
        }), 409
    return None


def save_config(cfg: dict, *, overwrite_invalid: bool = False) -> bool:
    """Atomically persist config.json (write tmp, rename over the target). Returns
    True when written.

    os.replace() is atomic on the same filesystem, so a container killed mid-write
    can't leave a half-written file that breaks every page load — readers always see
    the old or the new complete file. Writers are serialized by _config_io_lock and
    each uses a unique tmp name: concurrent saves do happen (e.g. the welcome popup's
    mark-seen POST racing the Config page's onboarding flag), and a shared tmp name
    would let one writer rename the file out from under another.

    Refused while the on-disk file holds invalid hand edits (saving would clear the
    lockout and silently replace the user's values with coerced ones); only the
    reset-invalid endpoint passes overwrite_invalid=True, and the full reset deletes
    the file instead.
    """
    with _config_io_lock:
        if not overwrite_invalid:
            saved = _read_saved_config_file()
            if saved is None or _config_file_issues(saved):
                print("WARNING: config.json holds invalid hand-edited values — save skipped "
                      "until they are reset.", file=sys.stderr)
                return False
        _atomic_write_json(CONFIG_PATH, cfg, indent=2)
    return True


def _atomic_write_json(path: Path, data, *, indent: int | None = None) -> None:
    """The one JSON write pattern for shared state files: a unique tmp then
    os.replace, so readers never see a torn file. The tmp name carries pid AND
    thread id — Flask threads and the engine subprocess can race on the same
    target, and two writers sharing a tmp path would interleave into garbage.
    Serialization (locks) is the caller's job; this only makes each write whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _radarr_section_method_label(method: str | None) -> str:
    method = str(method or "").strip()
    return RADARR_SECTION_METHOD_LABELS.get(method, method.replace("-", " ") if method else "")


def _clear_radarr_section_detection_cache(cfg: dict) -> None:
    for key in RADARR_SECTION_CACHE_KEYS:
        cfg.pop(key, None)


def _store_radarr_section_detection_cache(cfg: dict, detection: dict) -> bool:
    section_id = str(detection.get("section_id") or "").strip()
    if not detection.get("ok") or not section_id:
        return False

    section_name = str(detection.get("section_name") or "").strip()
    method = str(detection.get("method") or "").strip()
    method_label = str(detection.get("method_label") or _radarr_section_method_label(method)).strip()

    cfg["_RADARR_DETECTED_SECTION_ID"] = section_id
    if section_name:
        cfg["_RADARR_DETECTED_SECTION_NAME"] = section_name
    else:
        cfg.pop("_RADARR_DETECTED_SECTION_NAME", None)
    if method:
        cfg["_RADARR_DETECTED_SECTION_METHOD"] = method
    else:
        cfg.pop("_RADARR_DETECTED_SECTION_METHOD", None)
    if method_label:
        cfg["_RADARR_DETECTED_SECTION_METHOD_LABEL"] = method_label
    else:
        cfg.pop("_RADARR_DETECTED_SECTION_METHOD_LABEL", None)
    return True


def _radarr_section_detection_cache_incomplete(cfg: dict) -> bool:
    if not str(cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip():
        return False
    return not (
        str(cfg.get("_RADARR_DETECTED_SECTION_NAME") or "").strip()
        and str(cfg.get("_RADARR_DETECTED_SECTION_METHOD") or "").strip()
    )


def _has_saved_connection_credentials(cfg: dict | None = None) -> bool:
    """Return True once any saved Connection URL/API field has a value."""
    cfg = cfg or load_config()
    return any(str(cfg.get(name) or "").strip() for name in CONNECTION_CONFIG_FIELDS)


def _connection_onboarding_needed(cfg: dict | None = None) -> bool:
    """Show the first-run Config cue only for a fresh, never-configured instance."""
    cfg = cfg or load_config()
    if cfg.get(CONNECTION_ONBOARDING_SEEN_KEY) or cfg.get(CONNECTION_EVER_CONFIGURED_KEY):
        return False
    if _has_saved_connection_credentials(cfg):
        return False
    # config.json exists from first boot, so the persisted marker is the reliable
    # signal. Once Config has been opened or any connection value ever saved, this
    # stays False even if those values are later deleted.
    return True


def _mark_connection_onboarding_seen(cfg: dict | None = None) -> dict:
    """Persist that the user has visited Config so the first-run cue stops."""
    with _config_io_lock:
        cfg = dict(cfg or load_config())
        changed = False
        if not cfg.get(CONNECTION_ONBOARDING_SEEN_KEY):
            cfg[CONNECTION_ONBOARDING_SEEN_KEY] = True
            changed = True
        if _has_saved_connection_credentials(cfg) and not cfg.get(CONNECTION_EVER_CONFIGURED_KEY):
            cfg[CONNECTION_EVER_CONFIGURED_KEY] = True
            changed = True
        if changed:
            save_config(cfg)
        return cfg


def _welcome_guide_needed(cfg: dict | None = None) -> bool:
    """Show the first-run welcome/quick-start popup until dismissed once. The flag
    persists in config.json (surviving rebuilds) but returns after a full reset,
    which deletes config.json to restore the first-time state."""
    cfg = cfg or load_config()
    return not bool(cfg.get(WELCOME_GUIDE_SEEN_KEY))


def _mark_welcome_guide_seen() -> bool:
    with _config_io_lock:
        cfg = load_config()
        if not cfg.get(WELCOME_GUIDE_SEEN_KEY):
            return save_config(cfg | {WELCOME_GUIDE_SEEN_KEY: True})
    return True


def _preserve_connection_onboarding_flags(new_cfg: dict, old_cfg: dict | None = None) -> dict:
    """Keep first-run onboarding markers across normal Config saves."""
    old_cfg = old_cfg or load_config()
    if old_cfg.get(WELCOME_GUIDE_SEEN_KEY):
        new_cfg[WELCOME_GUIDE_SEEN_KEY] = True
    if old_cfg.get(CONNECTION_ONBOARDING_SEEN_KEY):
        new_cfg[CONNECTION_ONBOARDING_SEEN_KEY] = True
    if (
        old_cfg.get(CONNECTION_EVER_CONFIGURED_KEY)
        or _has_saved_connection_credentials(old_cfg)
        or _has_saved_connection_credentials(new_cfg)
    ):
        new_cfg[CONNECTION_EVER_CONFIGURED_KEY] = True
    return new_cfg


def force_paused_run_mode_on_startup():
    """Safety reset: every startup begins with automatic Live paused, so a restart
    never resumes a saved Live mode from the previous shutdown. Manual Dashboard runs
    are still allowed once checks pass. Connection URLs/keys are never auto-filled on
    startup — the Config page's Auto Detect button is the only appdata-to-field fill."""
    try:
        cfg = load_config()
        if cfg.get("RUN_MODE") != "paused":
            cfg["RUN_MODE"] = "paused"
            # Recorded so the dashboard/config can EXPLAIN the flip; a silent reset
            # read as "my Automatic Cleanup setting didn't stick". Cleared by the next config save
            # (the form never posts internal underscore keys).
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = "Automatic Cleanup is paused automatically after every restart."
            if save_config(cfg):
                print("Startup safety: RUN_MODE reset to paused.", flush=True)
    except Exception as e:
        print(f"WARNING: could not reset RUN_MODE to paused on startup: {e}", flush=True)


def disable_undersized_library_cap_on_startup():
    """Safety reset: an undersized cap (below the last-known library size) never
    survives a restart armed — the library may have grown while the app was down, and
    it would prune the moment Live is re-enabled. The cap is disabled but its value is
    kept (_MAX_LIBRARY_GB_LAST) so the Config field still shows it; re-enabling takes
    the usual two-click confirm."""
    try:
        cfg = load_config()
        cap = cfg.get("MAX_LIBRARY_GB")
        if cap is None:
            return
        library_gb = library_stats().get("library_gb")
        try:
            cap_f = float(cap)
            library_f = float(library_gb)
        except (TypeError, ValueError):
            return
        if library_f > cap_f > 0:
            cfg["_MAX_LIBRARY_GB_LAST"] = int(cap_f) if cap_f.is_integer() else cap_f
            cfg["MAX_LIBRARY_GB"] = None
            if save_config(cfg):
                print(f"Startup safety: Library Size Cap ({cap_f:g} GB) is below the "
                      f"last-known library size ({library_f:g} GB) — cap disabled, "
                      "value kept for re-enabling.", flush=True)
    except Exception as e:
        print(f"WARNING: could not check the Library Size Cap on startup: {e}", flush=True)


def output_dir() -> Path:
    return Path(load_config().get("OUTPUT_DIR", "/config"))


def db_path() -> Path:
    """The SQLite store shared with the engine (metadata cache, library
    snapshot, marked/eligible queue, kv meta)."""
    return output_dir() / "mediareducer.db"


def burn_daily_window_on_startup(reason: str = "startup") -> None:
    """Safety reset: stamp today as the last daily-cleanup date.

    The once-per-day window lives in the store, so a restart or cache wipe loses it,
    and a lost window would hand the scheduler a free immediate daily run when Live is
    re-armed. Burning it makes the first daily cleanup after a restart/clear TOMORROW's.
    Redline emergencies ignore the window and still fire."""
    today = time.strftime("%Y-%m-%d")
    try:
        with db.transaction(db_path()) as conn:
            if db.get_meta(conn, "last_cleanup_date") == today:
                return
            db.set_meta(conn, "last_cleanup_date", today)
        print(f"Startup safety: daily-run window marked used for today ({reason}).", flush=True)
    except Exception as e:
        print(f"WARNING: could not stamp the daily-run window ({e})", flush=True)


def reopen_daily_window(reason: str = "daily run time moved later") -> None:
    """Undo today's daily-window burn so a run time moved to a slot still ahead
    of the clock can fire again today. No-op unless the window is currently burned
    for today. Safe with the deletion delay: mark ages are calendar-day granular
    (see engine _mark_age_days), so a second run on the same day only re-marks
    candidates — no mark ages into eligibility between two runs on one day, so
    nothing extra is deleted."""
    today = time.strftime("%Y-%m-%d")
    try:
        with db.transaction(db_path()) as conn:
            if db.get_meta(conn, "last_cleanup_date") != today:
                return
            db.del_meta(conn, "last_cleanup_date")
        print(f"Daily-run window reopened for today ({reason}).", flush=True)
    except Exception as e:
        print(f"WARNING: could not reopen the daily-run window ({e})", flush=True)

def log_path()     -> Path: return output_dir() / "lastrun.log"
def deleted_path() -> Path: return output_dir() / "deleted.log"
def progress_path()-> Path: return output_dir() / "progress.json"
def logs_dir()     -> Path: return output_dir() / "logs"


def _archive_lastrun_log(out_dir: Path) -> None:
    """Move out_dir/lastrun.log into out_dir/logs/ under a timestamped name (the same
    naming the engine uses for archived cleanup runs), so the Dashboard run panel starts
    empty while the run's log is preserved — never deleted. A zero-byte/empty log is just
    removed. Best-effort: a failure here must never fail the caller. out_dir is passed in
    (not re-resolved via log_path()) so a caller mid-reset — with config.json already gone
    — still targets the pre-reset OUTPUT_DIR, and covers a logs/ folder on another mount."""
    last_log = out_dir / "lastrun.log"
    try:
        if last_log.exists() and last_log.stat().st_size > 0:
            archive_dir = out_dir / "logs"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            target = archive_dir / f"{stamp}.log"
            n = 1
            while target.exists():
                target = archive_dir / f"{stamp}_{n}.log"
                n += 1
            shutil.move(str(last_log), str(target))
        else:
            last_log.unlink(missing_ok=True)
    except OSError as e:
        print(f"WARNING: could not archive lastrun.log: {e}", flush=True)


def _clear_progress_state(out_dir: Path) -> None:
    """Delete progress.json and its writer temp files so the Dashboard run panel resets to
    'no runs yet' — api_run_progress() returns {} when the file is absent. Best-effort."""
    targets = [out_dir / "progress.json", out_dir / "progress.json.tmp"]
    try:
        targets.extend(out_dir.glob("progress.json.*.tmp"))  # pid-unique writer tmps
    except OSError:
        pass
    for p in targets:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            print(f"WARNING: could not clear progress state ({p.name}): {e}", flush=True)


_cache_file_memo_lock = threading.Lock()
_cache_file_memo = {"key": None, "data": {}}


def _cache_file_data() -> dict:
    """The whole store composed as one dict, memoized by a data
    fingerprint. One /api/status poll consults it several times (library stats,
    Live gating, the daily window) and every open page polls every ~3s, while the
    contents only change when a run, Summary, or window burn writes — so compose
    once per version, not once per consult. The fingerprint tracks the .db and
    its WAL sidecar, so a commit from the engine or an app write is picked up."""
    p = db_path()
    key = (str(p), db.data_fingerprint(p))
    with _cache_file_memo_lock:
        if _cache_file_memo["key"] == key:
            return _cache_file_memo["data"]
    try:
        data = db.read_cache_dict(p)
    except Exception:
        return {}
    if not isinstance(data, dict):
        data = {}
    with _cache_file_memo_lock:
        _cache_file_memo["key"] = key
        _cache_file_memo["data"] = data
    return data


def library_stats() -> dict:
    """Last-known dashboard storage stats (the store's dashboard_stats), written by
    Summary/run refreshes so the dashboard's frequent status poll reads cached values
    instead of touching the filesystem."""
    stats = _cache_file_data().get("dashboard_stats")
    return stats if isinstance(stats, dict) else {}

def cached_disk_stats(stats: dict | None = None) -> dict | None:
    """Last-known filesystem capacity, cache-only by design: /api/status polls often
    and must not call disk_usage() every few seconds. Fresh values are written to
    the store by the scheduler tick, config-triggered and manual Summary refreshes,
    and real runs."""
    stats = stats if isinstance(stats, dict) else library_stats()
    disk = stats.get("disk") if isinstance(stats, dict) else None
    if not isinstance(disk, dict):
        return None
    out = {}
    for key in ("used_gb", "total_gb", "free_gb", "pct_used"):
        try:
            out[key] = round(float(disk[key]), 1)
        except (KeyError, TypeError, ValueError):
            return None
    return out
def imdb_ratings_path() -> Path: return output_dir() / "title.ratings.tsv"


def _headroom_window_used_today() -> bool:
    """True when today's once-per-day daily cleanup (headroom or cap trigger)
    already ran. Mirrors the engine's check (local-time date vs the store's
    last_cleanup_date); only a Redline breach ignores the window."""
    try:
        return _cache_file_data().get("last_cleanup_date") == time.strftime("%Y-%m-%d")
    except Exception:
        return False


def _check_directory_read_write(path: Path, label: str) -> dict:
    """Return whether MediaReducer can create, list, write, read, and delete here."""
    # Unique per pid AND thread: two concurrent health checks sharing one probe name
    # could unlink each other's file between write and read — a spurious "cannot read
    # and write its config/log folders" that disables the run buttons.
    probe = path / f".mediareducer-rw-check.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return {"ok": False, "label": label, "path": str(path), "error": "path is not a folder"}
        list(path.iterdir())
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok":
            return {"ok": False, "label": label, "path": str(path), "error": "readback failed"}
        probe.unlink()
        return {"ok": True, "label": label, "path": str(path), "error": ""}
    except OSError as e:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "label": label, "path": str(path), "error": str(e)}


def _filesystem_rw_state() -> dict:
    """Health check for MediaReducer-owned folders under the configured output dir."""
    checks = [
        _check_directory_read_write(output_dir(), "Config folder"),
        _check_directory_read_write(logs_dir(), "Archived logs folder"),
    ]
    errors = [
        f"{item['label']} ({item['path']}): {item['error']}"
        for item in checks
        if not item.get("ok")
    ]
    return {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
    }

# ── Display time helpers ─────────────────────────────────────────────────────

_LOG_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?P<sep>\s+-\s+|\s+\|\s*)")
# ts | title | path, then optional size_bytes and rationale fields (score, plays,
# last_played) — the writer appends them best-effort, so any suffix may be absent.
# Each field is anchored by its key so the non-greedy path can't swallow the tail.
_DELETED_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\|\s+(?P<title>.*?)\s+\|\s+(?P<path>.*?)"
    r"(?:\s+\|\s+size_bytes=(?P<size_bytes>\d+))?"
    r"(?:\s+\|\s+score=(?P<score>[\d.]+))?"
    r"(?:\s+\|\s+plays=(?P<plays>\d+))?"
    r"(?:\s+\|\s+last_played=(?P<last_played>[^|]+?))?\s*$"
)

def _validate_time_zone(value: str | None) -> str:
    """Normalize the operating timezone. 'auto' means the container clock."""
    raw = str(value or "auto").strip()
    if not raw or raw.lower() == "auto":
        return "auto"
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        raise ValueError("Enter a valid IANA time zone such as America/Phoenix, or use auto.")
    return raw

def _apply_configured_time_zone(cfg: dict | None = None) -> bool:
    """Point the process clock at the configured zone. Everything keyed off local
    time — the once-per-day run window, deletion-delay aging, log timestamps —
    follows it. 'auto' restores the container clock. The engine subprocess inherits
    TZ from our environment and also applies the setting from config.json itself.
    Returns True when the effective zone changed."""
    try:
        name = _validate_time_zone((cfg if cfg is not None else load_config()).get("TIME_ZONE", "auto"))
    except ValueError:
        name = "auto"
    before = os.environ.get("TZ")
    if name == "auto":
        if _HOST_TZ is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = _HOST_TZ
    else:
        os.environ["TZ"] = name
    time.tzset()
    return os.environ.get("TZ") != before

def _validate_daily_run_time(value: str | None) -> str:
    """Time of day (24h HH:MM, operating zone) the daily cleanup may fire.
    Blank means midnight — the original hard-coded behavior."""
    raw = str(value or "").strip()
    if not raw:
        return "00:00"
    if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw):
        return raw
    raise ValueError("Daily run time must be a 24-hour HH:MM time, e.g. 03:30.")

def _validate_display_time_format(value: str | None) -> str:
    raw = str(value or "12h").strip().lower()
    if raw in ("12", "12h", "12-hour", "12 hour"):
        return "12h"
    if raw in ("24", "24h", "24-hour", "24 hour"):
        return "24h"
    raise ValueError("Time format must be 12h or 24h.")

def _request_time_format() -> str:
    # Callable outside a request (a background/test caller rendering timestamps):
    # fall back to the configured format when there is no query string to read.
    if has_request_context():
        requested = request.args.get("time_format") or request.args.get("fmt")
        if requested:
            try:
                return _validate_display_time_format(requested)
            except ValueError:
                pass
    return _validate_display_time_format(load_config().get("DISPLAY_TIME_FORMAT", "12h"))

def _format_dt_for_display(dt: datetime, fmt: str | None = None) -> str:
    fmt = _validate_display_time_format(fmt or _request_time_format())
    if fmt == "24h":
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    # Linux supports %-I. Fall back to stripping a leading zero from %I.
    try:
        return dt.strftime("%Y-%m-%d %-I:%M:%S %p")
    except ValueError:
        s = dt.strftime("%Y-%m-%d %I:%M:%S %p")
        return s.replace(" 0", " ", 1)

def _format_epoch_for_display(epoch_seconds: float | int | None) -> str | None:
    if epoch_seconds is None:
        return None
    # Local time IS the operating zone — TIME_ZONE is applied to the process.
    return _format_dt_for_display(datetime.fromtimestamp(float(epoch_seconds)))

def _format_log_timestamp_for_display(ts: str, fmt: str | None = None) -> str:
    """Re-render a script log timestamp in the configured 12/24-hour format.
    Log timestamps are already written in the operating time zone. Pass fmt to
    reuse a format resolved once when rendering many timestamps in a loop."""
    try:
        return _format_dt_for_display(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), fmt)
    except Exception:
        return ts

def _format_log_text_for_display(text: str) -> str:
    """Re-render leading log timestamps in the configured 12/24-hour format."""
    fmt = _request_time_format()

    def convert_line(line: str) -> str:
        m = _LOG_TS_RE.match(line)
        if not m:
            return line
        try:
            naive = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            return _format_dt_for_display(naive, fmt) + m.group("sep") + line[m.end():]
        except Exception:
            return line

    return "".join(convert_line(line) for line in text.splitlines(keepends=True))

def _format_reclaimed_size(size_bytes: int | float | None) -> str:
    """Compact display for lifetime space reclaimed from deleted.log size_bytes."""
    try:
        n = max(0, int(size_bytes or 0))
    except (TypeError, ValueError):
        n = 0
    gb = n / 1_000_000_000
    if gb >= 1000:
        return f"{gb / 1000:.1f} TB"
    if gb >= 10:
        return f"{gb:.1f} GB"
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = n / 1_000_000
    if mb >= 1:
        return f"{mb:.0f} MB"
    return "0 GB"

def _api_connection_error(cfg: dict | None = None) -> bool:
    """True when the LAST connection probe found API problems.

    Reads only cached health (never probes), so it is cheap for the header on every
    page. The signature check matters: Check for Errors can probe UNSAVED form values,
    and a failure there must not paint the tab red on every page after the user
    discards the edit."""
    with _connection_health_cache_lock:
        cached = _connection_health_cache.get("health")
        cached_sig = _connection_health_cache.get("signature")
    if not isinstance(cached, dict):
        return False
    if cached_sig is not None and cached_sig != _connection_health_signature(cfg or load_config()):
        return False
    return bool(cached.get("errors")) or not cached.get("critical_ok", True)


@app.context_processor
def inject_display_time_settings():
    cfg = load_config()
    return {
        "display_time_format": cfg.get("DISPLAY_TIME_FORMAT", "12h"),
        "server_time_zone": _server_time_zone_name(),
        "host_time_zone": _host_time_zone_name(),
        "server_epoch": time.time(),
        "connection_onboarding_needed": _connection_onboarding_needed(cfg),
        "api_connection_error": _api_connection_error(cfg),
        "debug_mode": bool(cfg.get("DEBUG_MODE")),
        "welcome_needed": _welcome_guide_needed(cfg),
        # First-launch only: adopt the browser's time zone (client posts it to
        # /api/timezone/init). True only on a brand-new install still on "auto"
        # that hasn't detected yet and has never configured connections — so an
        # existing install or a deliberate "auto" is never overwritten.
        "time_zone_needs_init": (not cfg.get("_TIME_ZONE_AUTODETECTED")
                                 and not cfg.get("_CONNECTIONS_EVER_CONFIGURED")
                                 and str(cfg.get("TIME_ZONE", "auto")).strip().lower() == "auto"),
    }


@app.route("/api/timezone/init", methods=["POST"])
def api_timezone_init():
    """First-launch only: adopt the browser-detected IANA time zone as the
    TIME_ZONE setting and persist it, so a fresh install lands on the user's
    zone instead of the container's (usually UTC).

    One-shot and heavily guarded: no-ops once _TIME_ZONE_AUTODETECTED is set, if
    connections were ever configured, or if TIME_ZONE is no longer "auto" — so a
    deliberate choice or an existing install is never overwritten. Keeps "auto"
    if the posted zone is missing or invalid. Only touches TIME_ZONE + the flag;
    not the full config-save path."""
    cfg = load_config()
    already = (cfg.get("_TIME_ZONE_AUTODETECTED")
               or cfg.get("_CONNECTIONS_EVER_CONFIGURED")
               or str(cfg.get("TIME_ZONE", "auto")).strip().lower() != "auto")
    if already:
        return jsonify({"ok": True, "already": True, "time_zone": cfg.get("TIME_ZONE", "auto")})
    if _run_active:
        # Never rewrite config mid-run; the next page load retries.
        return jsonify({"ok": False, "deferred": True})
    tz = str((request.get_json(silent=True) or {}).get("tz") or "").strip()
    try:
        applied = _validate_time_zone(tz) if tz else "auto"
    except ValueError:
        applied = "auto"   # unknown browser zone — stay on the container clock
    cfg["TIME_ZONE"] = applied
    cfg["_TIME_ZONE_AUTODETECTED"] = True
    save_config(cfg)
    if _apply_configured_time_zone(cfg):
        burn_daily_window_on_startup("timezone auto-detect")
    return jsonify({"ok": True, "time_zone": applied})

# ── Run manager ───────────────────────────────────────────────────────────────

# All in-memory: a restart resets every flag (no run survives a container stop).
_run_lock      = threading.Lock()
_run_active    = False
_run_cleanup      = False         # True while the active run is a Live (deleting) run, not a simulation
_run_debug_cleanup = False        # True while the active run is a Debug Cleanup (live path, no deletions)
_run_start     = None          # datetime
_run_process   = None          # subprocess.Popen
_run_stop_requested = threading.Event()
_shutting_down = False         # a container/app shutdown signal is being handled
_summary_active = False        # background Summary (debug_info) stats refresh in progress
_summary_queued = False        # coalesced follow-up Summary requested while one is active
# Today's daily-run epoch we last spun a paused-mode maintenance Simulate for, so a
# scan that fails past the connection probe (leaving the snapshot stale) isn't respun
# every ~15-min tick — it retries next window / on restart / on a manual Simulate.
_last_paused_scan_epoch = None

def _pause_scheduler_for_run():
    """Freeze the single background clock while a run or Summary is active."""
    try:
        scheduler.pause_job("engine")
    except Exception:
        # Scheduler may not be initialised yet during import/startup.
        pass

def _restart_schedule_clock():
    """Restart the single background clock from zero: a run or Summary just finished,
    so the info is fresh and the next tick should be a full interval away.
    Rescheduling also un-pauses the job if it was paused for the run. Best-effort —
    never let a scheduler hiccup escape into a worker's finally block."""
    try:
        scheduler.reschedule_job(
            "engine", trigger="interval", minutes=SCHEDULE_INTERVAL_MINUTES,
        )
    except Exception:
        pass

def _write_progress_start_stub(mode_override: str | None):
    """Reset progress.json to a 'starting' state the instant a run is launched, so the
    dashboard panel flips immediately — even before the subprocess has imported Python.
    The engine then overwrites this with real phase updates. Best-effort only."""
    try:
        now = time.time()
        _atomic_write_json(progress_path(), {
            "schema": 1, "status": "starting", "phase": "checking",
            "mode": mode_override or load_config().get("RUN_MODE"),
            "scanned": 0, "total": 0, "eligible": 0, "protected": 0, "skipped": 0,
            "deleted": 0, "bytes_freed": 0, "target_bytes": 0,
            "trigger": "", "current_title": "", "message": "Starting…",
            "started_at": now, "updated_at": now,
        })
    except Exception:
        pass


def _mark_progress_terminal(status: str, message: str, *, force: bool = False):
    """Best-effort terminal progress marker for stops/crashes. The engine owns normal
    progress updates; this only fills the gap when it is terminated or exits before
    writing a terminal state, preserving the last phase so the dashboard can mark the
    exact stopped/failed stage."""
    try:
        p = progress_path()
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if not force and data.get("status") in ("done", "stopped", "error"):
            return
        now = time.time()
        data.update({
            "schema": data.get("schema", 1),
            "status": status,
            "phase": data.get("phase") or "checking",
            "message": message,
            "updated_at": now,
            "ended_at": now,
            })
        data.setdefault("started_at", now)
        _atomic_write_json(p, data)
    except Exception:
        pass


def _preview_rebuild_needed(cleanup_run: bool) -> bool:
    """Should a background Simulate rebuild the standing queue after a run?

    Two triggers: the engine's queue_rebuild progress flag (a Redline fast path
    consumed marks), or — the invariant that catches every other path — any LIVE
    run in redline-only mode that actually deleted something (manual Cleanups
    and full-scan Redline fallbacks trim the queue too, and the mode has no
    daily runs to replenish it). Sim runs never qualify: the Simulate IS the
    rebuild, so they'd loop forever."""
    try:
        prog = json.loads(progress_path().read_text(encoding="utf-8"))
        if prog.get("queue_rebuild"):
            return True
        deleted_any = (prog.get("deleted") or 0) > 0
    except Exception:
        deleted_any = False
    if not cleanup_run:
        return False
    try:
        return _redline_only_mode_cfg() and deleted_any
    except Exception:
        return False


def _maybe_rebuild_preview_after_run(cleanup_run: bool = False) -> None:
    """After a successful run, rebuild the standing preview when needed (see
    _preview_rebuild_needed): kick a background Simulate so it grows back to
    full strength. The post-run Summary owns the lock briefly, so retry for a
    while rather than failing; any new engine run rewrites progress.json, which
    clears the fast path's flag."""
    if not _preview_rebuild_needed(cleanup_run):
        return

    def _kick():
        # The post-run Summary can legitimately take minutes on a large library
        # (its own subprocess budget is 600 s) — wait past that, not under it.
        for _ in range(140):           # up to ~11½ minutes
            time.sleep(5)
            if _run_active or _summary_active:
                continue
            # In headroom mode a within-limits Simulate would spin up just to
            # no-op ("nothing to simulate") — the daily run replans anyway.
            # Redline-only mode always rebuilds (the preview IS the plan).
            cfg = load_config()
            if not _redline_only_mode_cfg(cfg):
                try:
                    if not _deletion_limits_exceeded(cfg, disk_stats(),
                                                     library_stats().get("library_gb")):
                        print("Queue rebuild: skipped — limits are satisfied and the "
                              "daily run replans on the next breach.", flush=True)
                        return
                except Exception:
                    pass   # can't judge → let the Simulate decide
            ok, msg = run_script(mode_override="debug_sim")
            print(("Queue rebuild: background Simulate started to restore the marked "
                   "preview after the Redline fast path."
                   if ok else f"Queue rebuild: could not start the Simulate ({msg})."),
                  flush=True)
            return
        print("Queue rebuild: gave up waiting for the post-run refresh to finish.", flush=True)

    threading.Thread(target=_kick, daemon=True, name="queue-rebuild").start()


def run_script(mode_override: str | None = None, manual: bool = False) -> tuple[bool, str]:
    """Launch engine.py as a subprocess. manual=True marks a Dashboard-button run:
    a manual Cleanup prunes every breached target immediately — the deletion delay
    and once-per-day window pace automatic runs only. Returns (started, message)."""
    global _run_active, _run_cleanup, _run_debug_cleanup, _run_start

    with _run_lock:
        if _run_active:
            return False, "A run is already in progress."
        if _summary_active:
            return False, "A background status refresh is finishing — try again in a moment."
        _run_stop_requested.clear()
        _effective_mode = mode_override or load_config().get("RUN_MODE")
        _run_active  = True
        _run_cleanup    = _is_cleanup_mode(_effective_mode)
        _run_debug_cleanup = (_effective_mode == "debug_cleanup")
        _run_start   = datetime.now()
        _pause_scheduler_for_run()
        _write_progress_start_stub(mode_override)

    def _worker():
        global _run_active, _run_process
        try:
            env = os.environ.copy()
            env["MEDIAREDUCER_CONFIG"] = str(CONFIG_PATH)
            if mode_override:
                env["MEDIAREDUCER_MODE_OVERRIDE"] = mode_override
            if manual:
                env["MEDIAREDUCER_MANUAL"] = "1"

            if _run_stop_requested.is_set():
                return

            proc = subprocess.Popen(
                ["python3", "-u", str(SCRIPT_PATH)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with _run_lock:
                _run_process = proc
                if _run_stop_requested.is_set() and proc.poll() is None:
                    proc.terminate()
            returncode = proc.wait()
            stopped = _run_stop_requested.is_set()
            if stopped:
                _mark_progress_terminal("stopped", "Run stopped.", force=True)
            elif returncode == 0:
                # A completed Redline fast path asks for its preview to be rebuilt;
                # any cleanup in redline-only mode that thinned the preview does too.
                _maybe_rebuild_preview_after_run(
                    cleanup_run=_is_cleanup_mode(mode_override or load_config().get("RUN_MODE")))
            elif returncode != 0:
                _mark_progress_terminal("error", "Run failed — see the detailed log.")
                # Runs fail closed on any API error, so re-probe now: the cached
                # health drives the red Configuration tab, the jump-to-Connections
                # link, and the per-field error highlights.
                threading.Thread(
                    target=lambda: _refresh_connection_health_cache(load_config(), probe=True),
                    daemon=True, name="engine-postfail-health",
                ).start()
        except Exception as e:
            # A failed launch (Popen OSError etc.) must not vanish in the daemon
            # thread — mark the run terminal so the dashboard shows the failure
            # instead of a phantom active run.
            _mark_progress_terminal("error", f"Could not launch the run: {e}", force=True)
        finally:
            with _run_lock:
                _run_active  = False
                _run_process = None
                _run_stop_requested.clear()
            # Only a Live (deleting) run leaves storage stats stale: it reads the
            # library size before deleting and never recomputes it, and even a stop
            # can have removed files first. So kick a quiet Summary to refresh the
            # size (it also restarts the clock when it lands). A simulation deletes
            # nothing and writes fresh stats during its own pass, so it just needs
            # the clock restarted.
            effective_mode = mode_override or load_config().get("RUN_MODE")
            if _is_cleanup_mode(effective_mode):
                run_summary()
            else:
                _restart_schedule_clock()

    threading.Thread(target=_worker, daemon=True, name="engine-run").start()
    return True, "Run started."

def stop_script():
    with _run_lock:
        if not _run_active:
            return False
        _run_stop_requested.set()
        proc = _run_process
    _mark_progress_terminal("stopped", "Run stopped — deletions already made are permanent.", force=True)
    if proc and proc.poll() is None:
        proc.terminate()
    return True


def _graceful_shutdown(signum, frame):
    """Container/app stop (SIGTERM/SIGINT to PID 1): forward the stop to an active
    deletion run so the engine finishes its current file's unlink→deleted.log record
    and archives its partial log (the same clean path as the web Stop button) before
    the app exits.

    Without this, PID 1 exits on the signal and the kernel SIGKILLs the still-running
    engine child mid delete-and-record. We wait a few seconds (well inside Docker's
    default 10s stop grace) for the engine to exit; if it doesn't, we fall through and
    let the container's own SIGKILL take it."""
    global _shutting_down
    if _shutting_down:
        # A second signal (or an impatient orchestrator) — exit now.
        raise SystemExit(0)
    _shutting_down = True
    proc = _run_process
    if _run_active and proc is not None and proc.poll() is None:
        print("Shutdown: stopping the active run cleanly before exit…", flush=True)
        try:
            stop_script()                       # SIGTERM → engine's graceful path
        except Exception as e:
            print(f"Shutdown: stop_script failed ({e}); terminating engine directly.", flush=True)
            try:
                proc.terminate()
            except Exception:
                pass
        # poll() (not wait()) so we don't race the worker thread's own wait(): the
        # worker reaps the child and poll() then sees the cached returncode.
        deadline = time.time() + 8
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        print("Shutdown: engine exited; app shutting down.", flush=True)
    raise SystemExit(0)


def _run_summary_subprocess(config_path: Path, timeout: int = 600) -> tuple[bool, str]:
    # 600s: the summary walks every monitored dir — a large library on spinning/
    # network storage can legitimately take minutes, and 120s died mid-walk, blocking
    # run prechecks and dashboard stats.
    """Run engine.py in quiet debug_info mode against config_path."""
    env = os.environ.copy()
    env["MEDIAREDUCER_CONFIG"] = str(config_path)
    env["MEDIAREDUCER_MODE_OVERRIDE"] = "debug_info"
    proc = subprocess.run(
        ["python3", "-u", str(SCRIPT_PATH)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return False, "Summary refresh failed."
    return True, "Summary refreshed."


def run_summary() -> tuple[bool, str]:
    """Run a quiet Summary (debug_info) in the background to refresh the store
    dashboard stats.

    Same unified-clock rules as run_script (pauses the clock while working, restarts
    from zero when done), but never writes a progress stub or streams the log —
    debug_info leaves lastrun.log and the progress panel intact. Shares the run lock
    so it can never overlap a real run (and vice versa), which lets the dashboard
    ghost the run buttons while it is in flight."""
    global _summary_active, _summary_queued
    with _run_lock:
        if _run_active or _summary_active:
            if _summary_active and not _run_active:
                _summary_queued = True
                return True, "Summary refresh queued."
            return False, "A run or storage refresh is in progress — stats update when it finishes."
        _summary_active = True
    _pause_scheduler_for_run()  # freeze the clock while stats refresh

    def _worker():
        global _summary_active, _summary_queued
        while True:
            try:
                _run_summary_subprocess(CONFIG_PATH)
            except Exception:
                pass
            with _run_lock:
                if _summary_queued and not _run_active:
                    _summary_queued = False
                    continue
                # A queue abandoned because a real run started is dropped, not left
                # latched: the run refreshes the stats the queued summary wanted, and a
                # stale flag would fire one spurious summary after a LATER refresh.
                _summary_queued = False
                _summary_active = False
                break
        _restart_schedule_clock()  # restart the clock from zero

    threading.Thread(target=_worker, daemon=True, name="engine-summary").start()
    return True, "Summary started."


def run_summary_sync(timeout: int = 600) -> tuple[bool, str, dict]:
    """Run Summary/debug_info now and return the freshly cached dashboard stats."""
    global _summary_active, _summary_queued
    with _run_lock:
        if _run_active:
            return False, "A run is active. Try again when it finishes.", {}
        if _summary_active:
            return False, "A background storage refresh is already running. Try again in a moment.", {}
        _summary_active = True
    _pause_scheduler_for_run()

    try:
        ok, msg = _run_summary_subprocess(CONFIG_PATH, timeout=timeout)
        return ok, msg, library_stats()
    except subprocess.TimeoutExpired:
        return False, "Timed out while refreshing storage stats.", {}
    except Exception as e:
        return False, str(e), {}
    finally:
        with _run_lock:
            _summary_active = False
            # Consume a summary queued while this sync one held the flag —
            # run_summary() answered its caller "queued", so it must actually happen.
            queued = _summary_queued and not _run_active
            _summary_queued = False
        _restart_schedule_clock()
        if queued:
            run_summary()
        _maybe_launch_queued_reconcile()


# ── Config-save reconcile launcher ────────────────────────────────────────────
# A saved filtering / scoring / threshold / collections / favorites change rebuilds
# the marked & eligible queue in place from the stored snapshot (the engine's quiet
# `reconcile` mode) instead of leaving it stale until the next Simulate. It's a
# Summary-class background job: it shares _summary_active so it can never overlap a
# real run or a Summary, and it queues behind one that's already in flight.
_reconcile_queued = None   # (refetch: bool, trigger: str) waiting for the active job
_reconcile_held = None      # a refetch reconcile held because a needed server was down

def _run_reconcile_subprocess(config_path: Path, *, refetch: bool,
                              trigger: str, timeout: int = 600) -> bool:
    """Run engine.py in the quiet `reconcile` mode against config_path."""
    env = os.environ.copy()
    env["MEDIAREDUCER_CONFIG"] = str(config_path)
    env["MEDIAREDUCER_MODE_OVERRIDE"] = "reconcile"
    env["MEDIAREDUCER_RECONCILE_REFETCH"] = "1" if refetch else "0"
    env["MEDIAREDUCER_RECONCILE_TRIGGER"] = trigger
    proc = subprocess.run(
        ["python3", "-u", str(SCRIPT_PATH)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    return proc.returncode == 0


def _maybe_launch_queued_reconcile() -> None:
    """Fire a reconcile that was queued while a Summary/run held _summary_active."""
    global _reconcile_queued
    with _run_lock:
        nxt = _reconcile_queued
        _reconcile_queued = None
    if nxt and not _run_active:
        run_reconcile(refetch=nxt[0], trigger=nxt[1])


def _reconcile_worker(refetch: bool, trigger: str) -> None:
    global _summary_active
    try:
        _run_reconcile_subprocess(CONFIG_PATH, refetch=refetch, trigger=trigger)
    except Exception:
        pass
    with _run_lock:
        _summary_active = False
    _restart_schedule_clock()
    _maybe_launch_queued_reconcile()   # a reconcile requested while this one ran


def run_reconcile(*, refetch: bool, trigger: str) -> None:
    """Launch a background queue reconcile. If a run/Summary is in flight, queue it
    to fire on completion (the refetch flag is sticky — if either the queued or the
    new request needs a server lookup, the coalesced one does)."""
    global _summary_active, _reconcile_queued
    with _run_lock:
        if _run_active or _summary_active:
            prev = _reconcile_queued
            _reconcile_queued = ((prev[0] if prev else False) or refetch, trigger)
            return
        _summary_active = True
    _pause_scheduler_for_run()
    threading.Thread(target=_reconcile_worker, args=(refetch, trigger),
                     daemon=True, name="engine-reconcile").start()


# Plan-affecting settings that reconcile in place on save. PURE keys recompute from
# the snapshot with no server call; REFETCH keys change a protection SOURCE, so the
# reconcile re-fetches the live protected/favorite sets first.
_RECONCILE_PURE_KEYS = (
    "SCORE_BALANCE", "GRACE_PERIOD_DAYS", "MAX_IMDB_RATING", "NEAR_TIE_PTS",
    "MAX_STALENESS_MONTHS", "SKIP_UNPLAYED_MOVIES",
    "HEADROOM_GB", "REDLINE_GB", "REDLINE_ONLY_MODE", "MAX_LIBRARY_GB",
)
_RECONCILE_REFETCH_KEYS = (
    "PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS", "PROTECT_JELLYFIN_FAVORITES",
)


def _plan_value_changed(a, b) -> bool:
    """Compare two config values the way the plan cares about — order-insensitive
    for the collection lists, direct for scalars."""
    def _norm(v):
        if isinstance(v, (list, tuple)):
            return sorted(str(x) for x in v)
        return v
    return _norm(a) != _norm(b)


def _reconcile_after_save(old_cfg: dict, new_cfg: dict, *, source: str) -> str:
    """After a plan-affecting save, rebuild the marked & eligible queue in place from
    the stored snapshot (no rescan) instead of leaving it stale until the next
    Simulate. Returns "started", "held_connection", or "none".

    Pure filter/scoring/threshold changes reconcile with no server call. A
    protected-collection or Jellyfin-favorites change re-fetches the live sets; if
    the server it needs is unreachable, the reconcile is HELD and the Connections
    check flags that server (fix it, or disable the server — a disabled server has
    no collections/favorites to honor), with Cleanup/arming blocked meanwhile."""
    global _reconcile_held
    pure = any(_plan_value_changed(old_cfg.get(k), new_cfg.get(k)) for k in _RECONCILE_PURE_KEYS)
    refetch = any(_plan_value_changed(old_cfg.get(k), new_cfg.get(k)) for k in _RECONCILE_REFETCH_KEYS)
    if not (pure or refetch) or not _has_monitored_dirs(new_cfg):
        return "none"
    if refetch and not _refresh_connection_health_cache(new_cfg, probe=True).get("critical_ok", False):
        # Hold it: the Connections check (just refreshed) flags the down server, and
        # _retry_held_reconcile fires this once the connection recovers or the server
        # is disabled. Coalesce with any earlier held request (either wanting a
        # refetch keeps it).
        prev = _reconcile_held
        _reconcile_held = ((prev[0] if prev else False) or refetch, source)
        return "held_connection"
    _reconcile_held = None            # a change that reconciled supersedes a held one
    run_reconcile(refetch=refetch, trigger=source)
    return "started"


def _retry_held_reconcile(cfg: dict | None = None) -> bool:
    """Fire a reconcile that was held because a needed server was down, once the
    connection is healthy again (the user fixed it) or the server was disabled (a
    disabled server has no collections/favorites, so a pure recompute is enough).
    Called from the scheduler tick, so recovery is automatic within ~15 minutes; a
    re-save or a passing Connections check applies it sooner. Returns True if it
    launched a reconcile."""
    global _reconcile_held
    if not _reconcile_held:
        return False
    cfg = cfg or load_config()
    if not _has_monitored_dirs(cfg):
        _reconcile_held = None
        return False
    refetch, trigger = _reconcile_held
    # If both servers are off there's nothing to re-fetch — a pure recompute applies
    # the change; otherwise require the connection to be healthy before re-fetching.
    needs_server = refetch and (cfg.get("USE_PLEX") or cfg.get("USE_JELLYFIN"))
    if needs_server and not _refresh_connection_health_cache(cfg, probe=True).get("critical_ok", False):
        return False
    _reconcile_held = None
    run_reconcile(refetch=needs_server, trigger=trigger)
    return True


# ── IMDb dataset helpers ──────────────────────────────────────────────────────
# Download ceilings for the IMDb dataset (real archive ~10 MB, unpacks to ~25 MB):
# pure guardrails so a wrong URL or crafted archive can't balloon into memory.
_IMDB_GZ_MAX_BYTES = 64 * 1024 * 1024
_IMDB_TSV_MAX_BYTES = 512 * 1024 * 1024


def _download_imdb_gz(url: str) -> bytes:
    """Fetch and decompress the ratings archive with hard size caps on both
    the download and its decompressed output."""
    with urllib.request.urlopen(url, timeout=120) as resp:
        gz_data = resp.read(_IMDB_GZ_MAX_BYTES + 1)
    if len(gz_data) > _IMDB_GZ_MAX_BYTES:
        raise ValueError("IMDb ratings download exceeded the size limit.")
    tsv_data = gzip.GzipFile(fileobj=io.BytesIO(gz_data)).read(_IMDB_TSV_MAX_BYTES + 1)
    if len(tsv_data) > _IMDB_TSV_MAX_BYTES:
        raise ValueError("IMDb ratings archive decompressed beyond the size limit.")
    return tsv_data


def _read_library_snapshot():
    """The full-library snapshot the last completed scan stored under
    the store's library_snapshot — the Filtering & Scoring page's
    table. Returns (payload, None) or (None, "missing"). Reads through the
    memoized cache parse — the arming gate consults this on the status-poll
    path, and composing the whole store (all movie metadata + the snapshot) is
    far too big to redo every 3 seconds."""
    snap = _cache_file_data().get("library_snapshot")
    if not isinstance(snap, dict):
        return None, "missing"
    return snap, None


def _simulate_evidence(cfg: dict) -> bool:
    """Proof that a Simulate (or any completed scan) has already seen THIS
    library: the snapshot every completed scan writes, stamped with the paths
    it scanned. Used by the arming gate when space limits are satisfied — the
    eligible queue can legitimately be empty then, so the plan stamp alone
    can't prove a Simulate happened. A snapshot built from different monitored
    paths is not evidence for the current ones."""
    snap, err = _read_library_snapshot()
    if err is not None or not snap.get("built_at"):
        return False
    dirs = snap.get("monitor_dirs")
    return (isinstance(dirs, list)
            and sorted(str(d) for d in dirs) == _normalized_monitor_dirs(cfg))


# ── "A full scan is required every 48 hours" ──────────────────────────────────
# The store is PRESERVED across a restart, so an unchanged, recent plan stays
# usable with no re-scan. But a full library scan (a Simulate, or the automatic
# daily Cleanup / paused daily maintenance Simulate, which rebuild the whole
# snapshot) must have run within the last two days — otherwise the plan is treated
# as too old to trust and Cleanup + arming ghost until a fresh Simulate. A daily
# full scan normally keeps this satisfied with a full day of slack (so moving the
# daily run time later never trips it); if scans stop for two days (e.g. the APIs
# are unreachable) the plan ages out and the user runs Simulate manually.
# Evaluated live off the snapshot's built_at — no persistent flag: a completed
# scan refreshes built_at and clears it automatically.
_FULL_SCAN_MAX_AGE_SECONDS = 48 * 3600

_SCAN_OVERDUE_MESSAGE = (
    "It's been over two days since MediaReducer last scanned your whole library, so "
    "the saved deletion plan may be out of date — run Simulate to refresh it "
    "before running a Cleanup or enabling automatic mode. (A daily scan normally "
    "does this for you.)")


def _full_scan_overdue() -> bool:
    """True when a full library scan exists but completed over
    _FULL_SCAN_MAX_AGE_SECONDS ago. False when there's no snapshot at all — the
    first-time 'run a Simulate' case is handled by the plan/evidence logic with
    its own message. Cheap: reads the snapshot's built_at through the memoized
    store parse."""
    snap, err = _read_library_snapshot()
    if err is not None:
        return False
    built_at = snap.get("built_at")
    if not isinstance(built_at, (int, float)):
        return False
    return (time.time() - float(built_at)) > _FULL_SCAN_MAX_AGE_SECONDS


def _todays_daily_run_epoch(cfg: dict | None = None):
    """Epoch of today's configured daily run time (DAILY_RUN_TIME) in the app's
    local/configured timezone, or None if unparseable."""
    try:
        hh, mm = (int(x) for x in _daily_run_time(cfg).split(":"))
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0,
                            now.tm_wday, now.tm_yday, -1))
    except (ValueError, AttributeError, OverflowError):
        return None


def _paused_daily_scan_due(cfg: dict) -> bool:
    """True when a PAUSED schedule should run its once-a-day maintenance Simulate:
    the configured daily run time has passed and no full library scan has completed
    since then (the snapshot's built_at predates today's run time). This keeps the
    store current — refreshing the plan and the 'full scan within 48h' clock —
    even when no automatic Cleanup is armed to do it. Requires an existing snapshot
    (a prior Simulate); a fresh, never-scanned install keeps its manual-Simulate
    onboarding instead of auto-scanning."""
    snap, err = _read_library_snapshot()
    if err is not None:
        return False   # nothing scanned yet — leave onboarding's manual Simulate to the user
    run_epoch = _todays_daily_run_epoch(cfg)
    if run_epoch is None or time.time() < run_epoch:
        return False   # before today's scheduled time
    built_at = snap.get("built_at")
    if not isinstance(built_at, (int, float)):
        return True
    return float(built_at) < run_epoch   # no full scan since today's run time


# ── Disk / status helpers ─────────────────────────────────────────────────────

def disk_stats() -> dict | None:
    try:
        u = shutil.disk_usage(FILESYSTEM_CHECK_PATH)
        used_gb  = round(u.used  / 1e9, 1)
        total_gb = round(u.total / 1e9, 1)
        free_gb  = round(u.free  / 1e9, 1)
        pct_used = round(used_gb / total_gb * 100, 1) if total_gb else 0
        return {"used_gb": used_gb, "total_gb": total_gb,
                "free_gb": free_gb, "pct_used": pct_used}
    except Exception:
        return None


def _coerce_float(value) -> tuple[float | None, bool]:
    """Return (number, ok)."""
    if value is None:
        return None, False
    try:
        return float(value), True
    except (TypeError, ValueError):
        return None, False



def _is_cleanup_mode(mode: str | None) -> bool:
    return mode == "headroom"


def _ui_run_mode(mode: str | None) -> str:
    """Snap any stored value to the GUI's two modes: paused/live."""
    return "headroom" if _is_cleanup_mode(mode) else "paused"


def _threshold_gb_or_none(value):
    """A positive GB threshold as a float, or None when unset/invalid/zero."""
    if value is None or value == "":
        return None
    num, ok = _coerce_float(value)
    return num if ok and num is not None and num > 0 else None


def _space_threshold_state(cfg: dict | None = None, disk: dict | None = None,
                           library_gb=None, *, candidate_cfg: bool = False) -> dict:
    """Validate Space Thresholds for Simulate and cleanup runs.

    Simulate is deliberately allowed when HEADROOM_GB is above the safety percentage,
    or when the Library Size Cap would delete more than that percentage of the library,
    so users can preview the deletion order. Live is blocked by either condition. Other
    malformed threshold values block even Simulate — they make the run ambiguous."""
    cfg = cfg or load_config()
    disk = disk if disk is not None else disk_stats()
    if library_gb is None:
        library_gb = library_stats().get("library_gb")

    hard_errors: list[str] = []
    safety_errors: list[str] = []

    headroom_gb, headroom_ok = _coerce_float(cfg.get("HEADROOM_GB"))
    if not headroom_ok or headroom_gb is None or headroom_gb < 0:
        hard_errors.append("Headroom must be zero or greater.")
        headroom_ok = False

    max_pct, max_pct_ok = _coerce_float(cfg.get("MAX_HEADROOM_PCT", 15))
    if not max_pct_ok or max_pct is None or max_pct <= 0:
        hard_errors.append("Headroom safety percentage must be greater than zero.")
        max_pct_ok = False

    redline_gb = None
    redline_ok = True
    if cfg.get("REDLINE_GB") is not None:
        redline_gb, redline_ok = _coerce_float(cfg.get("REDLINE_GB"))
        if not redline_ok or redline_gb is None or redline_gb <= 0:
            hard_errors.append("Redline must be greater than zero, or disabled.")
            redline_ok = False
        elif (headroom_ok and not _coerce_bool(cfg.get("REDLINE_ONLY_MODE"))
              and redline_gb >= headroom_gb):
            # Only while Headroom is TICKED (REDLINE_ONLY_MODE off) must Redline sit
            # strictly below it. Unticked, there is no ceiling — even with a Library
            # Size Cap also armed (that's redline+cap, NOT a ticked-Headroom config,
            # so _redline_only_mode_cfg's no-cap requirement must not gate this).
            # Matches the save validator and the client-side check.
            hard_errors.append("Redline must be lower than Headroom — untick "
                               "Headroom for redline-only mode.")
            redline_ok = False

    cap_gb = None
    cap_configured = False
    cap_value = cfg.get("MAX_LIBRARY_GB")
    if cap_value is not None:
        cap_gb, cap_ok = _coerce_float(cap_value)
        if not cap_ok or cap_gb is None or cap_gb <= 0:
            hard_errors.append("Library Size Cap must be greater than zero, or disabled.")
        else:
            cap_configured = True

    total_gb = None
    try:
        total_gb = float((disk or {}).get("total_gb") or 0)
    except (TypeError, ValueError):
        total_gb = None

    limit_gb = None
    safety_ok = True
    safety_message = ""
    if headroom_ok and max_pct_ok and total_gb and total_gb > 0:
        limit_gb = round(total_gb * max_pct / 100, 1)
        # The safety cap bounds the free-space floor the system maintains: the
        # Headroom target normally, the Redline floor in redline-only mode.
        _maintained_gb = headroom_gb if headroom_gb > 0 else (
            redline_gb if (redline_ok and redline_gb) else None)
        safety_ok = _maintained_gb is None or _maintained_gb <= limit_gb
        if not safety_ok:
            safety_message = ("Redline floor is over the safety percentage."
                              if headroom_gb == 0 else
                              "Headroom target is over the safety percentage.")
            safety_errors.append(safety_message)

    # The same safety percentage caps how much a Library Size Cap may delete in a Live
    # run: the cap can't sit below (library - max_pct% of library), so a Live pass
    # removes at most max_pct% of the library. Simulate is exempt (preview only).
    library_gb_val = None
    try:
        library_gb_val = float(library_gb) if library_gb is not None else None
    except (TypeError, ValueError):
        library_gb_val = None
    cap_floor_gb = None
    cap_safety_ok = True
    cap_safety_message = ""
    if cap_configured and max_pct_ok and library_gb_val and library_gb_val > 0:
        cap_floor_gb = round(library_gb_val * (100 - max_pct) / 100, 1)
        cap_safety_ok = cap_gb >= cap_floor_gb
        if not cap_safety_ok:
            cap_safety_message = "Library Size Cap would delete more than the safety percentage of the library."
            safety_errors.append(cap_safety_message)

    # A Cleanup needs at least one active space target. Headroom 0 + no Redline + no
    # cap means nothing to enforce, so block Live (Simulate can preview an empty plan).
    if (headroom_ok and headroom_gb == 0
            and cfg.get("REDLINE_GB") is None
            and cfg.get("MAX_LIBRARY_GB") is None):
        safety_errors.append("Set a Headroom target, Redline, or Library Size Cap to enable Automatic Cleanup.")

    # Dedupe (order-preserving): a message can be reached by multiple paths.
    def _dedupe(items: list[str]) -> list[str]:
        seen = set()
        out = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    simulate_errors = _dedupe(hard_errors)
    headroom_errors = _dedupe(hard_errors + safety_errors)

    # User-facing Live (arming automatic mode, the manual Cleanup button) always
    # requires that a Simulate has seen the library under the CURRENT config:
    #   • limits breached (or redline-only, which deletes the moment its floor is
    #     hit): a deletion plan stamped with exactly these thresholds — moving
    #     Headroom/Redline/Cap ghosts Live until a Simulate refreshes it;
    #   • limits satisfied: proof a Simulate has run at least once — the plan
    #     stamp when there is one, else the library snapshot every completed
    #     scan writes (within limits the eligible queue can legitimately be
    #     empty, so the snapshot is the evidence).
    # Deliberately NOT part of ok_for_cleanup — the scheduler recomputes its own
    # plan every run and must not auto-pause an armed Live over this.
    simulate_required = False
    simulate_first_time = False
    # A full library scan must have run within the last two days (see
    # _full_scan_overdue) — an older plan is ghosted no matter what its stamp
    # says. The snapshot is kept for display; only Cleanup + arming lock until a
    # fresh scan. Skipped when there's no snapshot yet (the plan/evidence logic
    # below owns the first-time "run a Simulate" case).
    scan_overdue = _full_scan_overdue()
    if not headroom_errors:
        if scan_overdue:
            simulate_required = True
        else:
            try:
                _plan_current = _pending_plan_current(cfg, use_saved_file=not candidate_cfg)
                if _redline_only_mode_cfg(cfg):
                    simulate_required = not _plan_current
                elif _deletion_limits_exceeded(cfg, disk, library_gb_val):
                    simulate_required = not _plan_current
                else:
                    simulate_required = not (_plan_current or _simulate_evidence(cfg))
                    simulate_first_time = simulate_required
            except Exception:
                simulate_required = False

    if not simulate_required:
        simulate_required_message = ""
    elif scan_overdue:
        simulate_required_message = _SCAN_OVERDUE_MESSAGE
    elif _redline_only_mode_cfg(cfg):
        simulate_required_message = ("Run Simulate to build the Redline deletion-order "
                                     "preview before enabling Automatic Cleanup.")
    elif simulate_first_time:
        simulate_required_message = ("Run Simulate once so MediaReducer has scanned "
                                     "your library before enabling automatic mode.")
    elif _pending_raw():
        # A plan exists but its stamp no longer matches — the settings moved.
        simulate_required_message = ("Settings changed since the last Simulate — "
                                     "run it again to rebuild the deletion plan.")
    else:
        simulate_required_message = SIMULATE_REQUIRED_MESSAGE

    return {
        "ok_for_simulate": not simulate_errors,
        "ok_for_cleanup": not headroom_errors,
        "has_library_cap": cap_configured,
        # Live blocked specifically because a target exceeds the safety percentage
        # (headroom over the cap, or a cap below the floor) — the dashboard's breach
        # note words itself around this.
        "safety_blocked": not (safety_ok and cap_safety_ok),
        "simulate_required": simulate_required,
        "simulate_required_message": simulate_required_message,
        "simulate_tooltip": " ".join(simulate_errors),
        "cleanup_tooltip": " ".join(headroom_errors),
    }


def _find_appdata_file(base: str | Path, *names: str) -> Path | None:
    """Locate a config marker under a mounted appdata directory."""
    base = Path(base)
    for d in (base, base / "config"):
        for name in names:
            p = d / name
            try:
                if p.exists():
                    return p
            except OSError:
                pass
    try:
        if base.exists():
            for root, dirs, files in os.walk(base):
                rel = Path(root).relative_to(base)
                if len(rel.parts) >= 4:
                    dirs[:] = []
                    continue
                for name in names:
                    if name in files:
                        return Path(root) / name
    except OSError:
        pass
    return None


def _configured_connection_values(cfg: dict | None = None) -> dict:
    """Return only the saved connection values from config.json/form data."""
    cfg = cfg or load_config()
    return {
        "tautulli_url": str(cfg.get("TAUTULLI_URL") or "").strip(),
        "tautulli_key": str(cfg.get("TAUTULLI_API_KEY") or "").strip(),
        "plex_url": str(cfg.get("PLEX_URL") or "").strip(),
        "plex_token": str(cfg.get("PLEX_TOKEN") or "").strip(),
        "radarr_url": str(cfg.get("RADARR_URL") or "").strip(),
        "radarr_key": str(cfg.get("RADARR_API_KEY") or "").strip(),
        "jellyfin_url": str(cfg.get("JELLYFIN_URL") or "").strip(),
        "jellyfin_key": str(cfg.get("JELLYFIN_API_KEY") or "").strip(),
    }


def _effective_connection_values(cfg: dict | None = None) -> dict:
    """Connection values the way a connection should use them.

    Saved URLs win. A blank URL falls back to the detected default only when the
    service's credential is present — the key is the on/off switch, so a service with
    no credential keeps its blank URL and stays off (no probe, no error). The form
    never shows these resolved values; it renders the saved (possibly blank) fields via
    _connection_field_values."""
    cfg = cfg or load_config()
    conn = _configured_connection_values(cfg)
    defaults = None
    stored = cfg.get("_SERVICE_URL_DEFAULTS") or {}
    for url_field, key_field in _URL_KEY_FIELD_PAIRS.items():
        url_key = CONNECTION_FORM_FIELD_MAP[url_field]
        cred_key = CONNECTION_FORM_FIELD_MAP[key_field]
        if conn[url_key] or not conn[cred_key]:
            continue
        if defaults is None:
            defaults = _connection_url_defaults(cfg)
        conn[url_key] = defaults.get(url_field) or str(stored.get(url_field) or "").strip()
    return conn


def _autodetected_connection_values(cfg: dict | None = None) -> dict:
    """Best-effort one-shot appdata detection for the Config Auto Detect button."""
    import xml.etree.ElementTree as ET
    detected = {
        "tautulli_url": "",
        "tautulli_key": "",
        "plex_url": "",
        "plex_token": "",
        "radarr_url": "",
        "radarr_key": "",
        "jellyfin_url": "",
        "jellyfin_key": "",
    }

    tautulli_ini = _find_appdata_file(TAUTULLI_APPDATA_DIR, "config.ini")
    if tautulli_ini:
        try:
            parser = configparser.RawConfigParser(strict=False)
            parser.read(tautulli_ini, encoding="utf-8")
            host = parser.get("PMS", "pms_ip", fallback=None)
            if host:
                detected.setdefault("host", host)
                detected["tautulli_url"] = f"http://{host}:{parser.get('General', 'http_port', fallback='8181')}"
                detected["tautulli_key"] = parser.get("General", "api_key", fallback="") or ""
                detected["plex_url"] = f"http://{host}:{parser.get('PMS', 'pms_port', fallback='32400')}"
                detected["plex_token"] = parser.get("PMS", "pms_token", fallback="") or ""
        except Exception:
            pass

    # Radarr's API key comes from config.xml; its URL isn't in appdata, so default to
    # port 7878 on the host Tautulli reported (the user overrides if their port differs).
    radarr_xml = _find_appdata_file(RADARR_APPDATA_DIR, "config.xml")
    if radarr_xml:
        try:
            key = ET.parse(radarr_xml).getroot().findtext("ApiKey")
            if key:
                detected["radarr_key"] = key
                if detected.get("host"):
                    detected["radarr_url"] = f"http://{detected['host']}:7878"
        except Exception:
            pass

    # Jellyfin: read the HTTP port from its network config (default 8096). The API key
    # must be created in Jellyfin and can't be detected. The host is only known if
    # another service reported one, so a Jellyfin-only setup still needs a manual URL.
    jellyfin_port = "8096"
    jellyfin_xml = _find_appdata_file(JELLYFIN_APPDATA_DIR, "network.xml", "system.xml")
    if jellyfin_xml:
        try:
            root = ET.parse(jellyfin_xml).getroot()
            jellyfin_port = (root.findtext("PublicPort")
                             or root.findtext("InternalHttpPort")
                             or root.findtext("HttpServerPortNumber")
                             or "8096")
        except Exception:
            jellyfin_port = "8096"
    if detected.get("host"):
        detected["jellyfin_url"] = f"http://{detected['host']}:{jellyfin_port}"

    return detected



CONNECTION_FORM_FIELD_MAP = {
    "TAUTULLI_URL": "tautulli_url",
    "TAUTULLI_API_KEY": "tautulli_key",
    "PLEX_URL": "plex_url",
    "PLEX_TOKEN": "plex_token",
    "RADARR_URL": "radarr_url",
    "RADARR_API_KEY": "radarr_key",
    "JELLYFIN_URL": "jellyfin_url",
    "JELLYFIN_API_KEY": "jellyfin_key",
}

# Each service URL is paired with the credential that signals the user wants it:
# a blank URL is filled from the detected default only when its key is present.
_URL_KEY_FIELD_PAIRS = {
    "TAUTULLI_URL": "TAUTULLI_API_KEY",
    "PLEX_URL": "PLEX_TOKEN",
    "RADARR_URL": "RADARR_API_KEY",
    "JELLYFIN_URL": "JELLYFIN_API_KEY",
}

# Standard LAN ports, used to show a generic placeholder when appdata detection
# can't supply a real host. None of these services serve TLS by default.
_GENERIC_URL_PLACEHOLDERS = {
    "TAUTULLI_URL": "http://SERVER-IP:8181",
    "PLEX_URL": "http://SERVER-IP:32400",
    "RADARR_URL": "http://SERVER-IP:7878",
    "JELLYFIN_URL": "http://SERVER-IP:8096",
}


def _normalize_service_url(value) -> str:
    """Trim a service URL and assume http:// when no scheme is given — none of
    the supported services serve TLS by default, so a bare host:port is the
    common case."""
    raw = str(value or "").strip()
    if raw and "://" not in raw:
        raw = "http://" + raw
    return raw


_SERVICE_DEFAULT_PORTS = {
    "TAUTULLI_URL": 8181,
    "PLEX_URL": 32400,
    "RADARR_URL": 7878,
    "JELLYFIN_URL": 8096,
}


_LAST_REQUEST_LAN_HOST = ""


def _request_lan_host() -> str:
    """The host the browser used to reach MediaReducer — the natural 'server address'
    for same-box service defaults. Blank when loopback: inside a container 127.0.0.1 is
    the container itself, never a sibling.

    The connection-health probe runs in a background thread (startup, Check for Errors,
    after a save) with NO request context, so the last usable host a real request used
    is remembered and reused there. Otherwise the threaded probe resolves URL defaults
    to a different address than the Config page shows and can report a healthy service
    as down (e.g. locking Radarr optional cleanup)."""
    global _LAST_REQUEST_LAN_HOST
    try:
        host = urlparse("//" + (request.host or "")).hostname or ""
    except Exception:
        return _LAST_REQUEST_LAN_HOST   # no request context — reuse the last real one
    if not host or host == "localhost" or host.startswith("127.") or host == "::1":
        return _LAST_REQUEST_LAN_HOST
    _LAST_REQUEST_LAN_HOST = host
    return host


def _usable_detected_host(url: str) -> str:
    """Hostname from a detected URL, unless loopback/unspecified — appdata configs
    often say 127.0.0.1 for same-box services, unreachable from this container."""
    try:
        h = urlparse(url).hostname or ""
    except Exception:
        return ""
    if not h or h == "localhost" or h.startswith("127.") or h in ("0.0.0.0", "::1", "::"):
        return ""
    return h


def _connection_url_defaults(cfg: dict | None = None) -> dict:
    """Best-known default URL per service. Host comes from the address the browser used
    to reach MediaReducer (everything but Plex runs alongside it); Plex prefers the host
    Tautulli's config points at, since Plex can live on another box. Ports come from
    appdata detection where available, else the standard port. Blank when no host is
    known."""
    det = _autodetected_connection_values(cfg)
    req_host = _request_lan_host()
    out = {}
    for field, det_key in (("TAUTULLI_URL", "tautulli_url"), ("PLEX_URL", "plex_url"),
                           ("RADARR_URL", "radarr_url"), ("JELLYFIN_URL", "jellyfin_url")):
        det_url = det.get(det_key) or ""
        det_host = _usable_detected_host(det_url)
        try:
            port = urlparse(det_url).port or _SERVICE_DEFAULT_PORTS[field]
        except Exception:
            port = _SERVICE_DEFAULT_PORTS[field]
        host = (det_host or req_host) if field == "PLEX_URL" else (req_host or det_host)
        out[field] = f"http://{host}:{port}" if host else ""
    return out


def _normalize_saved_service_urls(cfg: dict) -> None:
    """Scheme-normalize the URL fields for saving. Blank stays blank — the default
    address is applied at connection time by _effective_connection_values, never
    written into the user's config."""
    for url_field in _URL_KEY_FIELD_PAIRS:
        cfg[url_field] = _normalize_service_url(cfg.get(url_field))


def _connection_field_values(cfg: dict | None = None) -> dict:
    """The saved connection form values only — never the resolved defaults,
    so a blank URL field stays blank in the form (its placeholder shows the
    address that will be used)."""
    conn = _configured_connection_values(cfg)
    return {field: str(conn.get(key) or "") for field, key in CONNECTION_FORM_FIELD_MAP.items()}


def _autodetected_connection_field_values() -> dict:
    """One-shot appdata detections mapped to Config form field names —
    credentials only, since URLs are default-driven and the Auto Detect
    button never fills them."""
    conn = _autodetected_connection_values()
    return {field: str(conn.get(key) or "")
            for field, key in CONNECTION_FORM_FIELD_MAP.items()
            if not field.endswith("_URL")}


def _json_request(url: str, headers: dict | None = None, timeout: int = 15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def _appdata_marker(path: str, *markers: str) -> dict:
    """Return mount/marker status for a Docker appdata volume."""
    base = Path(path)
    mounted = False
    try:
        mounted = base.exists()
    except OSError:
        mounted = False
    found = _find_appdata_file(base, *markers) if mounted else None
    return {
        "mounted": bool(mounted),
        "ok": bool(found),
        "path": str(found) if found else None,
    }


def _appdata_mount_state() -> dict:
    """Fast appdata marker check shared by dashboard/config health checks."""
    library = Path(FILESYSTEM_CHECK_PATH)
    try:
        library_ok = library.exists() and any(library.iterdir())
    except OSError:
        library_ok = False
    return {
        "tautulli":  _appdata_marker(TAUTULLI_APPDATA_DIR, "config.ini", "tautulli.db"),
        "radarr":    _appdata_marker(RADARR_APPDATA_DIR, "config.xml"),
        "jellyfin":  _appdata_marker(JELLYFIN_APPDATA_DIR, "system.xml", "network.xml"),
        "library":   {"mounted": library.exists(), "ok": library_ok, "path": FILESYSTEM_CHECK_PATH if library_ok else None},
    }


def _probe_json(url: str, headers: dict | None = None, timeout: int = 5) -> tuple[bool, str]:
    """Small JSON HTTP probe used by the Config health check."""
    try:
        _json_request(url, headers=headers, timeout=timeout)
        return True, "reachable"
    except Exception as e:
        return False, str(e)


def _resolve_reported_media_path(path_str: str | None) -> Path | None:
    """Resolve a media-server path by matching its longest existing /library suffix."""
    if not path_str:
        return None
    raw = str(path_str).strip().replace("\\", "/")
    if not raw:
        return None
    library_root = Path(FILESYSTEM_CHECK_PATH)
    library_s = str(library_root)

    if raw == library_s or raw.startswith(library_s + "/"):
        rel = raw[len(library_s):].lstrip("/")
        candidate = library_root / rel if rel else library_root
        return candidate if candidate.exists() else None

    parts = [part for part in raw.split("/") if part]
    for start in range(len(parts)):
        candidate = library_root.joinpath(*parts[start:])
        if candidate.exists():
            return candidate
    return None


def _extract_media_paths_from_item(item: dict | None) -> list[str]:
    paths: list[str] = []
    if not isinstance(item, dict):
        return paths

    for key in ("file", "file_path", "media_file", "location", "path", "Path"):
        value = item.get(key)
        if value:
            paths.append(str(value))

    containers = []
    for key in ("media_info", "Media", "MediaSources"):
        value = item.get(key)
        if isinstance(value, list):
            containers.extend(v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            containers.append(value)

    for media in containers:
        for key in ("file", "file_path", "media_file", "location", "path", "Path"):
            value = media.get(key)
            if value:
                paths.append(str(value))
        for parts_key in ("parts", "Part"):
            for part in _as_list(media.get(parts_key)):
                if not isinstance(part, dict):
                    continue
                for key in ("file", "file_path", "media_file", "location", "path", "Path"):
                    value = part.get(key)
                    if value:
                        paths.append(str(value))

    out = []
    seen = set()
    for path in paths:
        path = str(path).strip()
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _tautulli_api_request(conn: dict, cmd: str, timeout: int = 8, **params):
    values = dict(params)
    values.update({"apikey": conn["tautulli_key"], "cmd": cmd})
    url = f"{conn['tautulli_url'].rstrip('/')}/api/v2?{urlencode(values)}"
    payload = _json_request(url, timeout=timeout)
    response = (payload or {}).get("response") or {}
    if response.get("result") != "success":
        raise RuntimeError(f"Tautulli API error: {payload}")
    return response.get("data")


def _sample_tautulli_media_paths(conn: dict, limit: int = 12) -> list[str]:
    paths: list[str] = []
    libraries = _tautulli_api_request(conn, "get_libraries", timeout=8) or []
    section_ids = [
        lib.get("section_id") for lib in libraries
        if isinstance(lib, dict) and lib.get("section_type") == "movie" and lib.get("is_active", 1)
    ]
    for section_id in section_ids:
        data = _tautulli_api_request(
            conn,
            "get_library_media_info",
            timeout=10,
            section_id=section_id,
            section_type="movie",
            start=0,
            length=25,
            order_column="title",
            order_dir="asc",
        )
        rows = data.get("data", data if isinstance(data, list) else []) if data is not None else []
        for row in rows:
            paths.extend(_extract_media_paths_from_item(row))
            if len(paths) >= limit:
                return paths[:limit]
        for row in rows[: min(len(rows), 8)]:
            rating_key = row.get("rating_key") if isinstance(row, dict) else None
            if not rating_key:
                continue
            meta = _tautulli_api_request(conn, "get_metadata", timeout=10, rating_key=rating_key) or {}
            paths.extend(_extract_media_paths_from_item(meta))
            if len(paths) >= limit:
                return paths[:limit]
    return paths[:limit]


def _sample_jellyfin_media_paths(conn: dict, limit: int = 12) -> list[str]:
    data = _jellyfin_get(
        conn["jellyfin_url"],
        conn["jellyfin_key"],
        "Items",
        {
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": "Path,MediaSources",
            "Limit": max(limit, 25),
        },
        timeout=10,
    ) or {}
    paths: list[str] = []
    for item in _jellyfin_items_from_payload(data):
        paths.extend(_extract_media_paths_from_item(item))
        if len(paths) >= limit:
            break
    return paths[:limit]


def _media_path_compatibility_state(server_name: str, paths: list[str]) -> dict:
    resolved = []
    unresolved = []
    for raw in paths:
        match = _resolve_reported_media_path(raw)
        if match:
            resolved.append({"reported": raw, "resolved": str(match)})
        else:
            unresolved.append(raw)
    return {
        "server": server_name,
        "checked": len(paths),
        "matched": len(resolved),
        "unmatched": len(unresolved),
        "ok": len(unresolved) == 0 if paths else True,
        "resolved_examples": resolved[:3],
        "unmatched_examples": unresolved[:5],
    }


def _plex_collection_names(url: str, token: str, timeout: int = 8) -> list[str]:
    """All Plex collection titles across movie library sections (sorted).

    Plex returns section and collection lists under either ``Directory`` or
    ``Metadata`` depending on version/endpoint, so we accept either. If no
    section reports type ``movie`` we fall back to scanning every section.
    """
    base = url.rstrip("/")

    def _get(path):
        sep = "&" if "?" in path else "?"
        return _json_request(f"{base}{path}{sep}X-Plex-Token={token}",
                             headers={"Accept": "application/json"}, timeout=timeout)

    def _items(container):
        mc = (container or {}).get("MediaContainer") or {}
        items = mc.get("Directory")
        if not items:
            items = mc.get("Metadata") or []
        return [items] if isinstance(items, dict) else (items or [])

    sections = _items(_get("/library/sections"))
    section_keys = [s.get("key") for s in sections if s.get("type") == "movie" and s.get("key")]
    if not section_keys:  # fall back to all sections if type detection is off
        section_keys = [s.get("key") for s in sections if s.get("key")]

    names = set()
    for key in section_keys:
        for coll in _items(_get(f"/library/sections/{key}/collections")):
            title = str(coll.get("title") or "").strip()
            if title:
                names.add(title)
    return sorted(names, key=str.lower)


def _debug_collection_names(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
    return [str(n).strip() for n in (raw or []) if str(n).strip()]


def _plex_get(url: str, token: str, path: str, timeout: int = 15):
    sep = "&" if "?" in path else "?"
    return _json_request(
        f"{url.rstrip('/')}{path}{sep}X-Plex-Token={token}",
        headers={"Accept": "application/json"},
        timeout=timeout,
    )


def _plex_items(container):
    mc = (container or {}).get("MediaContainer") or {}
    items = mc.get("Directory")
    if not items:
        items = mc.get("Metadata") or []
    return [items] if isinstance(items, dict) else (items or [])


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _plex_debug_movie(item: dict) -> dict:
    files = []
    for media in _as_list(item.get("Media")):
        for part in _as_list(media.get("Part")):
            if isinstance(part, dict):
                files.append({
                    "id": part.get("id"),
                    "key": part.get("key"),
                    "file": part.get("file"),
                    "size": part.get("size"),
            })
    guids = []
    for guid in _as_list(item.get("Guid")):
        if isinstance(guid, dict) and guid.get("id"):
            guids.append(guid.get("id"))
    return {
        "title": item.get("title"),
        "type": item.get("type"),
        "year": item.get("year"),
        "rating_key": item.get("ratingKey"),
        "key": item.get("key"),
        "guid": item.get("guid"),
        "guids": guids,
        "files": files,
    }


@app.route("/api/collections/plex/debug", methods=["POST"])
def api_plex_collection_debug():
    """Show what Plex returns for the selected protected collections."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("plex_url") and conn.get("plex_token")):
        return jsonify({"ok": False, "error": "Add the Plex URL and token above and Save, then debug."}), 400

    data = request.get_json(silent=True) or {}
    names = _debug_collection_names(data.get("names") or [])
    if not names:
        return jsonify({"ok": False, "error": "Tick at least one Plex collection first."}), 400
    wanted = {n.lower() for n in names}

    url, token = conn["plex_url"], conn["plex_token"]
    try:
        section_payload = _plex_get(url, token, "/library/sections", timeout=15)
        sections = _plex_items(section_payload)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not list Plex sections: {e}"}), 400

    movie_sections = [s for s in sections if s.get("type") == "movie" and s.get("key")]
    section_source = "movie sections"
    if not movie_sections:
        movie_sections = [s for s in sections if s.get("key")]
        section_source = "all sections fallback"

    section_attempts = []
    matched = []
    seen_collection_keys = set()
    for section in movie_sections:
        section_key = section.get("key")
        endpoint = f"/library/sections/{section_key}/collections"
        try:
            payload = _plex_get(url, token, endpoint, timeout=15)
            collections = _plex_items(payload)
            error = ""
        except Exception as e:
            collections = []
            error = str(e)
        section_attempts.append({
            "section_key": section_key,
            "section_title": section.get("title"),
            "section_type": section.get("type"),
            "endpoint": endpoint,
            "count": len(collections),
            "error": error,
            "names": [str(c.get("title") or "") for c in collections[:100]],
            "truncated": len(collections) > 100,
        })
        for coll in collections:
            title = str(coll.get("title") or "").strip()
            coll_key = coll.get("ratingKey") or coll.get("key")
            dedupe_key = str(coll_key or "") + "|" + title.lower()
            if title.lower() in wanted and coll_key and dedupe_key not in seen_collection_keys:
                seen_collection_keys.add(dedupe_key)
                matched.append({
                    "section_key": section_key,
                    "section_title": section.get("title"),
                    "title": title,
                    "rating_key": coll.get("ratingKey"),
                    "key": coll.get("key"),
                    "collection_key": coll_key,
                    "type": coll.get("type"),
                })

    collections = []
    for coll in matched:
        coll_key = coll.get("collection_key")
        child_attempts = []
        for endpoint in (f"/library/collections/{coll_key}/children", f"/library/metadata/{coll_key}/children"):
            try:
                payload = _plex_get(url, token, endpoint, timeout=20)
                children = _plex_items(payload)
                error = ""
            except Exception as e:
                children = []
                error = str(e)
            child_attempts.append({
                "endpoint": endpoint,
                "count": len(children),
                "error": error,
                "items": [_plex_debug_movie(i) for i in children[:100]],
                "truncated": len(children) > 100,
            })
        coll = dict(coll)
        coll["child_attempts"] = child_attempts
        collections.append(coll)

    return jsonify({
        "ok": True,
        "selected": names,
        "section_source": section_source,
        "sections": [
            {"key": s.get("key"), "title": s.get("title"), "type": s.get("type")}
            for s in sections
        ],
        "section_attempts": section_attempts,
        "matched_count": len(collections),
        "collections": collections,
    })


def _jellyfin_boxset_names(url: str, key: str, timeout: int = 8) -> list[str]:
    """All Jellyfin BoxSet (collection) names (sorted)."""
    data = _json_request(
        f"{url.rstrip('/')}/Items?IncludeItemTypes=BoxSet&Recursive=true",
        headers={"Authorization": f'MediaBrowser Token="{key}"', "X-Emby-Token": key, "Accept": "application/json"},
        timeout=timeout,
    )
    items = (data or {}).get("Items") or []
    names = {str(i.get("Name") or "").strip() for i in items if i.get("Name")}
    return sorted(names, key=str.lower)


def _jellyfin_headers(key: str) -> dict:
    return {
        "Authorization": f'MediaBrowser Token="{key}"',
        "X-Emby-Token": key,
        "Accept": "application/json",
    }


def _jellyfin_get(url: str, key: str, path: str, params: dict | None = None, timeout: int = 15):
    query = ("?" + urlencode(params)) if params else ""
    return _json_request(
        f"{url.rstrip('/')}/{path.lstrip('/')}{query}",
        headers=_jellyfin_headers(key),
        timeout=timeout,
    )


def _jellyfin_debug_item(item: dict) -> dict:
    media_sources = []
    for ms in (item.get("MediaSources") or [])[:4]:
        if isinstance(ms, dict):
            media_sources.append({
                "id": ms.get("Id"),
                "path": ms.get("Path"),
                "size": ms.get("Size"),
            })
    return {
        "name": item.get("Name"),
        "id": item.get("Id"),
        "item_id": item.get("ItemId"),
        "movie_id": item.get("MovieId"),
        "type": item.get("Type"),
        "path": item.get("Path"),
        "parent_id": item.get("ParentId"),
        "collection_type": item.get("CollectionType"),
        "provider_ids": item.get("ProviderIds") or {},
        "media_sources": media_sources,
    }


def _jellyfin_items_from_payload(payload):
    items = payload.get("Items") if isinstance(payload, dict) else payload
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]
    return []


@app.route("/api/collections/jellyfin/debug", methods=["POST"])
def api_jellyfin_collection_debug():
    """Show what Jellyfin returns for the selected protected collections."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("jellyfin_url") and conn.get("jellyfin_key")):
        return jsonify({"ok": False, "error": "Add the Jellyfin URL and API key above and Save, then debug."}), 400

    data = request.get_json(silent=True) or {}
    names = data.get("names") or []
    if isinstance(names, str):
        names = [p.strip() for p in re.split(r"[\n,]+", names) if p.strip()]
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names:
        return jsonify({"ok": False, "error": "Tick at least one Jellyfin collection first."}), 400
    wanted = {n.lower() for n in names}

    url, key = conn["jellyfin_url"], conn["jellyfin_key"]
    fields = "Path,MediaSources,ProviderIds,ParentId,DateCreated"

    try:
        users_payload = _jellyfin_get(url, key, "Users", timeout=10) or []
    except Exception as e:
        users_payload = []
        users_error = str(e)
    else:
        users_error = ""
    users = [u for u in users_payload if isinstance(u, dict)]
    user_ids = [u.get("Id") for u in users if u.get("Id")]
    user_id = user_ids[0] if user_ids else None

    boxset_attempts = []
    boxsets_by_id = {}
    for path in ([f"Users/{user_id}/Items"] if user_id else []) + ["Items"]:
        params = {"IncludeItemTypes": "BoxSet", "Recursive": "true"}
        try:
            payload = _jellyfin_get(url, key, path, params, timeout=15) or {}
            items = _jellyfin_items_from_payload(payload)
            error = ""
        except Exception as e:
            items = []
            error = str(e)
        for item in items:
            if item.get("Id"):
                boxsets_by_id.setdefault(item.get("Id"), item)
        boxset_attempts.append({
            "endpoint": path,
            "params": params,
            "count": len(items),
            "error": error,
            "names": [str(i.get("Name") or "") for i in items[:100]],
            "truncated": len(items) > 100,
        })

    matched = [
        item for item in boxsets_by_id.values()
        if str(item.get("Name") or "").strip().lower() in wanted and item.get("Id")
    ]

    collections = []
    for box in sorted(matched, key=lambda b: str(b.get("Name") or "").lower()):
        box_id = box.get("Id")
        child_attempts = []
        # Parity attempts: EXACTLY the queries the engine's _jellyfin_boxset_children
        # runs (same endpoints, params, Fields string, order). Some Jellyfin builds
        # enumerate a BoxSet through only one query form, so a faithful mirror is the
        # only way the debug output reflects what a real run sees.
        engine_fields = "Path,MediaSources,ProviderIds"
        attempts = []  # (path, params, is_engine_parity)
        if user_id:
            attempts.append((f"Collections/{box_id}/Items", {"UserId": user_id, "IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
            attempts.append((f"Users/{user_id}/Items", {"ParentId": box_id, "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
            attempts.append((f"Users/{user_id}/Items", {"ParentId": box_id, "Fields": engine_fields}, True))
        attempts.append((f"Collections/{box_id}/Items", {"IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
        attempts.append(("Items", {"ParentId": box_id, "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
        attempts.append(("Items", {"ParentId": box_id, "Fields": engine_fields}, True))
        # Extra query forms the engine does not run, probed for diagnostics.
        if user_id:
            attempts.append((f"Collections/{box_id}/Items", {"UserId": user_id, "Fields": fields}, False))
            attempts.append((f"Users/{user_id}/Items", {"ParentId": box_id, "Recursive": "true", "Fields": fields}, False))
        attempts.append((f"Collections/{box_id}/Items", {"Fields": fields}, False))
        attempts.append(("Items", {"ParentId": box_id, "Recursive": "true", "Fields": fields}, False))

        for path, params, is_engine in attempts:
            try:
                payload = _jellyfin_get(url, key, path, params, timeout=20) or {}
                items = _jellyfin_items_from_payload(payload)
                error = ""
            except Exception as e:
                items = []
                error = str(e)
            child_attempts.append({
                "endpoint": path,
                "params": params,
                "engine": is_engine,
                "count": len(items),
                "error": error,
                "items": [_jellyfin_debug_item(i) for i in items[:100]],
                "truncated": len(items) > 100,
            })

        collections.append({
            "name": box.get("Name"),
            "id": box_id,
            "type": box.get("Type"),
            "child_attempts": child_attempts,
        })

    return jsonify({
        "ok": True,
        "selected": names,
        "users": [{"id": u.get("Id"), "name": u.get("Name")} for u in users],
        "users_error": users_error,
        "using_user_id": user_id,
        "boxset_attempts": boxset_attempts,
        "matched_count": len(collections),
        "collections": collections,
    })


# ── API debug endpoints (debug mode) ─────────────────────────────────────────
# Each mirrors the engine's real API calls (same commands, params, extraction) so
# the output shows what a run will actually see. All return
# {"ok": True, "text": "..."} — preformatted, secrets never echoed.

_DEBUG_SAMPLE_ROWS = 25
_DEBUG_SAMPLE_META = 3


def _debug_resolved_line(raw, monitor_dirs):
    """One 'raw -> resolved' line with monitored-dir status for path debugging."""
    resolved = _resolve_reported_media_path(raw)
    if resolved is None:
        return f"      {raw}\n        -> NO MATCH under {FILESYSTEM_CHECK_PATH}"
    rs = str(resolved)
    monitored = any(rs == d or rs.startswith(d.rstrip("/") + "/") for d in monitor_dirs)
    return (f"      {raw}\n        -> {rs} | exists=yes | "
            f"monitored={'yes' if monitored else 'NO'}")


@app.route("/api/debug/tautulli/movies", methods=["POST"])
def api_debug_tautulli_movies():
    """Mirror the engine's Tautulli movie-source calls: get_libraries ->
    get_library_media_info -> get_metadata (guids/collections + file paths)."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("tautulli_url") and conn.get("tautulli_key")):
        return jsonify({"ok": False, "error": "Add the Tautulli URL and API key above and Save, then debug."}), 400

    protected_names = set(cfg.get("PROTECTED_COLLECTIONS") or [])
    lines = ["Tautulli movie-source debug (mirrors engine calls)", ""]
    try:
        libraries = _tautulli_api_request(conn, "get_libraries", timeout=10) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"get_libraries failed: {e}"}), 400

    lines.append("get_libraries:")
    selected = []
    for lib in libraries:
        if not isinstance(lib, dict):
            continue
        is_movie = lib.get("section_type") == "movie" and lib.get("is_active", 1)
        tag = "  <- engine scans this" if is_movie else ""
        lines.append(f"  {lib.get('section_name')} [id={lib.get('section_id')}, type={lib.get('section_type')}, "
                     f"active={lib.get('is_active', 1)}, count={lib.get('count', 'n/a')}]{tag}")
        if is_movie:
            selected.append(lib)
    if not selected:
        lines.append("  WARNING: no active movie sections — the engine would find nothing to scan.")

    path_keys = ("file", "file_path", "media_file", "location", "path")
    sample_rows = []
    for lib in selected:
        section_id = lib.get("section_id")
        lines.append("")
        lines.append(f"get_library_media_info | section={lib.get('section_name')} [{section_id}] "
                     f"(engine params, first {_DEBUG_SAMPLE_ROWS} rows):")
        try:
            data = _tautulli_api_request(
                conn, "get_library_media_info", timeout=20,
                section_id=section_id, section_type="movie",
                start=0, length=_DEBUG_SAMPLE_ROWS,
                order_column="title", order_dir="asc",
            )
        except Exception as e:
            lines.append(f"  ERROR: {e}")
            continue
        rows = data.get("data", data if isinstance(data, list) else []) if data is not None else []
        total = data.get("recordsFiltered") or data.get("recordsTotal") if isinstance(data, dict) else None
        lines.append(f"  rows returned={len(rows)}" + (f" | total in section={total}" if total is not None else ""))
        rows_with_paths = sum(1 for r in rows if isinstance(r, dict) and any(r.get(k) for k in path_keys))
        lines.append(f"  rows carrying a file path: {rows_with_paths}/{len(rows)}"
                     + ("  <- expected: 0 (Tautulli rows have no paths; the engine resolves them via get_metadata)"
                        if rows_with_paths == 0 else ""))
        for r in rows[:3]:
            if isinstance(r, dict):
                lines.append(f"    - {r.get('title')!r} | rating_key={r.get('rating_key')} | "
                             f"play_count={r.get('play_count')} | last_played={r.get('last_played')} | "
                             f"added_at={r.get('added_at')}")
        sample_rows.extend(r for r in rows if isinstance(r, dict) and r.get("rating_key"))

    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]
    lines.append("")
    lines.append(f"get_metadata samples (first {_DEBUG_SAMPLE_META} movies — engine fetches this per movie):")
    for row in sample_rows[:_DEBUG_SAMPLE_META]:
        rk, title = row.get("rating_key"), row.get("title")
        lines.append(f"  {title!r} (rating_key={rk}):")
        try:
            meta = _tautulli_api_request(conn, "get_metadata", timeout=10, rating_key=rk, media_info=0) or {}
        except Exception as e:
            lines.append(f"    get_metadata media_info=0 ERROR: {e}")
            meta = {}
        colls = []
        for e in (meta.get("collections") or []):
            colls.append(e if isinstance(e, str) else (e.get("tag") if isinstance(e, dict) else str(e)))
        tmdb_id = imdb_id = None
        for guid in (meta.get("guids") or []):
            if isinstance(guid, str):
                if guid.startswith("tmdb://"):
                    tmdb_id = guid.replace("tmdb://", "").strip()
                elif guid.startswith("imdb://"):
                    imdb_id = guid.replace("imdb://", "").strip()
        protected = bool(protected_names & set(c for c in colls if c))
        lines.append(f"    collections={colls or '(none)'} | protected={'YES' if protected else 'no'} "
                     f"(vs {sorted(protected_names) or '(none configured)'})")
        lines.append(f"    guids -> imdb={imdb_id or '(none)'} | tmdb={tmdb_id or '(none)'}")
        try:
            meta_paths = _tautulli_api_request(conn, "get_metadata", timeout=10, rating_key=rk, media_info=1) or {}
        except Exception as e:
            lines.append(f"    get_metadata media_info=1 ERROR: {e}")
            meta_paths = {}
        raw_paths = _extract_media_paths_from_item(meta_paths)
        if not raw_paths:
            lines.append("    file paths: NONE FOUND — the engine would skip this movie (no_file_path)")
        else:
            lines.append("    file paths:")
            for raw in raw_paths[:4]:
                lines.append(_debug_resolved_line(raw, monitor_dirs))
    if not sample_rows:
        lines.append("  (no rows available to sample)")

    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/jellyfin/movies", methods=["POST"])
def api_debug_jellyfin_movies():
    """Mirror the engine's Jellyfin movie-source calls: System/Info, Items
    (movie listing), Users, and per-user play-data aggregation."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("jellyfin_url") and conn.get("jellyfin_key")):
        return jsonify({"ok": False, "error": "Add the Jellyfin URL and API key above and Save, then debug."}), 400
    url, key = conn["jellyfin_url"], conn["jellyfin_key"]

    lines = ["Jellyfin movie-source debug (mirrors engine calls)", ""]
    try:
        info = _jellyfin_get(url, key, "System/Info", timeout=10) or {}
        lines.append(f"System/Info: {info.get('ServerName') or '(no name)'} | version={info.get('Version') or 'n/a'}")
    except Exception as e:
        lines.append(f"System/Info ERROR: {e}")

    lines.append("")
    lines.append(f"Items (engine movie listing, first {_DEBUG_SAMPLE_ROWS} shown):")
    try:
        payload = _jellyfin_get(url, key, "Items", {
            "IncludeItemTypes": "Movie", "Recursive": "true",
            "Fields": "Path,MediaSources,DateCreated,ProviderIds",
            "EnableUserData": "false", "Limit": _DEBUG_SAMPLE_ROWS,
        }, timeout=20) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Items query failed: {e}"}), 400
    items = _jellyfin_items_from_payload(payload)
    total = payload.get("TotalRecordCount") if isinstance(payload, dict) else None
    lines.append(f"  returned={len(items)}" + (f" | TotalRecordCount={total}" if total is not None else ""))
    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]
    with_path = with_ids = 0
    for item in items:
        prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
        path = item.get("Path") or next((ms.get("Path") for ms in (item.get("MediaSources") or [])
                                         if isinstance(ms, dict) and ms.get("Path")), None)
        if path:
            with_path += 1
        if prov.get("imdb") or prov.get("tmdb"):
            with_ids += 1
    lines.append(f"  items with a Path: {with_path}/{len(items)} | with imdb/tmdb ProviderIds: {with_ids}/{len(items)}")
    for item in items[:3]:
        prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
        lines.append(f"    - {item.get('Name')!r} ({item.get('ProductionYear')}) | id={item.get('Id')} | "
                     f"imdb={prov.get('imdb') or '(none)'} | tmdb={prov.get('tmdb') or '(none)'} | "
                     f"DateCreated={item.get('DateCreated') or '(none)'}")
        raw = item.get("Path") or ""
        if raw:
            lines.append(_debug_resolved_line(raw, monitor_dirs))
        else:
            lines.append("      (no Path on item)")

    lines.append("")
    lines.append("Per-user play data (engine aggregates across ALL users):")
    try:
        users = [u for u in (_jellyfin_get(url, key, "Users", timeout=10) or []) if isinstance(u, dict)]
    except Exception as e:
        users = []
        lines.append(f"  Users ERROR: {e}")
    for u in users:
        uid = u.get("Id")
        try:
            up = _jellyfin_get(url, key, f"Users/{uid}/Items", {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "EnableUserData": "true", "Limit": 100,
            }, timeout=20) or {}
            uitems = _jellyfin_items_from_payload(up)
            played = sum(1 for i in uitems if (i.get("UserData") or {}).get("PlayCount"))
            latest = max((str((i.get("UserData") or {}).get("LastPlayedDate") or "") for i in uitems), default="")
            lines.append(f"  {u.get('Name') or '(no name)'} [{uid}]: sample={len(uitems)} | "
                         f"with PlayCount>0: {played} | most recent LastPlayedDate: {latest or '(none)'}")
        except Exception as e:
            lines.append(f"  {u.get('Name') or '(no name)'} [{uid}]: ERROR {e}")
    if not users:
        lines.append("  (no users listed — play counts would all be 0)")

    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/radarr", methods=["POST"])
def api_debug_radarr():
    """Mirror the engine's Radarr calls: system/status (startup check),
    /api/v3/movie (section detection) and the tmdbId lookup used at delete time."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("radarr_url") and conn.get("radarr_key")):
        return jsonify({"ok": False, "error": "Add the Radarr URL and API key above and Save, then debug."}), 400
    radarr_url, radarr_key = conn["radarr_url"].rstrip("/"), conn["radarr_key"]

    lines = ["Radarr API debug (mirrors engine calls)", ""]
    try:
        status = _json_request(f"{radarr_url}/api/v3/system/status",
                               headers={"X-Api-Key": radarr_key}, timeout=10) or {}
        lines.append(f"system/status: {status.get('appName') or 'Radarr'} | version={status.get('version') or 'n/a'}")
    except Exception as e:
        return jsonify({"ok": False, "error": f"system/status failed: {e}"}), 400

    lines.append("")
    lines.append("GET /api/v3/movie (engine uses this for section detection):")
    try:
        movies = _json_request(f"{radarr_url}/api/v3/movie",
                               headers={"X-Api-Key": radarr_key}, timeout=25) or []
    except Exception as e:
        lines.append(f"  ERROR: {e}")
        movies = []
    movies = [m for m in movies if isinstance(m, dict)]
    lines.append(f"  movies in Radarr: {len(movies)}")
    for m in movies[:5]:
        lines.append(f"    - {m.get('title')!r} ({m.get('year')}) | tmdbId={m.get('tmdbId')} | "
                     f"monitored={m.get('monitored')} | hasFile={m.get('hasFile')} | path={m.get('path')}")

    detected_id = cfg.get("_RADARR_DETECTED_SECTION_ID")
    detected_name = cfg.get("_RADARR_DETECTED_SECTION_NAME")
    lines.append("")
    lines.append(f"Detected Radarr/Overseerr section: "
                 f"{(str(detected_name) + ' [' + str(detected_id) + ']') if detected_id else '(none detected)'}")

    sample_tmdb = next((m.get("tmdbId") for m in movies if m.get("tmdbId")), None)
    lines.append("")
    if sample_tmdb:
        lines.append(f"Delete-time lookup form: GET /api/v3/movie?tmdbId={sample_tmdb}")
        try:
            hits = _json_request(f"{radarr_url}/api/v3/movie?tmdbId={sample_tmdb}",
                                 headers={"X-Api-Key": radarr_key}, timeout=10) or []
            hits = [h for h in hits if isinstance(h, dict)]
            lines.append(f"  returned {len(hits)} match(es)"
                         + (f": {hits[0].get('title')!r}" if hits else ""))
        except Exception as e:
            lines.append(f"  ERROR: {e}")
    else:
        lines.append("Delete-time lookup form: skipped (no movie with a tmdbId to sample)")

    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/media-paths", methods=["POST"])
def api_debug_media_paths():
    """Show how server-reported media paths resolve under /library and whether
    they land inside the monitored directories — the engine's deletion keyspace."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]

    lines = ["Media path mapping debug", ""]
    root = Path(FILESYSTEM_CHECK_PATH)
    try:
        top = sorted(p.name for p in root.iterdir() if p.is_dir())
    except Exception as e:
        top = []
        lines.append(f"{FILESYSTEM_CHECK_PATH} listing ERROR: {e}")
    lines.append(f"{FILESYSTEM_CHECK_PATH} top-level folders ({len(top)}): {', '.join(top[:20]) or '(none)'}"
                 + (" …" if len(top) > 20 else ""))

    lines.append("")
    lines.append("Monitored directories (the engine may only delete inside these):")
    if not monitor_dirs:
        lines.append("  (none configured)")
    for d in monitor_dirs:
        exists = Path(d).exists()
        lines.append(f"  {d} | exists={'yes' if exists else 'NO'}")

    if bool(cfg.get("USE_PLEX")) and conn.get("tautulli_url") and conn.get("tautulli_key"):
        lines.append("")
        lines.append("Tautulli-reported sample paths -> resolved /library paths:")
        try:
            samples = _sample_tautulli_media_paths(conn, limit=8)
            if not samples:
                lines.append("  (no paths sampled — Tautulli rows carry no paths; metadata sampling found none)")
            for raw in samples:
                lines.append(_debug_resolved_line(raw, monitor_dirs))
        except Exception as e:
            lines.append(f"  ERROR: {e}")

    if bool(cfg.get("USE_JELLYFIN")) and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        lines.append("")
        lines.append("Jellyfin-reported sample paths -> resolved /library paths:")
        try:
            samples = _sample_jellyfin_media_paths(conn, limit=8)
            if not samples:
                lines.append("  (no paths sampled)")
            for raw in samples:
                lines.append(_debug_resolved_line(raw, monitor_dirs))
        except Exception as e:
            lines.append(f"  ERROR: {e}")

    return jsonify({"ok": True, "text": "\n".join(lines)})


def _fmt_epoch(ts) -> str:
    try:
        ts = int(ts)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts > 0 else "never"
    except (TypeError, ValueError):
        return "unknown"


@app.route("/api/debug/library-snapshot", methods=["POST"])
def api_debug_library_snapshot():
    """Filtering & Scoring debug: what the current library snapshot holds."""
    cfg = load_config()
    lines = ["Library snapshot debug", ""]
    lines.append(f"USE_PLEX={bool(cfg.get('USE_PLEX'))} | USE_JELLYFIN={bool(cfg.get('USE_JELLYFIN'))}")
    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]
    lines.append("monitored paths (the snapshot only holds movies under these): "
                 + (", ".join(monitor_dirs) or "(none)"))
    lines.append(f"IMDb ratings dataset on disk: {imdb_ratings_path().exists()}")

    lines.append("")
    data, pool_err = _read_library_snapshot()
    if pool_err:
        lines.append(f"library snapshot: ({'missing — run a Simulate to build it' if pool_err == 'missing' else 'unreadable'})")
        return jsonify({"ok": True, "text": "\n".join(lines)})
    movies = data.get("movies") or []
    rated = sum(1 for m in movies if m.get("rating") is not None)
    protected = sum(1 for m in movies if m.get("protected"))
    favorite = sum(1 for m in movies if m.get("favorite"))
    unplayed = sum(1 for m in movies if not m.get("plays"))
    lines.append(f"library snapshot: built {_fmt_epoch(data.get('built_at'))} | {len(movies)} movies | "
                 f"rated={rated} | protected={protected} | favorite={favorite} | unplayed={unplayed}")
    lines.append("")
    # The snapshot is the ENTIRE library — cap the per-movie dump so the debug
    # popup stays usable on big collections.
    _dump_cap = 1000
    lines.append("Entries (scoring inputs per movie):")
    for m in movies[:_dump_cap]:
        lines.append(f"  {m.get('title') or '(no title)'} ({m.get('year') or '?'}) | "
                     f"rating={m.get('rating')} votes={m.get('votes')} | plays={m.get('plays')} "
                     f"users={m.get('users')} | last_played={_fmt_epoch(m.get('last_played'))} | "
                     f"added={_fmt_epoch(m.get('added_at'))} | size={m.get('size_gb')} GB"
                     + (" | PROTECTED" if m.get("protected") else "")
                     + (" | FAVORITE" if m.get("favorite") else ""))
    if len(movies) > _dump_cap:
        lines.append(f"  … and {len(movies) - _dump_cap} more (first {_dump_cap} shown)")
    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/cache", methods=["POST"])
def api_debug_cache():
    """Advanced/Cache debug: a readable dump of what the store currently holds — the
    library-snapshot summary, the marked & eligible deletion queue + its plan stamp,
    dashboard storage stats, and the top-level bookkeeping keys. Mirrors the other
    /api/debug/* popups (returns {ok, text} for prRunDebug)."""
    p = db_path()
    lines = ["Cache contents debug", ""]
    try:
        st = p.stat()
        lines.append(f"store: {p} | {st.st_size:,} bytes | modified {_fmt_epoch(st.st_mtime)}")
    except OSError:
        lines.append(f"store: {p} | not present (cleared on startup — run a Simulate to rebuild)")
        return jsonify({"ok": True, "text": "\n".join(lines)})
    try:
        cache = db.read_cache_dict(p)
        if not isinstance(cache, dict):
            raise ValueError("store did not compose to an object")
    except Exception as e:
        lines.append(f"  (unreadable — {e})")
        return jsonify({"ok": True, "text": "\n".join(lines)})

    lines.append("top-level keys: " + (", ".join(sorted(cache.keys())) or "(none)"))
    lines.append(f"code_checksum: {cache.get('code_checksum')!r}")
    lines.append(f"last_cleanup_date: {cache.get('last_cleanup_date')!r}")

    # Library snapshot (the Filtering & Scoring table) — summary only; the Filtering
    # & Scoring "Debug" button dumps it per-movie.
    lines.append("")
    snap = cache.get("library_snapshot")
    if isinstance(snap, dict):
        sm = snap.get("movies") or []
        dirs = snap.get("monitor_dirs")
        lines.append(f"library_snapshot: built {_fmt_epoch(snap.get('built_at'))} | {len(sm)} movies"
                     + (f" | monitor_dirs={dirs}" if dirs else ""))
    else:
        lines.append("library_snapshot: (none — run a Simulate to build it)")

    # Deletion queue + plan-currency stamp (the store's queue table + pending meta).
    lines.append("")
    pend = cache.get("pending")
    if isinstance(pend, dict):
        entries = pend.get("entries") if isinstance(pend.get("entries"), dict) else {}
        marked = sum(1 for e in entries.values() if isinstance(e, dict) and e.get("marked_at") is not None)
        lines.append(f"pending (deletion queue): schema={pend.get('schema')} | {len(entries)} entries | "
                     f"{marked} marked (delay clock running) | {len(entries) - marked} eligible")
        stamp = pend.get("plan_config")
        if isinstance(stamp, dict):
            lines.append("  plan_config stamp: " + ", ".join(f"{k}={v}" for k, v in sorted(stamp.items())))
        else:
            lines.append(f"  plan_config stamp: {stamp!r}")
        if pend.get("monitor_dirs") is not None:
            lines.append(f"  monitor_dirs: {pend.get('monitor_dirs')}")
        _cap = 200
        lines.append(f"  entries (stored deletion order, first {_cap} shown):")
        for pth, e in list(entries.items())[:_cap]:
            e = e if isinstance(e, dict) else {}
            marked_at = e.get("marked_at")
            tag = "marked  " if marked_at is not None else "eligible"
            size_gb = round((e.get("size_bytes") or 0) / 1e9, 2)
            lines.append(f"    [{tag}] {e.get('title') or pth} | score={e.get('score')} | "
                         f"size={size_gb} GB | marked_at={_fmt_epoch(marked_at) if marked_at else '—'}")
        if len(entries) > _cap:
            lines.append(f"    … and {len(entries) - _cap} more (first {_cap} shown)")
    else:
        lines.append("pending (deletion queue): (none — run a Simulate to build it)")

    # Dashboard storage stats.
    lines.append("")
    ds = cache.get("dashboard_stats")
    if isinstance(ds, dict):
        lines.append("dashboard_stats: " + ", ".join(f"{k}={v}" for k, v in sorted(ds.items())))
    else:
        lines.append(f"dashboard_stats: {ds!r}")

    # Metadata cache (summary only — it can be thousands of rows).
    meta_cache = cache.get("movies")
    if isinstance(meta_cache, dict):
        lines.append("")
        lines.append(f"metadata_cache: {len(meta_cache)} movie(s) | config_hash={cache.get('config_hash')!r}")

    # Any top-level keys not covered above, so nothing in the store is hidden.
    _known = {"code_checksum", "last_cleanup_date", "library_snapshot", "pending",
              "dashboard_stats", "movies", "config_hash"}
    other = sorted(k for k in cache.keys() if k not in _known)
    if other:
        lines.append("")
        lines.append("other keys: " + ", ".join(other))

    return jsonify({"ok": True, "text": "\n".join(lines)})


# ── Sanitized diagnostic report ───────────────────────────────────────────────
# "Create report" on the Config page: a full snapshot of config, connections, API
# samples, and engine state, with everything personal/identifiable replaced by
# stable hash tokens — safe to attach to a bug report. The same value always maps to
# the same token, so cross-server path/title comparisons still line up within one
# report. The report is downloaded via the browser and never written to disk.

_REPORT_SECRET_KEYS = ("TAUTULLI_API_KEY", "PLEX_TOKEN", "JELLYFIN_API_KEY", "RADARR_API_KEY")
_REPORT_URL_KEYS = ("TAUTULLI_URL", "PLEX_URL", "JELLYFIN_URL", "RADARR_URL", "IMDB_RATINGS_URL")


class _ReportSanitizer:
    def __init__(self, cfg: dict):
        # Raw values that must never appear anywhere in the report, even inside
        # error messages: secrets, configured URLs, and their hostnames.
        self._scrub: list[tuple[str, str]] = []
        for key in _REPORT_SECRET_KEYS:
            value = str(cfg.get(key) or "").strip()
            if value:
                self._scrub.append((value, f"<{key.lower()}>"))
        for key in _REPORT_URL_KEYS:
            value = str(cfg.get(key) or "").strip()
            if not value or key == "IMDB_RATINGS_URL":
                continue
            self._scrub.append((value, f"<{key.lower()}>"))
            try:
                host = urlparse(value).hostname
            except Exception:
                host = None
            if host:
                self._scrub.append((host, "<host>"))

    def token(self, value, prefix: str = "t") -> str:
        raw = str(value or "").strip()
        if not raw:
            return "(blank)"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"<{prefix}:{digest}>"

    def url(self, value) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "(blank)"
        try:
            parts = urlparse(raw)
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme or 'http'}://<host>{port}"
        except Exception:
            return "<url>"

    def path(self, value) -> str:
        """Keep a path's structure (root, depth, extension) but tokenize every other
        segment. The same segment always maps to the same token, so mount mismatches
        stay diagnosable without exposing titles."""
        raw = str(value or "").strip().replace("\\", "/")
        if not raw:
            return "(blank)"
        lead = "/" if raw.startswith("/") else ""
        parts = [seg for seg in raw.split("/") if seg]
        out = []
        for i, seg in enumerate(parts):
            if i == 0:
                out.append(seg)
                continue
            stem, ext = os.path.splitext(seg)
            out.append(self.token(stem, "p") + (ext if i == len(parts) - 1 else ""))
        return lead + "/".join(out)

    def redact(self, text: str) -> str:
        """Defensively de-identify a free-form line (a log or engine message) that can
        embed unpredictable private values: quoted/bracketed names (collections,
        titles) and absolute filesystem paths. Registered secrets/hosts are scrubbed
        too. Used where raw text is echoed and the specific values aren't known ahead
        of time."""
        raw = str(text or "")
        # Quoted names first ('Keep Forever', "The Matrix") — tokenize the inner value
        # so bracketed collection/title lists never surface verbatim.
        raw = re.sub(r"'[^']+'|\"[^\"]+\"",
                     lambda m: self.token(m.group(0)[1:-1], "name"), raw)
        # Then absolute paths of depth ≥2 (/library/movies/…): keep structure, tokenize
        # the segments. Segments MUST allow spaces — movie folders/filenames routinely
        # contain them ("The Grey (2012)"), and stopping at the first space would leak
        # the title after it. We still stop at field delimiters (| , : and quotes) so a
        # path ending a "key=… | key=…" line doesn't swallow the next field; a path
        # followed by bare prose over-tokenizes the prose — the safe
        # (never-under-redact) direction for a sanitizer.
        raw = re.sub(r"(?:/[^/'\"|,:]+){2,}/?",
                     lambda m: self.path(m.group(0)), raw)
        return self.scrub(raw)

    def scrub(self, text: str) -> str:
        for raw, placeholder in sorted(self._scrub, key=lambda t: -len(t[0])):
            text = text.replace(raw, placeholder)
        return text


def _build_debug_report() -> str:
    cfg = load_config()
    s = _ReportSanitizer(cfg)
    L: list[str] = []
    add = L.append

    add("MediaReducer diagnostic report (sanitized)")
    add(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    add(f"mediareducer: {APP_VERSION}")
    add(f"python: {sys.version.split()[0]}")
    add("titles, names, hosts, keys, and path segments are replaced by stable")
    add("hash tokens — the same value maps to the same token within this report.")
    add("")

    add("=" * 60)
    add("CONFIG")
    add("=" * 60)
    for key in sorted(cfg.keys()):
        value = cfg.get(key)
        if key in _REPORT_SECRET_KEYS:
            add(f"  {key} = {'<set>' if str(value or '').strip() else '(blank)'}")
        elif key in _REPORT_URL_KEYS and key != "IMDB_RATINGS_URL":
            add(f"  {key} = {s.url(value)}")
        elif key == "MONITOR_DIRS":
            add(f"  {key} = [{', '.join(s.path(d) for d in (value or []))}]")
        elif key in ("PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS"):
            names = value if isinstance(value, (list, set, tuple)) else [value]
            add(f"  {key} = [{', '.join(s.token(n, 'name') for n in names if n)}]")
        elif key in ("_RADARR_DETECTED_SECTION_NAME",):
            add(f"  {key} = {s.token(value, 'name')}")
        elif key == "OUTPUT_DIR":
            add(f"  {key} = {value}")
        else:
            add(f"  {key} = {value!r}")

    add("")
    add("=" * 60)
    add("SCHEDULER & CLOCK")
    add("=" * 60)
    add(f"  run mode: {cfg.get('RUN_MODE')!r}")
    if cfg.get("RUN_MODE") == "paused" and cfg.get("_RUN_MODE_AUTOPAUSE_REASON"):
        add(f"  auto-pause reason: {cfg.get('_RUN_MODE_AUTOPAUSE_REASON')}")
    add(f"  TIME_ZONE setting: {cfg.get('TIME_ZONE')!r}")
    add(f"  effective process zone: {_server_time_zone_name()} (host zone: {_host_time_zone_name()})")
    add(f"  process clock now: {time.strftime('%Y-%m-%d %H:%M:%S %z')} (tzname={'/'.join(time.tzname)})")
    add(f"  daily run time: {_daily_run_time(cfg)} · delete delay: {_delete_delay_days(cfg)} day(s)")
    try:
        job = scheduler.get_job("engine")
        if job is None:
            add("  scheduler job 'engine': (not registered)")
        else:
            nxt = getattr(job, "next_run_time", None)
            add(f"  next scheduler tick: {nxt.strftime('%Y-%m-%d %H:%M:%S %z') if nxt else '(paused)'}")
            add(f"  tick interval: every {SCHEDULE_INTERVAL_MINUTES} min")
    except Exception as e:
        add(f"  scheduler: unreadable — {s.redact(str(e))}")

    add("")
    add("=" * 60)
    add("CONNECTION HEALTH (fresh probe)")
    add("=" * 60)
    try:
        health = _connection_health_state(cfg, probe=True)
        add(f"  critical_ok={health.get('critical_ok')} | severity={health.get('severity')}")
        add(f"  plex_connected={health.get('plex_connected')} | tautulli_connected={health.get('tautulli_connected')} | "
            f"jellyfin_connected={health.get('jellyfin_connected')} | radarr_connected={health.get('radarr_connected')}")
        for err in health.get("errors") or []:
            add(f"  ERROR: {s.redact(str(err))}")
        for warn in health.get("warnings") or []:
            add(f"  warning: {s.redact(str(warn))}")
        for compat in health.get("media_path_compatibility") or []:
            add(f"  path compatibility [{compat.get('server')}]: matched {compat.get('matched')}/{compat.get('checked')}")
            for ex in compat.get("resolved_examples") or []:
                add(f"    {s.path(ex.get('reported'))} -> {s.path(ex.get('resolved'))}")
            for ex in compat.get("unmatched_examples") or []:
                add(f"    UNMATCHED: {s.path(ex)}")
        appdata = health.get("appdata") or {}
        for name, state in sorted(appdata.items()):
            if isinstance(state, dict):
                add(f"  appdata {name}: mounted={state.get('mounted')} ok={state.get('ok')}")
    except Exception as e:
        add(f"  probe failed: {s.redact(str(e))}")

    add("")
    add("=" * 60)
    add("FILESYSTEM & STORAGE")
    add("=" * 60)
    disk = disk_stats()
    if disk:
        add(f"  {FILESYSTEM_CHECK_PATH} disk: used={disk.get('used_gb')} GB / total={disk.get('total_gb')} GB free={disk.get('free_gb')} GB")
    stats = library_stats()
    add(f"  cached library size: {stats.get('library_gb', 'n/a')} GB (refreshed {_fmt_epoch(stats.get('updated_at'))})")
    for d in cfg.get("MONITOR_DIRS") or []:
        add(f"  monitored: {s.path(d)} | exists={Path(d).exists()}")

    add("")
    add("=" * 60)
    add("SPACE VERDICT & FORECAST")
    add("=" * 60)
    try:
        lib_gb = stats.get("library_gb")
        free_gb = (disk or {}).get("free_gb")
        headroom_gb = _threshold_gb_or_none(cfg.get("HEADROOM_GB"))
        redline_gb = _threshold_gb_or_none(cfg.get("REDLINE_GB"))
        cap_gb = _threshold_gb_or_none(cfg.get("MAX_LIBRARY_GB"))

        def _breach(free, target):
            if free is None or target is None:
                return "n/a"
            # Match the engine's trigger (free <= target → a run would delete);
            # using < here would print "ok" at the exact boundary while the
            # "a run would delete now" line two rows down says yes.
            return "BREACH" if free <= target else "ok"

        add(f"  free space: {free_gb} GB")
        add(f"  headroom target: {headroom_gb} GB → {_breach(free_gb, headroom_gb)}")
        add(f"  redline target: {redline_gb} GB → {_breach(free_gb, redline_gb)}")
        if cap_gb is not None:
            add(f"  library size cap: {cap_gb} GB | library now {lib_gb} GB → "
                f"{'BREACH' if (lib_gb is not None and lib_gb > cap_gb) else 'ok'}")
        else:
            add("  library size cap: (disabled)")
        try:
            limits_exceeded = _deletion_limits_exceeded(cfg, disk, lib_gb)
            add(f"  a run would delete now: {'yes — limits exceeded' if limits_exceeded else 'no — limits satisfied'}")
        except Exception as e:
            add(f"  a run would delete now: (undetermined — {s.redact(str(e))})")
        live = _cleanup_button_state(cfg, disk).get("space_thresholds", {})
        add(f"  ok_for_simulate={live.get('ok_for_simulate')} ok_for_cleanup={live.get('ok_for_cleanup')} "
            f"safety_blocked={live.get('safety_blocked')} simulate_required={live.get('simulate_required')}")
    except Exception as e:
        add(f"  verdict unavailable: {s.redact(str(e))}")

    conn = _effective_connection_values(cfg)

    if cfg.get("USE_PLEX") and conn.get("tautulli_url") and conn.get("tautulli_key"):
        add("")
        add("=" * 60)
        add("TAUTULLI API")
        add("=" * 60)
        try:
            libraries = _tautulli_api_request(conn, "get_libraries", timeout=10) or []
            for lib in libraries:
                if isinstance(lib, dict):
                    add(f"  section {s.token(lib.get('section_name'), 'name')} [id={lib.get('section_id')}, "
                        f"type={lib.get('section_type')}, active={lib.get('is_active', 1)}, count={lib.get('count', 'n/a')}]")
            movie_sections = [l for l in libraries if isinstance(l, dict)
                              and l.get("section_type") == "movie" and l.get("is_active", 1)]
            for lib in movie_sections[:2]:
                data = _tautulli_api_request(conn, "get_library_media_info", timeout=20,
                                             section_id=lib.get("section_id"), section_type="movie",
                                             start=0, length=5, order_column="title", order_dir="asc")
                rows = (data or {}).get("data") or []
                add(f"  media info sample (section id={lib.get('section_id')}, total={((data or {}).get('recordsFiltered'))}):")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    add(f"    {s.token(row.get('title'), 'title')} ({row.get('year') or '?'}) | plays={row.get('play_count')} | "
                        f"last_played={_fmt_epoch(row.get('last_played'))} | added={_fmt_epoch(row.get('added_at'))} | "
                        f"size={row.get('file_size')} | file={s.path(row.get('file'))}")
                    add(f"      row keys: {', '.join(sorted(row.keys()))}")
        except Exception as e:
            add(f"  ERROR: {s.redact(str(e))}")

    if cfg.get("USE_JELLYFIN") and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        add("")
        add("=" * 60)
        add("JELLYFIN API")
        add("=" * 60)
        try:
            info = _jellyfin_get(conn["jellyfin_url"], conn["jellyfin_key"], "System/Info", timeout=8) or {}
            add(f"  server version: {info.get('Version', 'n/a')}")
            data = _jellyfin_get(conn["jellyfin_url"], conn["jellyfin_key"], "Items", {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "Fields": "Path,MediaSources,DateCreated,ProviderIds", "Limit": 5,
            }, timeout=10) or {}
            add(f"  movie count: {data.get('TotalRecordCount', 'n/a')}")
            users = _jellyfin_get(conn["jellyfin_url"], conn["jellyfin_key"], "Users", timeout=8) or []
            add(f"  user count: {len(users)}")
            for item in _jellyfin_items_from_payload(data)[:5]:
                prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
                add(f"    {s.token(item.get('Name'), 'title')} ({item.get('ProductionYear') or '?'}) | "
                    f"imdb={'yes' if prov.get('imdb') else 'no'} tmdb={'yes' if prov.get('tmdb') else 'no'} | "
                    f"path={s.path(item.get('Path'))}")
        except Exception as e:
            add(f"  ERROR: {s.redact(str(e))}")

    if conn.get("radarr_url") and conn.get("radarr_key"):
        add("")
        add("=" * 60)
        add("RADARR API")
        add("=" * 60)
        try:
            status = _json_request(f"{conn['radarr_url'].rstrip('/')}/api/v3/system/status",
                                   headers={"X-Api-Key": conn["radarr_key"]}, timeout=8) or {}
            add(f"  version: {status.get('version', 'n/a')}")
            movies = _json_request(f"{conn['radarr_url'].rstrip('/')}/api/v3/movie",
                                   headers={"X-Api-Key": conn["radarr_key"]}, timeout=20) or []
            add(f"  movie count: {len(movies)}")
            roots = _json_request(f"{conn['radarr_url'].rstrip('/')}/api/v3/rootfolder",
                                  headers={"X-Api-Key": conn["radarr_key"]}, timeout=8) or []
            for root in roots:
                if isinstance(root, dict):
                    add(f"  root folder: {s.path(root.get('path'))}")
        except Exception as e:
            add(f"  ERROR: {s.redact(str(e))}")
        add(f"  cleanup enabled: {cfg.get('RADARR_OVERSEERR_SECTION_ID') is not None} "
            f"(section={cfg.get('RADARR_OVERSEERR_SECTION_ID')!r}, "
            f"detected id={cfg.get('_RADARR_DETECTED_SECTION_ID')!r} via {cfg.get('_RADARR_DETECTED_SECTION_METHOD')!r})")

    add("")
    add("=" * 60)
    add("PROTECTED COLLECTIONS (resolution — counts only, no names)")
    add("=" * 60)

    def _configured_count(config_key):
        selected = cfg.get(config_key) or []
        if isinstance(selected, str):
            selected = [p.strip() for p in selected.split(",") if p.strip()]
        return selected

    def _collection_resolution(label, config_key, use_flag, connected, names_fn):
        selected = _configured_count(config_key)
        if not use_flag:
            add(f"  {label}: server disabled | {len(selected)} configured")
            return
        if not connected:
            add(f"  {label}: not connected | {len(selected)} configured (cannot resolve)")
            return
        try:
            available = set(names_fn())
        except Exception as e:
            add(f"  {label}: {len(selected)} configured | resolve failed — {s.redact(str(e))}")
            return
        missing = [n for n in selected if n not in available]
        add(f"  {label}: {len(selected)} configured | {len(selected) - len(missing)} resolved | "
            f"{len(missing)} missing (renamed/removed) | server lists {len(available)} collections")

    _collection_resolution(
        "Plex", "PROTECTED_COLLECTIONS", bool(cfg.get("USE_PLEX")),
        bool(conn.get("plex_url") and conn.get("plex_token")),
        lambda: _plex_collection_names(conn["plex_url"], conn["plex_token"]))
    _collection_resolution(
        "Jellyfin", "JELLYFIN_PROTECTED_COLLECTIONS", bool(cfg.get("USE_JELLYFIN")),
        bool(conn.get("jellyfin_url") and conn.get("jellyfin_key")),
        lambda: _jellyfin_boxset_names(conn["jellyfin_url"], conn["jellyfin_key"]))

    add("")
    add("=" * 60)
    add("ENGINE STATE FILES")
    add("=" * 60)
    try:
        cache = db.read_cache_dict(db_path())
        movies = cache.get("movies") or {}
        add(f"  store: {len(movies)} cached movie entries | last_cleanup_date={cache.get('last_cleanup_date')!r} | "
            f"dashboard_stats={'yes' if isinstance(cache.get('dashboard_stats'), dict) else 'no'}")
    except Exception as e:
        add(f"  store: unreadable — {s.redact(str(e))}")
    pool, pool_err = _read_library_snapshot()
    if pool_err:
        add(f"  library snapshot: ({pool_err})")
    else:
        pm = pool.get("movies") or []
        add(f"  library snapshot: {len(pm)} movies | built {_fmt_epoch(pool.get('built_at'))} | "
            f"rated={sum(1 for m in pm if m.get('rating') is not None)} | "
            f"protected={sum(1 for m in pm if m.get('protected'))} | favorite={sum(1 for m in pm if m.get('favorite'))}")
    try:
        prog = json.loads(progress_path().read_text(encoding="utf-8"))
        title = prog.get("current_title")
        message = str(prog.get("message") or "")
        if title:
            message = message.replace(str(title), s.token(title, "title"))
        # Engine abort messages embed the movie as "title=<name> |" — tokenize it.
        message = re.sub(r"title=([^|]+)",
                         lambda m: "title=" + s.token(m.group(1).strip(), "title"), message)
        # Defensively de-identify any remaining quoted names (e.g. a protected
        # collection list) or paths the specific-field passes above didn't catch.
        message = s.redact(message)
        add(f"  progress.json: status={prog.get('status')} phase={prog.get('phase')} mode={prog.get('mode')} | "
            f"scanned={prog.get('scanned')}/{prog.get('total')} eligible={prog.get('eligible')} "
            f"deleted={prog.get('deleted')} | ended {_fmt_epoch(prog.get('ended_at'))}")
        add(f"    message: {message}")
    except FileNotFoundError:
        add("  progress.json: (missing — no run yet)")
    except Exception as e:
        add(f"  progress.json: unreadable — {s.redact(str(e))}")
    try:
        archived = sorted(p.name for p in logs_dir().glob("*.log"))
        add(f"  archived run logs: {len(archived)}" + (f" (latest {archived[-1]})" if archived else ""))
    except Exception:
        add("  archived run logs: (unreadable)")
    try:
        deleted_lines = deleted_path().read_text(encoding="utf-8").strip().splitlines()
        add(f"  deleted.log: {len(deleted_lines)} entries (content not included)")
    except FileNotFoundError:
        add("  deleted.log: (missing)")
    except Exception:
        add("  deleted.log: (unreadable)")
    imdb_path = imdb_ratings_path()
    if imdb_path.exists():
        try:
            st = imdb_path.stat()
            age_days = max(0.0, (time.time() - st.st_mtime) / 86400.0)
            max_age = cfg.get("IMDB_RATINGS_MAX_AGE_DAYS")
            stale = isinstance(max_age, (int, float)) and age_days > float(max_age)
            with imdb_path.open("r", encoding="utf-8", errors="replace") as fh:
                rows = max(0, sum(1 for _ in fh) - 1)   # minus the header row
            add(f"  IMDb ratings dataset: present | {rows} ratings | "
                f"updated {_fmt_epoch(st.st_mtime)} ({age_days:.1f} days old"
                + (f", STALE > {max_age}d" if stale else "") + ")")
        except Exception as e:
            add(f"  IMDb ratings dataset: present (details unreadable — {s.redact(str(e))})")
    else:
        add("  IMDb ratings dataset: missing")

    add("")
    add("=" * 60)
    add("DELETION PLAN & CURRENCY")
    add("=" * 60)
    try:
        data = _pending_file_data()
        forecast = pending_delete_forecast(cfg)
        plan_current = _pending_plan_current(cfg)
        add(f"  pending plan file: {'present' if data else 'missing'}")
        add(f"  plan current under saved config: {plan_current}"
            + ("" if plan_current else "  → Automatic Cleanup is LOCKED until a new Simulate"))
        if data and not plan_current:
            # Name only WHICH keys drifted (key names, never their values) so the
            # report stays private while still pinpointing the staleness cause.
            reasons = []
            if (not isinstance(data.get("monitor_dirs"), list)
                    or sorted(str(d) for d in data.get("monitor_dirs") or []) != _normalized_monitor_dirs(cfg)):
                reasons.append("monitor_dirs")
            stamp = data.get("plan_config")
            if not isinstance(stamp, dict) or set(stamp) != set(_PLAN_CONFIG_KEYS):
                reasons.append("plan_config stamp shape")
            else:
                def _n(v):
                    if v is None or isinstance(v, (bool, str)):
                        return v
                    if isinstance(v, list):
                        return sorted(str(x) for x in v)
                    try:
                        return round(float(v), 3)
                    except (TypeError, ValueError):
                        return "invalid"
                reasons += [k for k in _PLAN_CONFIG_KEYS if _n(stamp.get(k)) != _n(cfg.get(k))]
            add(f"  staleness cause (changed keys): {', '.join(reasons) or 'unknown'}")
        add(f"  marked queue: {forecast['count']} total | {forecast['ripe']} deletable now")
        if forecast["event_on"]:
            add(f"  next deletion event: {forecast['event_count']} movie(s), "
                f"{_format_reclaimed_size(forecast['event_bytes'])} on {forecast['event_on']}")
        # Sample rows carry NO title, NO path, NO host — only non-identifying
        # score / size / schedule fields, per the privacy requirement.
        entries = pending_deletion_entries(cfg)
        if entries:
            add("  sample marked entries (up to 5, de-identified):")
            for e in entries[:5]:
                add(f"    score={e.get('score')} | size={e.get('size') or 'n/a'} | "
                    f"delete_on={e.get('delete_on')} | days_remaining={e.get('days_remaining')}")
    except Exception as e:
        add(f"  plan state unavailable: {s.redact(str(e))}")

    add("")
    add("=" * 60)
    add("RECENT ERRORS (last run log)")
    add("=" * 60)
    try:
        lp = log_path()
        if not lp.exists():
            add("  (no run log yet)")
        else:
            rx = _LOG_SECTION_RES["errors"]
            flagged = [ln.rstrip("\n") for ln in lp.read_text(encoding="utf-8").splitlines()
                       if ln.strip() and rx.search(ln)]
            if not flagged:
                add("  (none flagged in the last run)")
            else:
                add(f"  {len(flagged)} flagged line(s); showing the last {min(len(flagged), 20)}:")
                for ln in flagged[-20:]:
                    add(f"  {s.redact(ln)}")
    except Exception as e:
        add(f"  errors unreadable — {s.redact(str(e))}")

    add("")
    add("run/refresh flags at report time: "
        f"run_active={_run_active} summary_active={_summary_active}")

    return s.scrub("\n".join(L)) + "\n"


@app.route("/api/debug/report", methods=["POST"])
def api_debug_report():
    """Build a full sanitized diagnostic report and return it for the browser to
    download — nothing is written to the container filesystem."""
    # Mid-run the report would capture a half-written run log and a moving
    # deletion plan, and its live connection probes compete with the engine's
    # own API traffic — a snapshot like that misleads the bug report it's for.
    if _run_active:
        return jsonify({"ok": False, "error": "A run is active. Try again when it finishes."}), 409
    try:
        text = _build_debug_report()
        name = f"debug_report_{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        return jsonify({"ok": True, "filename": name, "text": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/collections", methods=["POST"])
def api_collections():
    """List Plex collections and Jellyfin collections for the protected-collection pickers."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    out = {
        "plex":     {"enabled": bool(cfg.get("USE_PLEX")),     "ok": False, "names": [], "error": "", "missing": []},
        "jellyfin": {"enabled": bool(cfg.get("USE_JELLYFIN")), "ok": False, "names": [], "error": "", "missing": []},
    }

    def _flag_missing(server_key: str, config_key: str):
        # Report saved selections the server no longer lists, but NEVER auto-remove
        # them: silently dropping a protection when a collection is renamed once let the
        # next run delete the very movies it was guarding. A stale name stays saved
        # (runs fail closed on it and say why); the user unchecks it deliberately.
        available = set(out[server_key].get("names") or [])
        selected = cfg.get(config_key) or []
        if isinstance(selected, str):
            selected = [p.strip() for p in selected.split(",") if p.strip()]
        out[server_key]["missing"] = [name for name in selected if name not in available]

    if cfg.get("USE_PLEX"):
        if conn.get("plex_url") and conn.get("plex_token"):
            try:
                out["plex"]["names"] = _plex_collection_names(conn["plex_url"], conn["plex_token"])
                out["plex"]["ok"] = True
                _flag_missing("plex", "PROTECTED_COLLECTIONS")
            except Exception as e:
                out["plex"]["error"] = f"Could not read Plex collections: {e}"
        else:
            out["plex"]["error"] = "Add the Plex URL and token above and Save, then scan."
    if cfg.get("USE_JELLYFIN"):
        if conn.get("jellyfin_url") and conn.get("jellyfin_key"):
            try:
                out["jellyfin"]["names"] = _jellyfin_boxset_names(conn["jellyfin_url"], conn["jellyfin_key"])
                out["jellyfin"]["ok"] = True
                _flag_missing("jellyfin", "JELLYFIN_PROTECTED_COLLECTIONS")
            except Exception as e:
                out["jellyfin"]["error"] = f"Could not read Jellyfin collections: {e}"
        else:
            out["jellyfin"]["error"] = "Add the Jellyfin URL and API key above and Save, then scan."
    return jsonify(out)


def _connection_health_state(cfg: dict | None = None, *, probe: bool = False) -> dict:
    """Validate connection health for the services MediaReducer talks to.

    Checks selected-API reachability and whether connected media-server paths resolve
    to real files under the /library mount. Does NOT validate headroom, library caps,
    scoring, or other thresholds. Tautulli is required only when Plex is selected;
    Jellyfin is checked when selected; optional Plex/Radarr helpers are probed once
    their URL and key are present, so dependent UI sections stay locked until each API
    connects. Blank optional URLs raise no warning/error. MediaReducer-owned folders
    are also checked for read/write, since cache, logs, IMDb data, saves, and runs all
    depend on them."""
    cfg = cfg or load_config()
    mounts = _appdata_mount_state()
    filesystem = _filesystem_rw_state()
    conn = _effective_connection_values(cfg)
    errors: list[str] = []
    warnings: list[str] = []
    highlights: list[str] = []
    mount_highlights: list[str] = []
    cleanup_warning_msgs: list[str] = []
    optional_cleanup_warned = False
    tautulli_blocker = False

    cleanup_section_value = str(cfg.get("RADARR_OVERSEERR_SECTION_ID") or "").strip().lower()
    cleanup_enabled = cleanup_section_value not in ("", "none", "null")
    cleanup_auto_section = cleanup_enabled and cleanup_section_value == "auto"
    plex_connected = False
    tautulli_connected = False
    radarr_connected = False
    use_plex = bool(cfg.get("USE_PLEX"))
    use_jellyfin = bool(cfg.get("USE_JELLYFIN"))
    jellyfin_connected = False
    jellyfin_blocker = False
    media_path_blocker = False
    filesystem_blocker = False
    media_path_compatibility: list[dict] = []
    no_server_selected = not (use_plex or use_jellyfin)

    def dedupe(items):
        out = []
        seen = set()
        for item in items:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def add_error(message: str, fields: list[str] | tuple[str, ...] = (), mounts_to_highlight: list[str] | tuple[str, ...] = ()):  # keep order / de-dupe later
        errors.append(message)
        highlights.extend(fields)
        mount_highlights.extend(mounts_to_highlight)

    def add_warning(message: str, fields: list[str] | tuple[str, ...] = (), mounts_to_highlight: list[str] | tuple[str, ...] = (), cleanup: bool = False):
        nonlocal optional_cleanup_warned
        warnings.append(message)
        highlights.extend(fields)
        mount_highlights.extend(mounts_to_highlight)
        optional_cleanup_warned = optional_cleanup_warned or cleanup
        if cleanup:
            # Remembered Radarr state, shown only when cleanup is enabled, so it must
            # not count toward auto-opening the section.
            cleanup_warning_msgs.append(message)

    # Hand-edited config.json with invalid values: everything locks (runs and config
    # edits) until the values are reset or MediaReducer is reset.
    invalid_config = list(_CONFIG_FILE_ISSUES)
    if invalid_config:
        add_error(
            "config.json was edited outside MediaReducer and contains invalid values. "
            "Everything is locked until they are reset to defaults or MediaReducer is reset.",
        )
        for issue in invalid_config:
            add_error(f"{issue['key']} {issue['message']}.", [issue["key"]])

    if not filesystem.get("ok", True):
        filesystem_blocker = True
        # Include the captured OSError — it distinguishes a read-only mount from a
        # permissions problem.
        _fs_detail = (filesystem.get("errors") or [""])[0]
        add_error(
            "MediaReducer cannot read and write its config/log folders"
            + (f" — {_fs_detail}" if _fs_detail else "")
            + ". Check the /config mount and its permissions, then recheck.",
        )

    if no_server_selected:
        # Both servers off is saveable but non-functional — health stays red until one
        # media server API is configured. SERVER_SOFTWARE is a pseudo-field that
        # highlights the checkbox row.
        add_error(
            "No media server is enabled — select Plex or Jellyfin under Server software and configure its API.",
            ["SERVER_SOFTWARE"],
        )

    if use_plex:
        # Required: Tautulli URL/API key + reachable API. The /tautulli appdata mount
        # exists ONLY so Auto Detect can read the key from config.ini — the engine
        # talks to Tautulli over HTTP. So a missing mount warns (Auto Detect
        # unavailable) rather than disabling the URL/key fields, which used to leave
        # users who skipped the optional volume unable to type them in.
        tautulli_ok = bool(mounts["tautulli"].get("ok"))
        if not tautulli_ok:
            add_warning(
                "Tautulli appdata is not mounted — Auto Detect is unavailable. "
                "Enter the URL and API key manually.",
                mounts_to_highlight=["tautulli"],
            )
        # A blank URL fills from the server default once the key is present, so the key
        # is the one thing to ask for. A blank URL WITH a key means no default host
        # could be detected.
        if not conn.get("tautulli_key"):
            tautulli_blocker = True
            add_error(
                "Enter the Tautulli API key — the URL fills in automatically.",
                ["TAUTULLI_API_KEY"],
            )
        elif not conn.get("tautulli_url"):
            tautulli_blocker = True
            add_error(
                "Tautulli URL is missing and no default address could be detected — enter it manually.",
                ["TAUTULLI_URL"],
            )
        if probe and conn.get("tautulli_url") and conn.get("tautulli_key"):
            url = f"{conn['tautulli_url'].rstrip('/')}/api/v2?apikey={conn['tautulli_key']}&cmd=get_libraries"
            ok, msg = _probe_json(url, timeout=6)
            tautulli_connected = ok
            if not ok:
                tautulli_blocker = True
                add_error(
                    "Tautulli did not connect. Check the URL and API key.",
                    ["TAUTULLI_URL", "TAUTULLI_API_KEY"],
                    mounts_to_highlight=["tautulli"],
                )

        # Plex is optional and the token is its on/off switch: no token means Plex-only
        # features stay locked with no warning, whatever the URL holds. With a token, a
        # blank URL resolves to its default and real connection/auth problems surface.
        plex_url = conn.get("plex_url")
        plex_token = conn.get("plex_token")
        plex_has_url = bool(plex_url)
        plex_has_creds = bool(plex_url and plex_token)
        # Plex connection/auth problems highlight only the Plex URL/token. Protected
        # collections is a dependent feature: when saved Plex stops connecting, the UI
        # locks it like an empty state while keeping the user's collection list for when
        # Plex reconnects.
        plex_dependent_fields = []
        # No token = the user does not use direct Plex access: dependent features stay
        # locked silently, whether or not a URL was typed.
        if plex_token and not plex_has_url:
            add_warning(
                "Plex token is set but no URL default could be detected — enter the Plex URL.",
                ["PLEX_URL"],
            )
        if probe and plex_has_creds:
            url = f"{plex_url.rstrip('/')}/library/sections?X-Plex-Token={plex_token}"
            ok, msg = _probe_json(url, headers={"Accept": "application/json"}, timeout=6)
            plex_connected = ok
            if not ok:
                add_warning(
                    "Plex did not connect. Check the URL and token.",
                    ["PLEX_URL", "PLEX_TOKEN"] + plex_dependent_fields,
                    cleanup=cleanup_auto_section,
                )
                if cleanup_auto_section:
                    add_warning(
                        "Radarr section auto-detection needs Plex to connect.",
                        ["PLEX_URL", "PLEX_TOKEN"],
                        cleanup=True,
                    )

    # Jellyfin (native API), only checked when selected. Its API key must be created by
    # hand, so validate URL + key by probing /System/Info.
    if use_jellyfin:
        jf_url = conn.get("jellyfin_url")
        jf_key = conn.get("jellyfin_key")
        # Same shape as Tautulli: the key is the ask; the URL only surfaces when it
        # stayed blank because no default host could be detected.
        if not jf_key:
            jellyfin_blocker = True
            add_error("Enter the Jellyfin API key — the URL fills in automatically.", ["JELLYFIN_API_KEY"])
        elif not jf_url:
            jellyfin_blocker = True
            add_error("Jellyfin URL is missing and no default address could be detected — enter it manually.", ["JELLYFIN_URL"])
        if probe and jf_url and jf_key:
            ok, msg = _probe_json(
                f"{jf_url.rstrip('/')}/System/Info",
                headers={"Authorization": f'MediaBrowser Token="{jf_key}"', "X-Emby-Token": jf_key},
                timeout=6,
            )
            jellyfin_connected = ok
            if not ok:
                jellyfin_blocker = True
                add_error("Jellyfin did not connect. Check the URL and API key.", ["JELLYFIN_URL", "JELLYFIN_API_KEY"])

    # Radarr is optional and its API key is the on/off switch: no key means Radarr
    # integration stays locked with no warning, whatever the URL holds. With a key, a
    # blank URL resolves to its default and connection/auth problems surface.
    radarr_url = conn.get("radarr_url")
    radarr_key = conn.get("radarr_key")
    radarr_has_url = bool(radarr_url)
    radarr_has_creds = bool(radarr_url and radarr_key)
    # No key = the user does not use Radarr: cleanup stays locked silently, whether or
    # not a URL was typed.
    if radarr_key and not radarr_has_url:
        add_warning(
            "Radarr API key is set but no URL default could be detected — enter the Radarr URL. Optional cleanup is locked.",
            ["RADARR_URL"],
            cleanup=True,
        )
    if probe and radarr_has_creds:
        ok, msg = _probe_json(
            f"{radarr_url.rstrip('/')}/api/v3/system/status",
            headers={"X-Api-Key": radarr_key},
            timeout=6,
        )
        radarr_connected = ok
        if not ok:
            add_warning(
                "Radarr did not connect. Optional cleanup is locked.",
                ["RADARR_URL", "RADARR_API_KEY"],
                cleanup=True,
            )

    if probe:
        if use_plex and tautulli_connected:
            try:
                compat = _media_path_compatibility_state("Plex/Tautulli", _sample_tautulli_media_paths(conn))
                media_path_compatibility.append(compat)
                if compat["checked"] == 0:
                    add_warning("Plex/Tautulli connected, but no movie paths were available to check.")
                elif not compat["ok"]:
                    media_path_blocker = True
                    add_error(f"Plex paths do not line up with files under {FILESYSTEM_CHECK_PATH}. Check the media mounts, then recheck.")
            except Exception:
                add_warning("Could not check Plex media paths.")

        if use_jellyfin and jellyfin_connected:
            try:
                compat = _media_path_compatibility_state("Jellyfin", _sample_jellyfin_media_paths(conn))
                media_path_compatibility.append(compat)
                if compat["checked"] == 0:
                    add_warning("Jellyfin connected, but no movie paths were available to check.")
                elif not compat["ok"]:
                    media_path_blocker = True
                    add_error(f"Jellyfin paths do not line up with files under {FILESYSTEM_CHECK_PATH}. Check the media mounts, then recheck.")
            except Exception:
                add_warning("Could not check Jellyfin media paths.")

    # Threshold values, library-cap readiness, and other run-mode blockers live in the
    # Dashboard buttons, Config validation, and the engine's own safety checks. Only
    # API reachability and media path compatibility are decided here.
    errors = dedupe(errors)
    warnings = dedupe(warnings)
    highlights = dedupe(highlights)
    mount_highlights = dedupe(mount_highlights)

    # Radarr connection problems show on the Radarr URL/API fields — the Optional
    # Radarr cleanup box is never painted red here. Health checks only report
    # connection state; the save endpoint force-disables cleanup after SAVED
    # credentials fail a probe, not while typing.
    radarr_cleanup_forced_disabled = False
    # Live deletion is gated on every selected server being healthy. The engine reads
    # library and watch history from whichever servers are enabled (Plex via Tautulli,
    # and/or Jellyfin), so a Jellyfin-only setup can run once Jellyfin connects — no
    # Tautulli required.
    critical_ok = (
        (not invalid_config)
        and (not no_server_selected)
        and (not tautulli_blocker if use_plex else True)
        and (not jellyfin_blocker if use_jellyfin else True)
        and not media_path_blocker
        and not filesystem_blocker
    )
    if invalid_config:
        required_tooltip = "Fix the invalid config file on the Configuration page first."
    elif no_server_selected:
        # Shown on the dashboard run buttons too, where the server-software checkboxes
        # aren't visible — point at the fix, not at controls the user can't see.
        required_tooltip = "Fix the API connections on the Configuration page first."
    elif errors:
        required_tooltip = errors[0]
    else:
        required_tooltip = ""

    has_visible_issues = bool(errors) or bool(warnings)

    return {
        "ok": True,
        "critical_ok": critical_ok,
        "invalid_config": invalid_config,
        "severity": "error" if errors else ("warning" if warnings else "ok"),
        "summary": "Ready." if not errors and not warnings else ("Fix the errors below." if errors else "Ready with warnings."),
        "errors": errors,
        "warnings": warnings,
        "highlights": highlights,
        "radarr_cleanup_forced_disabled": radarr_cleanup_forced_disabled,
        "media_path_blocker": media_path_blocker,
        "filesystem_blocker": filesystem_blocker,
        "media_path_compatibility": media_path_compatibility,
        "has_visible_issues": has_visible_issues,
        "mount_highlights": mount_highlights,
        "probed": probe,
        "appdata": mounts,
        "radarr_connected": radarr_connected,
        "plex_connected": plex_connected,
        "tautulli_connected": tautulli_connected,
        "jellyfin_connected": jellyfin_connected,
        "required_tooltip": required_tooltip,
    }

# The connection-related change detectors share one shape: serialize a fixed set of
# config keys and compare the strings. `strip` compares values as trimmed strings
# (user-typed fields) rather than raw.
_API_CREDENTIAL_KEYS = (
    "USE_PLEX", "USE_JELLYFIN",
    "TAUTULLI_URL", "TAUTULLI_API_KEY", "PLEX_URL", "PLEX_TOKEN",
    "JELLYFIN_URL", "JELLYFIN_API_KEY",
    "RADARR_URL", "RADARR_API_KEY",
    "RADARR_OVERSEERR_SECTION_ID",
)


def _config_signature(cfg: dict | None, keys, *, strip: bool = False) -> str:
    cfg = cfg or load_config()
    if strip:
        payload = {key: (str(cfg.get(key)).strip() if cfg.get(key) is not None else None) for key in keys}
    else:
        payload = {key: cfg.get(key) for key in keys}
    return json.dumps(payload, sort_keys=True, default=str)


def _connection_health_signature(cfg: dict | None = None) -> str:
    """Hash only the values that can change connection health. Includes the
    invalid-config issues so a hand edit invalidates the cached health."""
    return _config_signature(cfg, _API_CREDENTIAL_KEYS + (
        "PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS", "OUTPUT_DIR",
    )) + json.dumps(_CONFIG_FILE_ISSUES, sort_keys=True)


def _api_config_signature(cfg: dict | None = None) -> str:
    """Hash user-edited API/connection settings that should pause Automatic Cleanup when changed."""
    return _config_signature(cfg, _API_CREDENTIAL_KEYS, strip=True)


def _radarr_section_detection_signature(cfg: dict | None = None) -> str:
    """Hash the saved credentials that can change Radarr/Plex section detection."""
    return _config_signature(cfg, ("PLEX_URL", "PLEX_TOKEN", "RADARR_URL", "RADARR_API_KEY"), strip=True)


def _refresh_connection_health_cache(cfg: dict | None = None, *, probe: bool = True) -> dict:
    """Run the connection check and store it for first-page display."""
    cfg = cfg or load_config()
    sig = _connection_health_signature(cfg)
    try:
        health = _connection_health_state(cfg, probe=probe)
    except Exception as e:
        health = {
            "ok": False,
            "critical_ok": False,
            "severity": "error",
            "summary": "Could not check connections.",
            "errors": [str(e)],
            "warnings": [],
            "highlights": [],
            "mount_highlights": [],
            "has_visible_issues": True,
            "probed": probe,
            "appdata": _appdata_mount_state(),
            "filesystem_blocker": True,
            "required_tooltip": "Connection check failed.",
            "invalid_config": list(_CONFIG_FILE_ISSUES),
        }
    with _connection_health_cache_lock:
        _connection_health_cache.update({
            "signature": sig,
            "health": health,
            "checked_at": time.time(),
        })
    return health


def _kick_startup_health_check_and_summary(cfg: dict | None = None):
    """On startup, probe connections first, then refresh library stats.

    The dashboard Storage card shows a cached library size from the store that can be
    stale if MediaReducer was stopped while the library changed. This worker runs the
    same health check the UI uses; when connections are good it immediately runs a quiet
    Summary/debug_info refresh so the store catches up rather than waiting for the
    first scheduled tick. That Summary also restarts the clock, so startup is a fresh
    interval reset."""
    cfg = dict(cfg or load_config())

    def _worker():
        # Probe connections either way (onboarding shows their status), but only
        # refresh the library Summary when there's something monitored — no paths
        # means nothing to size, matching the idle scheduler.
        health = _refresh_connection_health_cache(cfg, probe=True)
        if health.get("critical_ok", False) and _has_monitored_dirs(cfg):
            run_summary()

    threading.Thread(target=_worker, daemon=True, name="engine-startup-summary").start()


def _connection_health_for_ui(cfg: dict | None = None) -> dict:
    """Return the last matching connection probe without launching new probes.

    Connection health is sampled: once at startup, on Check for Errors, and after saved
    API settings change. Cached results are reused only for the same
    connection-relevant config signature, so an old good check can't keep Live enabled
    after the API settings changed."""
    cfg = cfg or load_config()
    sig = _connection_health_signature(cfg)
    with _connection_health_cache_lock:
        cached = _connection_health_cache.get("health")
        cached_sig = _connection_health_cache.get("signature")
    if cached and cached_sig == sig:
        return cached
    # A startup/manual probe may still be running. Return a fast, non-network view so
    # the page can render without creating a second background check.
    return _connection_health_state(cfg, probe=False)


def _norm_service_path(value: str | None) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    raw = re.sub(r"/+", "/", raw)
    return raw.rstrip("/").lower() if len(raw) > 1 else raw.lower()


def _path_under_or_same(path: str | None, root: str | None) -> bool:
    p = _norm_service_path(path)
    r = _norm_service_path(root)
    return bool(p and r and (p == r or p.startswith(r.rstrip("/") + "/")))


def _detect_radarr_plex_section(cfg: dict | None = None) -> dict:
    """Infer the Plex movie section Radarr manages by comparing paths."""
    cfg = cfg or load_config()
    conn = _effective_connection_values(cfg)
    result = {
        "ok": False,
        "section_id": None,
        "section_name": None,
        "method": None,
        "message": "Unable to detect Radarr's Plex section.",
        "sections": [],
        "counts": {},
    }

    radarr_url = (conn.get("radarr_url") or "").rstrip("/")
    radarr_key = conn.get("radarr_key") or ""
    plex_url = (conn.get("plex_url") or "").rstrip("/")
    plex_token = conn.get("plex_token") or ""
    if not radarr_url or not radarr_key:
        result["message"] = "Radarr URL/API key are not available."
        return result
    if not plex_url or not plex_token:
        result["message"] = "Plex URL/token are not available."
        return result

    try:
        movies = _json_request(f"{radarr_url}/api/v3/movie", headers={"X-Api-Key": radarr_key}, timeout=20) or []
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
        if movie.get("path") or movie.get("folderName"):
            radarr_paths.append(str(movie.get("path") or movie.get("folderName")))
        if movie.get("rootFolderPath"):
            radarr_roots.append(str(movie.get("rootFolderPath")))
    if not radarr_paths and not radarr_roots:
        result["message"] = "Radarr movies did not include usable paths."
        return result

    try:
        sep = "&" if "?" in "/library/sections" else "?"
        plex_data = _json_request(f"{plex_url}/library/sections{sep}X-Plex-Token={plex_token}", headers={"Accept": "application/json"}, timeout=15)
    except Exception as e:
        result["message"] = f"Could not read Plex sections: {e}"
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
        sid = str(sec.get("key") or "").strip()
        locations = [str(loc.get("path", "")) for loc in (sec.get("Location") or []) if loc.get("path")]
        if sid and locations:
            sections.append({"id": sid, "name": str(sec.get("title") or sid), "locations": locations})
    result["sections"] = sections
    if not sections:
        result["message"] = "Plex returned no movie sections with folder locations."
        return result

    def unique_winner(counts: dict):
        positive = [(sid, count) for sid, count in counts.items() if count > 0]
        if len(positive) != 1:
            return None
        sid, count = positive[0]
        return sid

    counts = {s["id"]: 0 for s in sections}
    for path in radarr_paths:
        for sec in sections:
            if any(_path_under_or_same(path, loc) for loc in sec["locations"]):
                counts[sec["id"]] += 1
    # A section wins if it's the ONLY one any Radarr movie path falls under. We don't
    # require every movie to match: a single stray unresolvable path shouldn't void an
    # otherwise-unambiguous match.
    winner = unique_winner(counts)
    method = "path-prefix"

    if not winner and radarr_roots:
        counts = {s["id"]: 0 for s in sections}
        roots = sorted(set(radarr_roots))
        for root in roots:
            for sec in sections:
                if any(_path_under_or_same(root, loc) or _path_under_or_same(loc, root) for loc in sec["locations"]):
                    counts[sec["id"]] += 1
        winner = unique_winner(counts)
        method = "root-prefix"

    if not winner:
        counts = {s["id"]: 0 for s in sections}
        names = []
        for value in sorted(set(radarr_roots or [])) or radarr_paths:
            name = Path(value.replace("\\", "/")).name.lower()
            if name:
                names.append(name)
        for name in names:
            for sec in sections:
                if name in {Path(loc).name.lower() for loc in sec["locations"] if Path(loc).name}:
                    counts[sec["id"]] += 1
        winner = unique_winner(counts)
        method = "folder-name"

    result["counts"] = counts
    if not winner:
        nonzero = {sid: count for sid, count in counts.items() if count}
        if len(nonzero) > 1:
            result["message"] = f"Radarr paths matched more than one Plex section: {nonzero}. Set the section manually."
        else:
            result["message"] = "Radarr paths did not match any Plex movie section. Set the section manually."
        return result

    sec = next((s for s in sections if s["id"] == winner), None)
    result.update({
        "ok": True,
        "section_id": winner,
        "section_name": sec["name"] if sec else winner,
        "method": method,
        "method_label": _radarr_section_method_label(method),
        "message": f"Detected: {winner}" + (f" ({sec['name']})" if sec else "") + (f" via {_radarr_section_method_label(method)}" if method else ""),
    })
    return result


NO_MONITORED_DIRS_MESSAGE = "No monitored library paths are set — add one on the Configuration page."
SIMULATE_REQUIRED_MESSAGE = "Over space limits — run Simulate to review the deletion plan first."


def _has_monitored_dirs(cfg: dict | None = None) -> bool:
    """Return True when at least one monitored library path is configured."""
    cfg = cfg or load_config()
    return bool(cfg.get("MONITOR_DIRS") or [])

def _cleanup_button_state(cfg: dict | None = None, disk: dict | None = None) -> dict:
    cfg = cfg or load_config()
    threshold_state = _space_threshold_state(cfg, disk)
    health = _connection_health_for_ui(cfg)
    has_monitored_dirs = _has_monitored_dirs(cfg)

    if not health.get("critical_ok", True):
        # A healthy media server is required for every run button, including Summary;
        # this takes priority over monitored-path and threshold warnings.
        msg = health.get("required_tooltip") or "Connect the selected media server first."
        # These tooltips show on the DASHBOARD's run buttons, but the error strings are
        # written for the Config page ("Enter the API key…", "…then recheck") — point at
        # where the fix lives.
        if "Configuration page" not in msg:
            msg = msg.rstrip(".") + " — fix it on the Configuration page."
        return {
            "summary_disabled": True,
            "summary_tooltip": msg,
            "simulate_disabled": True,
            "simulate_tooltip": msg,
            "cleanup_disabled": True,
            "cleanup_tooltip": msg,
            "debug_disabled": True,
            "debug_tooltip": msg,
            "space_satisfied": False,
            "space_thresholds": threshold_state,
            "connection_health": health,
        }

    if not has_monitored_dirs:
        # No monitored folders is the highest-priority non-connection Dashboard block:
        # nothing can be simulated or deleted until the allow-list has at least one path
        # (use / to monitor all of /library).
        return {
            "summary_disabled": False,
            "summary_tooltip": "",
            "simulate_disabled": True,
            "simulate_tooltip": NO_MONITORED_DIRS_MESSAGE,
            "cleanup_disabled": True,
            "cleanup_tooltip": NO_MONITORED_DIRS_MESSAGE,
            "debug_disabled": True,
            "debug_tooltip": NO_MONITORED_DIRS_MESSAGE,
            "space_satisfied": False,
            "space_thresholds": threshold_state,
            "connection_health": health,
        }

    # Everything configured and connected — but if every space limit is satisfied a
    # Cleanup would delete nothing, so ghost the Cleanup button with the reason. Simulate
    # never ghosts on satisfied limits in any mode: its job includes building/refreshing
    # the standing marked & eligible queue, which exists regardless of breach state.
    # Unknown values fail OPEN (buttons stay enabled; the engine is the authority
    # and no-ops safely), like _deletion_limits_exceeded.
    satisfied_msg = ""
    if threshold_state["ok_for_simulate"] or threshold_state["ok_for_cleanup"]:
        try:
            _lib_gb = library_stats().get("library_gb")
        except Exception:
            _lib_gb = None
        if not _deletion_limits_exceeded(cfg, disk, _lib_gb):
            satisfied_msg = "Space limits are satisfied — a run would delete nothing."

    # Over limits without a plan computed under these exact thresholds, the manual Live
    # Run ghosts too — it deletes immediately, so the user must have seen what a run
    # would remove. Simulate stays available: running it is how Live gets un-ghosted.
    # Redline-only mode never masks the plan requirement behind satisfied limits.
    rl_only = _redline_only_mode_cfg(cfg)
    simulate_required = bool(threshold_state.get("simulate_required")) and (rl_only or not satisfied_msg)

    return {
        "summary_disabled": False,
        "summary_tooltip": "",
        "simulate_disabled": not threshold_state["ok_for_simulate"],
        "simulate_tooltip": threshold_state["simulate_tooltip"],
        # Debug Cleanup replays the standing marked queue, so it needs a CURRENT
        # plan to exist: ghost it (like Live) until a Simulate has built/refreshed
        # the queue under the current settings. Uses the Simulate gate for hard
        # config errors, but ignores the live/safety thresholds (it deletes nothing).
        "debug_disabled": (not threshold_state["ok_for_simulate"]
                           or bool(threshold_state.get("simulate_required"))),
        "debug_tooltip": (threshold_state["simulate_tooltip"]
                          or ("Run Simulate first — Debug Cleanup replays the marked "
                              "& eligible queue a Simulate builds."
                              if threshold_state.get("simulate_required") else "")),
        "cleanup_disabled": (not threshold_state["ok_for_cleanup"] or bool(satisfied_msg)
                          or simulate_required),
        "cleanup_tooltip": (threshold_state["cleanup_tooltip"]
                         or (threshold_state.get("simulate_required_message", "") if simulate_required and rl_only else "")
                         or satisfied_msg
                         or (threshold_state.get("simulate_required_message", "") if simulate_required else "")),
        "space_satisfied": bool(satisfied_msg),
        "space_thresholds": threshold_state,
        "connection_health": health,
    }


def _cache_clear_state() -> dict:
    """Return whether the cache store exists and can be cleared from the UI."""
    p = db_path()
    state = {
        "exists": False,
        "can_clear": False,
        "path": str(p),
        "size_kb": None,
        "mtime": None,
        "reason": "A run is active." if _run_active else "No cache file found.",
    }
    if p.exists():
        try:
            st = p.stat()
            state.update({
                "exists": True,
                "can_clear": not _run_active,
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": _format_epoch_for_display(st.st_mtime),
                "mtime_ts": st.st_mtime,
                "reason": "A run is active." if _run_active else "Cache file is available to clear.",
            })
        except Exception as e:
            state.update({
                "exists": True,
                "can_clear": False,
                "reason": f"Could not inspect cache file: {e}",
            })
    return state

def _imdb_download_state() -> dict:
    """Return local IMDb ratings file status and rolling 24-hour throttle state."""
    p = imdb_ratings_path()
    state = {
        "exists": False,
        "download_locked": False,
        "can_download": not _run_active,
        "path": str(p),
        "size_mb": None,
        "mtime": None,
        "mtime_ts": None,
        "age_days": None,
        "next_download": None,
        "next_download_ts": None,
        "seconds_until_download": 0,
        "reason": "No local IMDb ratings file found." if not _run_active else "A run is active.",
    }

    if p.exists():
        try:
            st = p.stat()
            now = time.time()
            age_seconds = max(0.0, now - st.st_mtime)
            age_days = age_seconds / 86400
            next_download_ts = st.st_mtime + 86400
            seconds_until_download = max(0, int(next_download_ts - now + 0.999))
            download_locked = seconds_until_download > 0
            state.update({
                "exists": True,
                "download_locked": download_locked,
                "can_download": (not _run_active) and (not download_locked),
                "size_mb": round(st.st_size / 1_000_000, 1),
                "mtime": _format_epoch_for_display(st.st_mtime),
                "mtime_ts": st.st_mtime,
                "age_days": round(age_days, 2),
                "next_download": _format_epoch_for_display(next_download_ts),
                "next_download_ts": next_download_ts,
                "seconds_until_download": seconds_until_download,
                "reason": "Download is available." if not download_locked else "Already downloaded within the last 24 hours.",
            })
        except OSError as e:
            state.update({"can_download": False, "reason": f"Could not inspect local file: {e}"})

    if _run_active:
        state["can_download"] = False
        state["reason"] = "A run is active. Try again when it finishes."
    return state


def _filesystem_write_block_response(status: dict | None = None):
    filesystem = _filesystem_rw_state()
    if filesystem.get("ok", True):
        return None
    payload = {
        "ok": False,
        "message": "MediaReducer cannot read and write its config/log folders. Check the /config Docker mount, then recheck.",
        "filesystem": filesystem,
    }
    if status is not None:
        payload["status"] = status
    return jsonify(payload), 409


def _validate_imdb_url(url: str) -> str:
    url = str(url or "").strip()
    parsed = urlparse(url)
    if not url or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a valid HTTP(S) URL for the IMDB ratings dataset.")
    return url

def last_run_epoch() -> float | None:
    p = log_path()
    if p.exists():
        try:
            return p.stat().st_mtime
        except OSError:
            return None
    return None

def last_run_time() -> str | None:
    ts = last_run_epoch()
    return _format_epoch_for_display(ts) if ts is not None else None

_deleted_log_memo: tuple | None = None

def _deleted_log_lines() -> list[str]:
    """Read deleted.log, memoized by file identity. /api/status polls this every ~3s
    per open dashboard, and deleted.log only grows. Re-parse only when (mtime_ns, size)
    changes — engine appends and the Erase button both change it, so the cache can't go
    stale. Callers never mutate the returned list."""
    global _deleted_log_memo
    p = deleted_path()
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns, st.st_size)
    memo = _deleted_log_memo
    if memo and memo[0] == key:
        return memo[1]
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []
    _deleted_log_memo = (key, lines)
    return lines


def _deleted_line_size_bytes(match) -> int:
    try:
        return max(0, int(match.group("size_bytes") or 0))
    except (TypeError, ValueError):
        return 0

def deleted_count() -> int:
    return len(_deleted_log_lines())

_deleted_stats_memo: tuple | None = None


def deleted_stats() -> dict:
    """Totals over deleted.log, memoized on the same list object the line cache returns,
    so the regex re-sum only runs when the file changed (called on every /api/status
    poll)."""
    global _deleted_stats_memo
    lines = _deleted_log_lines()
    memo = _deleted_stats_memo
    if memo and memo[0] is lines:
        return memo[1]
    reclaimed_bytes = 0
    for raw_line in lines:
        m = _DELETED_LOG_RE.match(raw_line)
        if m:
            reclaimed_bytes += _deleted_line_size_bytes(m)
    stats = {
        "count": len(lines),
        "reclaimed_bytes": reclaimed_bytes,
        "reclaimed_label": _format_reclaimed_size(reclaimed_bytes),
    }
    _deleted_stats_memo = (lines, stats)
    return stats

def deleted_entries(limit: int | None = None) -> list[dict]:
    lines = _deleted_log_lines()
    if limit is not None:
        lines = lines[-max(0, int(limit)):]
    entries = []
    for raw_line in lines:
        m = _DELETED_LOG_RE.match(raw_line)
        if m:
            display_time = _format_log_timestamp_for_display(m.group("ts"))
            title = m.group("title").strip()
            path = m.group("path").strip()
            size_bytes = _deleted_line_size_bytes(m)
            size_label = _format_reclaimed_size(size_bytes) if size_bytes else ""
            # The WHY, recorded by newer engine versions: score, plays, last watch.
            # Older lines simply lack the fields.
            why_bits = []
            if m.group("score") is not None:
                why_bits.append(f"score {m.group('score')}")
            if m.group("plays") is not None:
                why_bits.append(f"{m.group('plays')} plays")
            if m.group("last_played") is not None:
                lp = m.group("last_played").strip()
                why_bits.append("never watched" if lp == "never" else f"last watched {lp.split(' ')[0]}")
            why = " · ".join(why_bits)
            line_parts = [display_time, title]
            if size_label:
                line_parts.append(size_label)
            if why:
                line_parts.append(why)
            line_parts.append(path)
            entries.append({
                "time": display_time,
                "title": title,
                "path": path,
                "size_bytes": size_bytes,
                "size": size_label,
                "why": why,
                "line": " | ".join(line_parts),
            })
        else:
            converted = _format_log_text_for_display(raw_line + "\n").strip()
            entries.append({"time": "", "title": "", "path": "", "size_bytes": 0, "size": "", "line": converted})
    return entries


def _pending_file_data() -> dict | None:
    """The marked & eligible queue document ({schema, entries, plan_config,
    monitor_dirs}), from the store's queue table + pending meta. Read through the
    memoized _cache_file_data(), so the frequent /api/status consults reuse the
    same once-per-version compose."""
    doc = _cache_file_data().get("pending")
    return doc if isinstance(doc, dict) else None


def _pending_raw() -> dict:
    """The engine's marked-for-deletion queue ({path: entry}), {} on any problem."""
    data = _pending_file_data()
    entries = data.get("entries") if data else None
    return entries if isinstance(entries, dict) else {}


def pending_count() -> int:
    return len(_pending_raw())


def _unschedule_pending_marks() -> int:
    """Null every running delay clock (marked_at) in the queue, keeping the
    queue itself — it is the standing eligible deletion order in every mode,
    and the engine's own satisfied-limits upkeep does exactly the same
    (_revalidate_pending_marks). Returns how many marks were unscheduled.
    Callers must ensure no run is active (the engine owns the queue during a
    run); the config-save path guards that by re-checking
    _run_active/_summary_active under _run_lock right at the write. One targeted
    UPDATE inside the WAL write transaction — every other table is untouched."""
    try:
        with db.transaction(db_path()) as conn:
            cur = conn.execute(
                "UPDATE queue SET marked_at=NULL WHERE marked_at IS NOT NULL")
            return cur.rowcount
    except Exception:
        return 0


def _normalized_monitor_dirs(cfg: dict) -> list[str]:
    out = []
    for raw in cfg.get("MONITOR_DIRS") or []:
        n = _normalize_library_path(str(raw))
        if n and n not in out:
            out.append(n)
    return sorted(out)


# Everything that changes WHAT a run would mark or delete AND can't be reconciled
# from the existing cache. A completed Simulate stamps the raw values of these
# keys (plus the monitored paths) into the plan; changing ANY of them ghosts both
# Live actions — arming automatic mode and the manual Cleanup button — until a
# fresh Simulate rebuilds the plan. Mirrored in engine.py (_PLAN_CONFIG_KEYS);
# keep the two lists identical.
#
# Deliberately EXCLUDED so they DON'T force a Simulate: PROTECTED_COLLECTIONS /
# JELLYFIN_PROTECTED_COLLECTIONS (the engine's 15-minute upkeep re-fetches the
# current protected set and drops newly-protected marks, and every real deletion
# re-verifies protection fresh — so a change is honored from the standing cache),
# and Radarr cleanup on/off (the queue carries the tmdb_id/section_id a delete
# needs). Monitored-path changes still force a Simulate — they're compared
# separately, via the plan's stamped monitor_dirs (see below).
_PLAN_CONFIG_KEYS = (
    "HEADROOM_GB", "REDLINE_GB", "REDLINE_ONLY_MODE", "MAX_LIBRARY_GB",
    "GRACE_PERIOD_DAYS", "SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES",
    "MAX_IMDB_RATING", "SCORE_BALANCE", "NEAR_TIE_PTS", "MAX_STALENESS_MONTHS",
    "MOVIE_EXTENSIONS",
)


def _pending_plan_current(cfg: dict, *, use_saved_file: bool = True) -> bool:
    """True when the marked-for-deletion queue holds a plan computed under the CURRENT
    deletion-affecting config — thresholds, filters, scoring, and monitored paths, all
    stamped by a completed Simulate (a stopped/partial one never writes). Any mismatch
    means the plan can't be trusted: Live locks until a fresh Simulate, which also pulls
    fresh play/last-played data in its full scan.

    use_saved_file=False compares against the PASSED cfg instead of the file on
    disk — the config-save arming gate must judge the plan against the candidate
    config being saved, not the old file it is about to replace (else one save
    could change thresholds AND arm automatic mode against a stamp only the old
    thresholds match)."""
    data = _pending_file_data()
    # An EMPTY stamped queue is a real plan: a completed Simulate that found
    # nothing eligible under these settings (all filtered/protected). Treating
    # it as stale would loop the user through Simulate forever.
    if not data or not isinstance(data.get("entries"), dict):
        return False
    if (not isinstance(data.get("monitor_dirs"), list)
            or sorted(str(d) for d in data["monitor_dirs"]) != _normalized_monitor_dirs(cfg)):
        return False
    stamp = data.get("plan_config")
    if not isinstance(stamp, dict) or set(stamp) != set(_PLAN_CONFIG_KEYS):
        return False

    # Compare RAW config.json values, like the engine's _plan_stamp_current: the
    # stamp holds the file's verbatim values, and load_config()'s normalization
    # (rounding SCORE_BALANCE, clamping cutoffs) would make a hand-edited
    # off-grid value mismatch its own stamp forever — every fresh Simulate would
    # instantly read as stale. Falls back to the normalized cfg only when the
    # file is unreadable (invalid config locks runs anyway).
    raw_saved = _read_saved_config_file() if use_saved_file else None
    src = raw_saved if isinstance(raw_saved, dict) else cfg
    current = {key: src.get(key) for key in _PLAN_CONFIG_KEYS}
    try:
        # Mirror the engine's one stamp normalization: a rating cutoff <= 0
        # reads as disabled (None) on both sides.
        _rating = current.get("MAX_IMDB_RATING")
        if _rating is not None and float(_rating) <= 0:
            current["MAX_IMDB_RATING"] = None
    except (TypeError, ValueError):
        pass

    def _norm(value):
        if value is None or isinstance(value, bool) or isinstance(value, str):
            return value
        if isinstance(value, list):
            return sorted(str(x) for x in value)
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return "invalid"

    return all(_norm(stamp.get(key)) == _norm(current.get(key))
               for key in _PLAN_CONFIG_KEYS)


def _redline_deficit_bytes(cfg: dict) -> int:
    """Bytes a Redline breach would need to free right now — 0 above the floor,
    outside redline-only mode, or when the disk can't be read."""
    if not _redline_only_mode_cfg(cfg):
        return 0
    try:
        free_gb = float((disk_stats() or {}).get("free_gb"))
        red_gb = float(cfg.get("REDLINE_GB"))
    except (TypeError, ValueError):
        return 0
    return int((red_gb - free_gb) * 1_000_000_000) if free_gb < red_gb else 0


def _marked_imminent_count(cfg: dict) -> int:
    """The 'marked' half of the Marked & Eligible display. Redline-only: the
    queue-front entries covering the current Redline deficit. Every other
    mode: the entries the engine actually scheduled (delay clock running)."""
    raw = _pending_raw()
    if not raw:
        return 0
    if not _redline_only_mode_cfg(cfg):
        return sum(1 for e in raw.values()
                   if isinstance(e, dict) and e.get("marked_at") is not None)
    deficit = _redline_deficit_bytes(cfg)
    if not deficit:
        return 0
    cum = n = 0
    for e in raw.values():
        if not isinstance(e, dict):
            continue
        if cum >= deficit:
            break
        cum += _entry_size_bytes(e)
        n += 1
    return n


def _entry_size_bytes(e: dict) -> int:
    """A queue entry's size_bytes as a safe non-negative int — the file is
    hand-editable, so a string, Infinity, or garbage value must read as 0,
    not crash every status poll."""
    try:
        return max(0, int(float(e.get("size_bytes") or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def pending_deletion_entries(cfg: dict | None = None, *, with_lines: bool = True) -> list[dict]:
    """The marked & eligible queue, in the file's own (deletion) order for every
    mode. MARKED entries are the ones actually scheduled: in normal modes the
    entries the engine clocked (marked_at set — delay info computed against the
    CURRENT delay, so shortening it moves every pending deletion up); in
    redline-only mode the queue prefix covering the current Redline deficit.
    Everything else is merely ELIGIBLE — visible deletion order with no dates.
    Pass an already-loaded cfg when you have one — the default reloads
    config.json.

    with_lines=False skips the per-entry display strings (title, size label,
    "when" text, formatted timestamp, joined line) — the /api/status forecast
    reads only marked_at/days_remaining/delete_on/size_bytes and pays for none
    of that formatting on a queue that can be thousands of entries."""
    raw = _pending_raw()
    if not raw:
        return []
    cfg = cfg or load_config()
    rl_only = _redline_only_mode_cfg(cfg)
    delay = _delete_delay_days(cfg)
    now = time.time()
    today = datetime.now().date()
    _deficit_bytes = _redline_deficit_bytes(cfg)
    _marked_bytes = 0
    # The display format is one config value; resolve it once, not per entry.
    _fmt = _request_time_format() if with_lines else None
    out = []
    for i, (path, e) in enumerate(raw.items(), start=1):
        if not isinstance(e, dict):
            continue
        try:
            marked_at = float(e["marked_at"]) if e.get("marked_at") is not None else None
        except (TypeError, ValueError):
            marked_at = now
        if marked_at is not None:
            # A hand-edited out-of-range epoch would OverflowError the date
            # math below and 500 every status poll — read it as "just marked".
            # The probe includes the +delay headroom (a year-9999 epoch passes
            # fromtimestamp but overflows date + timedelta).
            try:
                datetime.fromtimestamp(marked_at).date() + timedelta(days=400)
            except (OverflowError, OSError, ValueError):
                marked_at = now
        size_bytes = _entry_size_bytes(e)
        marked = False
        remaining = delete_on = None
        if rl_only:
            if _marked_bytes < _deficit_bytes:
                _marked_bytes += size_bytes
                marked = True
        elif marked_at is not None:
            # Calendar-day aging, matching the engine: marked date + delay is
            # when the mark becomes deletable at that day's daily run (or any
            # manual Cleanup from then on).
            marked = True
            delete_on = datetime.fromtimestamp(marked_at).date() + timedelta(days=delay)
            remaining = max(0, (delete_on - today).days)
        if not with_lines:
            out.append({
                "marked_at": marked_at,
                "size_bytes": size_bytes,
                "marked": marked,
                "days_remaining": remaining,
                "delete_on": delete_on.isoformat() if delete_on else None,
            })
            continue
        title = str(e.get("title") or Path(path).name)
        size_label = _format_reclaimed_size(size_bytes) if size_bytes else ""
        if rl_only:
            when = (f"#{i} — deletes on the next Cleanup (Redline breached)" if marked
                    else f"#{i} — deletes when Redline hits")
        elif marked_at is not None:
            when = ("deletable now" if remaining <= 0
                    else f"deletable from {delete_on.isoformat()}")
        else:
            when = f"#{i} — next in line if more space is needed"
        marked_disp = "" if marked_at is None else _format_log_timestamp_for_display(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(marked_at)), _fmt)
        line_parts = [p for p in (marked_disp, title) if p]
        if size_label:
            line_parts.append(size_label)
        if e.get("score") is not None:
            line_parts.append(f"score {e.get('score')}")
        line_parts.append(when)
        line_parts.append(path)
        out.append({
            "time": marked_disp,
            "marked_at": marked_at,
            "title": title,
            "path": path,
            "size_bytes": size_bytes,
            "size": size_label,
            "score": e.get("score"),
            "when": when,
            "marked": marked,
            "days_remaining": remaining,
            "delete_on": delete_on.isoformat() if delete_on else None,
            "line": " | ".join(line_parts),
        })
    # Normal (delay) modes: float the marked entries to the top, SOONEST deletion
    # first — with the incremental re-verify a backfilled mark starts a fresh,
    # longer countdown, so it sinks below the aging original marks and the list
    # reads "what's about to go" downward. Eligible entries keep the deletion
    # order behind them (stable sort). Redline-only has no delay clock, so its
    # deficit-order display is left untouched. Only the displayed list is
    # reordered; the /api/status forecast (with_lines=False) never sorts.
    if with_lines and not rl_only:
        out.sort(key=lambda r: (0 if r.get("marked") else 1,
                                r["days_remaining"] if (r.get("marked")
                                and r.get("days_remaining") is not None) else 0))
    return out


def _delete_delay_days(cfg: dict | None = None) -> int:
    try:
        # Defensive floor of 1: a marked movie is never deleted the same day. Valid
        # config is always >= 1 (the file guard rejects less); max() covers the rest.
        return max(1, int(float((cfg or load_config()).get("DELETE_DELAY_DAYS", 1) or 1)))
    except (TypeError, ValueError):
        return 1


def _daily_run_time(cfg: dict | None = None) -> str:
    try:
        return _validate_daily_run_time((cfg or load_config()).get("DAILY_RUN_TIME"))
    except ValueError:
        return "00:00"


def _run_time_moved_into_past(old_time: str, new_time: str, now_hhmm: str) -> bool:
    """True when a save moves the daily run time to a slot already behind today's
    clock. The engine would otherwise fire an immediate catch-up run (the window
    is unused and now >= the new time), so today's window is burned and the new
    time takes effect tomorrow. An unchanged time, or one still ahead today (which
    simply fires later), returns False — HH:MM strings compare correctly within a
    day."""
    return new_time != old_time and now_hhmm > new_time


def _run_time_moved_ahead_today(old_time: str, new_time: str, now_hhmm: str) -> bool:
    """True when a save moves the daily run time to a slot still ahead of the clock
    today. If today's run already fired (its window is burned), it is reopened so
    the new, later time can run again today — safe, since a same-day re-run only
    re-marks. Unchanged times, and times already behind the clock, return False."""
    return new_time != old_time and now_hhmm < new_time


def pending_delete_forecast(cfg: dict | None = None) -> dict:
    """For the dashboard's breach note and red countdown: queue size, marks ripe now,
    and the next deletion EVENT — the exact batch the next deleting daily run removes
    (ripe marks, or failing any, the earliest upcoming eligibility date's batch) with
    its true movie count and bytes. Pass an already-loaded cfg when you have one."""
    cfg = cfg or load_config()
    entries = pending_deletion_entries(cfg, with_lines=False)
    # Redline-only mode schedules nothing: the queue is the deletion-order preview
    # and only a Redline breach deletes, so there is no ripe set, no next event,
    # and no delay to age against.
    if _redline_only_mode_cfg(cfg):
        return {"count": len(entries), "ripe": 0, "event_on": None,
                "event_count": 0, "event_bytes": 0, "waiting_ages": []}
    # Only CLOCKED entries are scheduled; the eligible remainder of the queue
    # (days_remaining None) has no dates and never enters the forecast.
    clocked = [e for e in entries if e["days_remaining"] is not None]
    ripe = [e for e in clocked if e["days_remaining"] <= 0]
    if ripe:
        event_on, batch = None, ripe   # deletable at the next daily run
    else:
        event_on = min((e["delete_on"] for e in clocked), default=None)
        batch = [e for e in clocked if e["delete_on"] == event_on] if event_on else []
    # Calendar-day age of each mark that is still WAITING (not yet ripe under the
    # current delay). Lowering the delay to N makes any waiting mark whose age is
    # >= N deletable at the next cleanup, so the Config page uses these to warn
    # before a save that would delete more than the current delay would.
    today = datetime.now().date()
    waiting_ages = sorted(
        (today - datetime.fromtimestamp(e["marked_at"]).date()).days
        for e in clocked if e["days_remaining"] > 0
    )
    return {
        "count": len(entries),
        "ripe": len(ripe),
        "event_on": event_on,
        "event_count": len(batch),
        "event_bytes": int(sum(e.get("size_bytes") or 0 for e in batch)),
        "waiting_ages": waiting_ages,
    }

# ── Routes — pages ────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    cfg  = load_config()
    monitoring = _has_monitored_dirs(cfg)
    disk = disk_stats()
    cleanup_state = _cleanup_button_state(cfg, disk)
    deleted = deleted_stats()
    stats = library_stats()
    return render_template("dashboard.html",
                           run_mode=cfg.get("RUN_MODE", "paused"),
                           autopause_reason=(str(cfg.get("_RUN_MODE_AUTOPAUSE_REASON") or "")
                                             if cfg.get("RUN_MODE") == "paused" else ""),
                           thresholds_configured=_has_monitored_dirs(cfg),
                           headroom_gb=cfg.get("HEADROOM_GB"),
                           redline_gb=cfg.get("REDLINE_GB"),
                           redline_only=_redline_only_mode_cfg(cfg),
                           max_library_gb=cfg.get("MAX_LIBRARY_GB"),
                           headroom_window_used_today=_headroom_window_used_today(),
                           disk=(disk if monitoring else None),
                           library_gb=(stats.get("library_gb") if monitoring else None),
                           cleanup_state=cleanup_state,
                           run_active=_run_active,
                           last_run=last_run_time(),
                           last_run_ts=last_run_epoch(),
                           deleted_count=deleted["count"],
                           deleted_reclaimed_bytes=deleted["reclaimed_bytes"],
                           deleted_reclaimed_label=deleted["reclaimed_label"],
                           marked_count=pending_count(),
                           marked_imminent=_marked_imminent_count(cfg),
                           delete_forecast=pending_delete_forecast(cfg),
                           delete_delay_days=_delete_delay_days(cfg),
                           daily_run_time=_daily_run_time(cfg),
                           library_root=FILESYSTEM_CHECK_PATH)

@app.route("/config")
def config_page():
    with open(DEFAULT_CFG_PATH) as _f:
        defaults = json.load(_f)
    cfg = load_config()
    connection_onboarding_active = _connection_onboarding_needed(cfg)
    if connection_onboarding_active:
        cfg = _mark_connection_onboarding_seen(cfg)
    disk = disk_stats()
    url_defaults = _connection_url_defaults(cfg)
    url_placeholders = {k: (url_defaults.get(k) or _GENERIC_URL_PLACEHOLDERS[k])
                        for k in _GENERIC_URL_PLACEHOLDERS}
    return render_template(
        "config.html",
        config=cfg,
        defaults=defaults,
        disk=disk,
        space_thresholds=_space_threshold_state(cfg, disk),
        connection_health=_connection_health_for_ui(cfg),
        connection_onboarding_active=connection_onboarding_active,
        connection_onboarding_needed=False,
        connection_url_defaults=url_defaults,
        connection_url_placeholders=url_placeholders,
        run_active=_run_active,
        summary_active=_summary_active,
        time_zone_options=_time_zone_options(),
        library_root=FILESYSTEM_CHECK_PATH,
    )

@app.route("/explorer")
def explorer():
    # Only the keys the page uses — never the full config (it holds tokens/keys that
    # must not appear in page source).
    cfg = load_config()
    page_cfg = _score_page_config(cfg)
    health = _connection_health_for_ui(cfg)
    jellyfin_connected = bool(cfg.get("USE_JELLYFIN")) and bool(health.get("jellyfin_connected"))
    return render_template("deletion_score_explorer.html", config=page_cfg,
                           jellyfin_connected=jellyfin_connected, scoring=SCORING,
                           run_active=_run_active)


def _score_page_config(cfg: dict) -> dict:
    """Only the keys the Score Explorer uses — never the full config (it holds
    tokens/keys that must not reach the page or the score-config response)."""
    return {
        "SCORE_BALANCE": cfg.get("SCORE_BALANCE", 50),
        "MAX_IMDB_RATING": cfg.get("MAX_IMDB_RATING"),
        "SKIP_UNPLAYED_MOVIES": bool(cfg.get("SKIP_UNPLAYED_MOVIES")),
        "GRACE_PERIOD_DAYS": cfg.get("GRACE_PERIOD_DAYS", 0),
        "PROTECT_JELLYFIN_FAVORITES": bool(cfg.get("PROTECT_JELLYFIN_FAVORITES")),
        "NEAR_TIE_PTS": cfg.get("NEAR_TIE_PTS", 2),
        "MAX_STALENESS_MONTHS": cfg.get("MAX_STALENESS_MONTHS", 36),
        "USE_JELLYFIN": bool(cfg.get("USE_JELLYFIN")),
        # Last entered values of the optional fields, kept while disabled so the
        # greyed-out inputs still show them (surviving restarts).
        "_MAX_IMDB_RATING_LAST": cfg.get("_MAX_IMDB_RATING_LAST"),
        "_NEAR_TIE_PTS_LAST": cfg.get("_NEAR_TIE_PTS_LAST"),
    }

# ── Routes — API ──────────────────────────────────────────────────────────────

@app.route("/api/library-snapshot")
def api_library_snapshot():
    """The full-library snapshot from the last completed scan (the store's
    "library_snapshot"): every monitored-path movie with merged Plex+Jellyfin
    data. Built by Simulate and full-scan Cleanups; the Filtering & Scoring
    page paginates and scores it client-side."""
    data, pool_err = _read_library_snapshot()
    if pool_err == "missing":
        resp = jsonify({"ok": False, "reason": "no_pool",
                        "message": "No library snapshot yet — run a Simulate to build it."})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    movies = (data or {}).get("movies") or []
    if pool_err or not isinstance(movies, list):
        resp = jsonify({"ok": False, "reason": "bad_pool",
                        "message": "The library snapshot is unreadable — run a Simulate to rebuild it."})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    if not movies:
        resp = jsonify({"ok": False, "reason": "empty_pool",
                        "message": "The last scan found no movies under the monitored library paths."})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    # Whether an IMDb dataset is on disk — when the snapshot has no ratings,
    # the explorer uses this to explain whether the next run will annotate.
    tsv = imdb_ratings_path()
    imdb_on_disk = tsv.exists() or tsv.with_name(tsv.name + ".gz").exists()
    # The snapshot carries each movie's /library path as an internal join key for
    # the engine's incremental re-verify. Paths never leave the server (the debug
    # report redacts them too), so strip it from the browser payload.
    movies = [{k: v for k, v in m.items() if k != "path"} if isinstance(m, dict) else m
              for m in movies]
    resp = jsonify({"ok": True, "built_at": data.get("built_at"), "movies": movies,
                    "imdb_dataset_on_disk": imdb_on_disk})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/status")
def api_status():
    cfg = load_config()
    # With nothing monitored the scheduler is truly idle (see _scheduled_tick), so
    # there are no meaningful filesystem/library numbers or a next tick to show —
    # the dashboard dashes the storage card and reads "Scheduler paused".
    monitoring = _has_monitored_dirs(cfg)
    stats = library_stats()
    disk = cached_disk_stats(stats)
    cleanup_state = _cleanup_button_state(cfg, disk)
    job = scheduler.get_job("engine")
    next_run = None
    mode = cfg.get("RUN_MODE")
    if (
        job and job.next_run_time and _is_cleanup_mode(mode) and not _run_active
        and cleanup_state.get("connection_health", {}).get("critical_ok", True)
        and cleanup_state["space_thresholds"].get("ok_for_cleanup", False)
    ):
        next_run = job.next_run_time.isoformat()
    # The raw next scheduler tick, ANY mode (unlike next_run_time, which is gated to
    # a run that will actually delete). Monitor Only surfaces this as its ~15-minute
    # library-check / queue-refresh countdown; None while a run holds the clock or
    # nothing is monitored (the tick then no-ops — see _scheduled_tick).
    next_tick = (job.next_run_time.isoformat()
                 if job and job.next_run_time and not _run_active and monitoring else None)
    deleted = deleted_stats()
    _forecast = pending_delete_forecast(cfg)
    _delay_days = _delete_delay_days(cfg)
    resp = jsonify({
        "run_active":              _run_active,
        "run_cleanup":                _run_active and _run_cleanup,
        # A Debug Cleanup drives the cleanup path but deletes nothing — the dashboard
        # header badge and run pill read "Debugging" in yellow for it.
        "run_debug_cleanup":          _run_active and _run_debug_cleanup,
        "summary_active":          _summary_active,
        "last_run":                last_run_time(),
        "last_run_ts":             last_run_epoch(),
        "deleted_count":           deleted["count"],
        "deleted_reclaimed_bytes": deleted["reclaimed_bytes"],
        "deleted_reclaimed_label": deleted["reclaimed_label"],
        "marked_count":            _forecast["count"],
        # Redline-only: how many queue-front entries cover the current deficit
        # — the "marked" half of the dashboard's Marked & Queued button.
        "marked_imminent_count":   _marked_imminent_count(cfg),
        "marked_ripe_count":       _forecast["ripe"],
        # Ages (calendar days) of marks still waiting out the delay — the Config
        # page warns before a save that lowers the delay enough to delete them.
        "marked_waiting_ages":     _forecast["waiting_ages"],
        "marked_event_on":         _forecast["event_on"],
        "marked_event_count":      _forecast["event_count"],
        "marked_event_bytes":      _forecast["event_bytes"],
        "delete_delay_days":       _delay_days,
        # Redline-only mode (Headroom disabled): the marked queue is a standing
        # deletion-order preview, not a schedule — drives UI wording.
        "redline_only":            _redline_only_mode_cfg(cfg),
        # Time of day (24h HH:MM, operating zone) the daily cleanup may fire; drives the
        # dashboard breach-note wording and red-countdown gating.
        "daily_run_time":          _daily_run_time(cfg),
        "next_run_time":           next_run,
        "next_tick_time":          next_tick,
        # Why automatic Live is paused, when the app (not the user) paused it — startup
        # safety, forced pause on save, etc.
        "run_mode_autopause_reason": (str(cfg.get("_RUN_MODE_AUTOPAUSE_REASON") or "")
                                      if cfg.get("RUN_MODE") == "paused" else ""),
        # Dashed out when nothing is monitored — no paths means no filesystem/media
        # size worth showing (the storage card and bar go to "—").
        "disk":                    (disk if monitoring else None),
        "library_gb":              (stats.get("library_gb") if monitoring else None),
        # Storage-bar threshold markers: Headroom/Redline are free-space targets;
        # the library cap is a library-size target, drawn against the library.
        "headroom_gb":             _threshold_gb_or_none(cfg.get("HEADROOM_GB")),
        "redline_gb":              _threshold_gb_or_none(cfg.get("REDLINE_GB")),
        "library_cap_gb":          _threshold_gb_or_none(cfg.get("MAX_LIBRARY_GB")),
        # Monitored dirs exist — the dashboard's Cleanup Targets card renders
        # real values ("Off"/"Disabled" included) instead of "Not set".
        "thresholds_configured":   _has_monitored_dirs(cfg),
        # Once-per-day headroom window: a headroom-only breach won't prune again today
        # once it's used (redline/cap ignore it); the dashboard red countdown keys off
        # this.
        "headroom_window_used_today": _headroom_window_used_today(),
        "cleanup_state":              cleanup_state,
        # Drives the red Configuration tab live (no reload): onboarding cue or an API
        # error in the saved config's cached health.
        "config_attention":        _connection_onboarding_needed(cfg) or _api_connection_error(cfg),
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/welcome/seen", methods=["POST"])
def api_welcome_seen():
    """Persist that the first-run welcome/quick-start popup was dismissed."""
    try:
        return jsonify({"ok": _mark_welcome_guide_seen()})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@app.route("/api/config/check", methods=["POST", "GET"])
def api_config_check():
    """Run a fast configuration health check for the Config page."""
    cfg = load_config()
    if request.method == "POST":
        posted = request.get_json(silent=True)
        if isinstance(posted, dict):
            cfg.update(posted)
            _normalize_library_paths(cfg)
            cfg["CHECK_PATH"] = FILESYSTEM_CHECK_PATH
            cfg["TAUTULLI_APPDATA"] = TAUTULLI_APPDATA_DIR
            cfg["RADARR_APPDATA"] = RADARR_APPDATA_DIR
            # The form posts 'auto' for an enabled Radarr cleanup; a save normalizes
            # that to the cached detection. Mirror it here so a clean form's check
            # carries the SAVED config's signature — otherwise every manual check looks
            # like an unsaved-values probe and stops driving the red Configuration tab.
            if cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None:
                cached_detected_section = str(cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip()
                cfg["RADARR_OVERSEERR_SECTION_ID"] = cached_detected_section or "auto"
            # Match save semantics: scheme-normalize the posted URLs. Blank
            # URLs resolve to their defaults inside the health check itself.
            _normalize_saved_service_urls(cfg)
    health = _connection_health_state(cfg, probe=True)
    # Cache both manual and automatic checks so highlighted override fields stay stable
    # if the user leaves Config and returns, instead of falling back to the non-probed
    # startup state. Keyed by the connection-related signature, so stale form values
    # aren't reused after the saved config changes.
    with _connection_health_cache_lock:
        _connection_health_cache.update({
            "signature": _connection_health_signature(cfg),
            "health": health,
            "checked_at": time.time(),
        })
    return jsonify(health)


@app.route("/api/connections/verify")
def api_verify_connections():
    """Check whether each service's appdata is mounted at the expected path."""
    mounts = _appdata_mount_state()
    return jsonify({
        "tautulli":  bool(mounts["tautulli"].get("ok")),
        "radarr":    bool(mounts["radarr"].get("ok")),
        "jellyfin":  bool(mounts["jellyfin"].get("ok")),
    })


@app.route("/api/connections/autodetect", methods=["POST", "GET"])
def api_connections_autodetect():
    """One-shot appdata auto-detect used only by the Config Auto Detect button."""
    values = _autodetected_connection_field_values()
    found = {k: bool(str(v or "").strip()) for k, v in values.items()}
    return jsonify({
        "ok": any(found.values()),
        "values": values,
        "found": found,
        "appdata": _appdata_mount_state(),
    })


@app.route("/api/library/browse")
def api_library_browse():
    """List folders under the library root for the Movie Library Paths browser."""
    root = FILESYSTEM_CHECK_PATH
    root_name = root.rsplit("/", 1)[-1] or "library"
    base = Path(root).resolve(strict=False)
    raw = (request.args.get("path") or root).strip().replace("\\", "/")

    if raw in ("", root, root_name):
        target = base
    elif raw.startswith(root + "/"):
        target = Path(raw).resolve(strict=False)
    else:
        target = (base / raw.lstrip("/")).resolve(strict=False)

    try:
        target.relative_to(base)
    except ValueError:
        return jsonify({"ok": False, "error": f"Path must be inside {root}."}), 400

    if not target.exists() or not target.is_dir():
        return jsonify({"ok": False, "error": f"Folder not found: {target}"}), 404

    dirs = []
    try:
        for child in target.iterdir():
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child.resolve(strict=False))})
            except OSError:
                continue
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    dirs.sort(key=lambda item: item["name"].lower())
    parent = target.parent if target != base else base
    try:
        parent.relative_to(base)
    except ValueError:
        parent = base

    return jsonify({
        "ok": True,
        "path": str(target),
        "parent": str(parent),
        "dirs": dirs,
    })

@app.route("/api/library/validate")
def api_library_validate():
    """Validate that a monitored library folder exists under the library root."""
    root = FILESYSTEM_CHECK_PATH
    base = Path(root).resolve(strict=False)
    raw = (request.args.get("path") or "").strip().replace("\\", "/")
    normalized = _normalize_library_path(raw)
    if not normalized:
        return jsonify({"ok": False, "error": f"Enter a folder under {root}."}), 400

    target = Path(normalized).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError:
        return jsonify({"ok": False, "error": f"Path must be inside {root}."}), 400

    if not target.exists() or not target.is_dir():
        return jsonify({"ok": False, "error": f"Folder not found: {target}"}), 404

    return jsonify({"ok": True, "path": str(target)})


@app.route("/api/score-config", methods=["POST"])
def api_save_score_config():
    """Save only the scoring/filter fields edited from the Score Explorer."""
    if _run_active:
        # Same rule as the Configuration save: the engine loaded its scoring at run
        # start, so a mid-run save would silently apply to the NEXT run while the UI
        # implies it changed this one.
        return jsonify({"ok": False, "error": "A run is active. Try again when it finishes."}), 409
    blocked = _invalid_config_response()
    if blocked:
        return blocked
    try:
        payload = request.get_json(force=True) or {}

        # Error strings surface as explorer toasts — use the page's labels, not raw
        # config keys ("SCORE_BALANCE must be a number.").
        _FIELD_LABELS = {
            "SCORE_BALANCE": "The scoring balance",
            "GRACE_PERIOD_DAYS": "Minimum age (grace period)",
            "MAX_IMDB_RATING": "Maximum IMDb rating",
            "NEAR_TIE_PTS": "The file-size-optimization window",
            "MAX_STALENESS_MONTHS": "Max staleness",
        }

        def _float_field(name, minimum=None, maximum=None):
            label = _FIELD_LABELS.get(name, name)
            try:
                val = float(payload.get(name))
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a number.")
            if minimum is not None and val < minimum:
                raise ValueError(f"{label} must be at least {minimum}.")
            if maximum is not None and val > maximum:
                raise ValueError(f"{label} must be at most {maximum}.")
            return val

        updates = {
            "SCORE_BALANCE": max(0, min(100, round(_float_field("SCORE_BALANCE", 0, 100)))),
        }

        # The Filtering & Scoring page edits every scoring/filter field, so
        # any of them may arrive here.
        if "GRACE_PERIOD_DAYS" in payload:
            try:
                grace = int(float(payload.get("GRACE_PERIOD_DAYS")))
            except (TypeError, ValueError):
                raise ValueError("Minimum age (grace period) must be a whole number of days.")
            if grace < 0:
                raise ValueError("Minimum age (grace period) must be zero or greater.")
            updates["GRACE_PERIOD_DAYS"] = grace

        if "MAX_IMDB_RATING" in payload:
            raw_cutoff = payload.get("MAX_IMDB_RATING")
            if not _is_blank(raw_cutoff):
                try:
                    cutoff_val = float(raw_cutoff)
                except (TypeError, ValueError):
                    raise ValueError("Maximum IMDb rating must be a number above 0, up to 10, or disabled.")
                if cutoff_val <= 0:
                    # 0 would match nothing — unchecking is the off switch.
                    raise ValueError("Maximum IMDb rating must be above 0 — uncheck it instead.")
            updates["MAX_IMDB_RATING"] = _clamp_max_imdb_rating(raw_cutoff)

        if "NEAR_TIE_PTS" in payload:
            raw_tie = payload.get("NEAR_TIE_PTS")
            if not _is_blank(raw_tie):
                try:
                    tie_val = float(raw_tie)
                except (TypeError, ValueError):
                    raise ValueError("The file-size-optimization window must be a number of points, or disabled.")
                if tie_val < 0.5:
                    raise ValueError("The file-size-optimization window must be at least 0.5 points — uncheck it instead.")
            updates["NEAR_TIE_PTS"] = _clamp_near_tie_pts(raw_tie)

        if "MAX_STALENESS_MONTHS" in payload:
            try:
                stale_val = float(payload.get("MAX_STALENESS_MONTHS"))
            except (TypeError, ValueError):
                raise ValueError("Max staleness must be a number of months (1–120).")
            if not 1 <= stale_val <= 120:
                raise ValueError("Max staleness must be 1–120 months.")
            updates["MAX_STALENESS_MONTHS"] = _clamp_staleness_months(stale_val)

        cfg = load_config()
        _old_cfg = dict(cfg)   # pre-save values, for the reconcile's changed-key check
        if "SKIP_UNPLAYED_MOVIES" in payload:
            updates["SKIP_UNPLAYED_MOVIES"] = _coerce_bool(payload.get("SKIP_UNPLAYED_MOVIES"))
        if "PROTECT_JELLYFIN_FAVORITES" in payload:
            updates["PROTECT_JELLYFIN_FAVORITES"] = _coerce_bool(payload.get("PROTECT_JELLYFIN_FAVORITES"))
        # Disabled optional fields keep their last entered value so the greyed-out field
        # still shows it (surviving restarts). Prefer the text still in the disabled
        # input (posted as _<key>_LAST), then the value this save is disabling, then the
        # memory on disk. Saving the field enabled clears its memory.
        for _opt_key, _opt_clamp in (("MAX_IMDB_RATING", _clamp_max_imdb_rating),
                                     ("NEAR_TIE_PTS", _clamp_near_tie_pts)):
            if _opt_key not in updates:
                continue
            _last_key = f"_{_opt_key}_LAST"
            if updates[_opt_key] is None:
                _last = next((v for v in (_opt_clamp(payload.get(_last_key)),
                                          _opt_clamp(cfg.get(_opt_key)),
                                          _opt_clamp(cfg.get(_last_key)))
                              if v is not None), None)
                if _last is not None:
                    updates[_last_key] = _last
                else:
                    cfg.pop(_last_key, None)
            else:
                cfg.pop(_last_key, None)
        cfg.update(updates)
        if not save_config(cfg):
            return _invalid_config_response() or (jsonify({
                "ok": False, "error": "Save was refused — config.json changed on disk. Reload the page.",
            }), 409)
        # Rebuild the marked/eligible queue in place from the snapshot under the new
        # scoring/filter settings — no Simulate needed (favorites re-fetch from
        # Jellyfin; everything else is a pure recompute).
        reconcile = _reconcile_after_save(_old_cfg, cfg, source="filtering & scoring")
        return jsonify({"ok": True, "config": _score_page_config(cfg), "reconcile": reconcile})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config/reset", methods=["POST"])
def api_reset_config():
    """Wipe configuration and operational state back to first-time setup.

    Removes the state files the app creates: config.json, the engine's cache (which
    includes the library snapshot), progress state, and saved debug reports.
    LOGS ARE NEVER LOST — deleted.log and the archived logs/ folder survive, and
    lastrun.log is moved into logs/ so the Dashboard run panel starts empty while the
    run stays archived. The IMDb ratings dataset is kept so a reset never forces a
    re-download. Uses an explicit allowlist, never a blanket directory wipe. An active
    run is stopped first."""
    if _run_active:
        stop_script()
        # Give the worker a moment to tear down the subprocess and clear the
        # active flag before we delete its config out from under it.
        for _ in range(50):  # up to ~5s
            if not _run_active:
                break
            time.sleep(0.1)
    # A storage summary in flight would recreate the store with pre-reset stats
    # moments after this wipe (its engine subprocess merges into the cache on
    # exit), resurrecting stale numbers on the "first-time" dashboard — and the
    # summary worker's clock restart would resume the schedule the reset just
    # paused. Wait briefly; if one is stuck, tell the user instead of half-resetting.
    for _ in range(100):  # up to ~10s
        if not _summary_active:
            break
        time.sleep(0.1)
    if _summary_active:
        return jsonify({
            "ok": False,
            "error": "A background storage refresh is still running — "
                     "try the reset again in a moment.",
        }), 409

    out_dir = output_dir()
    files = [
        CONFIG_PATH,
        out_dir / "progress.json",
        out_dir / "progress.json.tmp",
    ]
    try:
        files.extend(out_dir.glob("progress.json.*.tmp"))  # pid-unique writer tmps
        files.extend(out_dir.glob("debug_report_*.txt"))
    except OSError:
        pass
    errors = []
    for p in files:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            errors.append(f"{p.name}: {e}")
    db.reset_store(out_dir / "mediareducer.db")  # SQLite store + sidecars, under the init lock

    # lastrun.log drives the Last Run timestamp, the detailed-log window, and the
    # run-stat jump targets — after a reset those must read as "no runs yet". It is
    # archived into logs/ (never deleted). out_dir was captured above, BEFORE config.json
    # was deleted; the helper takes it explicitly so it can't re-resolve to the default.
    _archive_lastrun_log(out_dir)

    # Return to the first-time (paused) default and drop stale in-memory caches.
    try:
        scheduler.pause_job("engine")
    except Exception:
        pass
    with _connection_health_cache_lock:
        _connection_health_cache.update({"signature": None, "health": None, "checked_at": None})
    # Re-probe the post-reset defaults now: the red Configuration tab reads cached
    # health, and an empty cache would let the first visit clear the onboarding cue even
    # though nothing is configured yet.
    threading.Thread(
        target=lambda: _refresh_connection_health_cache(load_config(), probe=True),
        daemon=True, name="engine-postreset-health",
    ).start()

    if errors:
        return jsonify({"ok": False, "message": "Reset completed with problems: " + "; ".join(errors)}), 500
    return jsonify({"ok": True, "message": "Configuration reset."})


@app.route("/api/config/reset-invalid", methods=["POST"])
def api_reset_invalid_config():
    """Reset ONLY the hand-edited invalid values in config.json to their defaults,
    leaving valid settings untouched; an unparseable file is replaced wholesale. The
    targeted way out of the invalid-config lockout (Reset MediaReducer is the full one)."""
    with _config_io_lock:
        defaults = _CONFIG_DEFAULTS
        saved = _read_saved_config_file()
        if saved is None:
            fixed = ["config.json"]
            save_config(defaults, overwrite_invalid=True)
        else:
            # Defaulting a flagged key can surface a NEW issue (the REDLINE_GB default
            # may exceed a valid smaller HEADROOM_GB), so iterate to a fixed point; a key
            # flagged while already at its default drags its cross-check partner along.
            # Every key the validator can flag exists in default_config.json.
            partners = {"REDLINE_GB": ("HEADROOM_GB",)}
            fixed = []
            for _ in range(3):
                issues = _config_file_issues(saved)
                if not issues:
                    break
                for issue in issues:
                    key = issue["key"]
                    if saved.get(key) == defaults.get(key):
                        for partner in partners.get(key, ()):
                            saved[partner] = defaults[partner]
                            fixed.append(partner)
                    saved[key] = defaults[key]
                    fixed.append(key)
            if fixed:
                fixed = list(dict.fromkeys(fixed))
                save_config(saved, overwrite_invalid=True)
    cfg = load_config()  # refreshes _CONFIG_FILE_ISSUES
    # The startup safeties were skipped while the file was invalid; re-apply them now
    # that it is writable, so a hand-edited live mode or an armed undersized cap never
    # rides through the reset.
    if not _CONFIG_FILE_ISSUES:
        force_paused_run_mode_on_startup()
        disable_undersized_library_cap_on_startup()
        burn_daily_window_on_startup(reason="invalid-config reset")
        cfg = load_config()
    health = _refresh_connection_health_cache(cfg, probe=True)
    residual = _CONFIG_FILE_ISSUES
    return jsonify({
        "ok": not residual,
        "invalid_config": residual,
        "error": ("Some values could not be reset: "
                  + "; ".join(f"{i['key']} {i['message']}" for i in residual)) if residual else "",
        "connection_health": health,
    })

@app.route("/api/config", methods=["POST"])
def api_save_config():
    try:
        if _run_active:
            return jsonify({"ok": False, "error": "A run is active. Try again when it finishes."}), 409
        blocked = _invalid_config_response()
        if blocked:
            return blocked
        cfg = request.get_json(force=True)
        if cfg is None:
            return jsonify({"ok": False, "error": "Invalid JSON"}), 400

        saved_cfg = load_config()
        cfg = _preserve_connection_onboarding_flags(cfg, saved_cfg)
        # Filtering & Scoring lives on its own page (/explorer, saved via
        # /api/score-config). The Config form doesn't send those fields, so carry the
        # saved values through untouched. Remember WHICH keys were carried: they are
        # re-read from disk right before the final write, because this handler runs
        # multi-second probes and a /api/score-config save landing mid-probe must not be
        # reverted by this save's stale snapshot.
        _score_fields = ("GRACE_PERIOD_DAYS", "MAX_IMDB_RATING", "SCORE_BALANCE",
                         "SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES",
                         "NEAR_TIE_PTS", "MAX_STALENESS_MONTHS",
                         "_MAX_IMDB_RATING_LAST", "_NEAR_TIE_PTS_LAST")
        _carried_score_fields = [k for k in _score_fields if k not in cfg]
        for key in _carried_score_fields:
            if key in saved_cfg:
                cfg[key] = saved_cfg[key]
        # Disabled optional thresholds keep their last entered value so the greyed-out
        # field still shows it (surviving restarts). The form posts _REDLINE_GB_LAST /
        # _MAX_LIBRARY_GB_LAST with the disabled field's text; fall back to the value
        # being disabled, then the memory on disk (also written by the startup
        # undersized-cap reset). Saving the field enabled clears its memory. Underscore
        # keys bypass the file validator, so only a positive number is ever kept.
        for _opt_key in ("REDLINE_GB", "MAX_LIBRARY_GB"):
            _last_key = f"_{_opt_key}_LAST"
            if cfg.get(_opt_key) is None:
                _last = next((n for n in (_config_num(cfg.get(_last_key)),
                                          _config_num(saved_cfg.get(_opt_key)),
                                          _config_num(saved_cfg.get(_last_key)))
                              if n is not None and n > 0), None)
                if _last is not None:
                    cfg[_last_key] = int(_last) if float(_last).is_integer() else _last
                else:
                    cfg.pop(_last_key, None)
            else:
                cfg.pop(_last_key, None)
        # Headroom's value memory works the same way, with 0 (its disable toggle,
        # redline-only mode) as the off spelling instead of null.
        _hr_last_key = "_HEADROOM_GB_LAST"
        if _config_num(cfg.get("HEADROOM_GB")) == 0:
            _hr_last = next((n for n in (_config_num(cfg.get(_hr_last_key)),
                                         _config_num(saved_cfg.get("HEADROOM_GB")),
                                         _config_num(saved_cfg.get(_hr_last_key)))
                             if n is not None and n > 0), None)
            if _hr_last is not None:
                cfg[_hr_last_key] = int(_hr_last) if float(_hr_last).is_integer() else _hr_last
            else:
                cfg.pop(_hr_last_key, None)
        else:
            cfg.pop(_hr_last_key, None)
        # Compare signatures on a copy normalized EXACTLY like the save below (and like
        # api_config_check) — the form posts RADARR_OVERSEERR_SECTION_ID='auto' and raw
        # URL text, while the saved file holds the concrete detected section id and
        # scheme-normalized URLs. Signing the raw body made every save with Radarr
        # cleanup enabled read as "connection settings changed": Live force-paused for
        # nothing and Live force-paused on every save.
        _sig_cfg = dict(cfg)
        if _sig_cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None:
            _sig_cached = str(_sig_cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip()
            _sig_cfg["RADARR_OVERSEERR_SECTION_ID"] = _sig_cached or "auto"
        _normalize_saved_service_urls(_sig_cfg)
        api_config_changed = _api_config_signature(_sig_cfg) != _api_config_signature(saved_cfg)
        radarr_section_credentials_changed = (
            _radarr_section_detection_signature(cfg)
            != _radarr_section_detection_signature(saved_cfg)
        )
        saved_was_cleanup = _is_cleanup_mode(saved_cfg.get("RUN_MODE"))
        forced_pause_for_api_change = False
        forced_pause_for_tautulli = False
        forced_pause_for_threshold_change = False
        save_health = None
        radarr_section_detection = None
        radarr_section_cache_incomplete = _radarr_section_detection_cache_incomplete(cfg)
        if radarr_section_credentials_changed:
            # The cached section was detected with different Radarr/Plex values. Clear
            # it; a successful one-shot detection below repopulates it for the UI and
            # future cleanup enables.
            _clear_radarr_section_detection_cache(cfg)

        # Two overlapping key lists serve two different rules — keep them apart:
        #   • _PLAN_CONFIG_KEYS (plan staleness): everything that changes WHAT a
        #     run would delete; any mismatch with the Simulate stamp ghosts Live.
        #   • _threshold_snapshot (this): just the space targets + delay; a save
        #     that changes one of THESE while limits are satisfied unschedules
        #     the delay clocks. DELETE_DELAY_DAYS lives here deliberately and is
        #     NOT a plan key — changing the delay re-paces marks, it doesn't
        #     change which movies are in the plan.
        def _threshold_snapshot(config_obj: dict) -> dict:
            def _number_or_none(value):
                if value is None or value == "":
                    return None
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return value

            return {
                "HEADROOM_GB": _number_or_none(config_obj.get("HEADROOM_GB")),
                "REDLINE_GB": _number_or_none(config_obj.get("REDLINE_GB")),
                "REDLINE_ONLY_MODE": bool(config_obj.get("REDLINE_ONLY_MODE")),
                "MAX_LIBRARY_GB": _number_or_none(config_obj.get("MAX_LIBRARY_GB")),
                "DELETE_DELAY_DAYS": _number_or_none(config_obj.get("DELETE_DELAY_DAYS", 1)),
            }

        # The form posts a number while Headroom is enabled and 0 when its toggle is
        # off — blank means the enabled field was left empty.
        if _is_blank(cfg.get("HEADROOM_GB")):
            return jsonify({"ok": False, "error": "Enter a Headroom target in GB "
                                                  "(0 = trigger off)."}), 400

        # Changing thresholds while automatic mode is armed is allowed — the
        # save force-pauses Live below (same pattern as connection/path
        # changes) so the scheduler never runs on targets no Simulate has
        # previewed. The user reviews with Simulate and re-enables.
        threshold_change_while_cleanup = (
            _is_cleanup_mode(saved_cfg.get("RUN_MODE"))
            and _threshold_snapshot(cfg) != _threshold_snapshot(saved_cfg)
        )

        try:
            headroom_gb = float(cfg.get("HEADROOM_GB"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Headroom must be a number of GB (blank = 0)."}), 400
        if headroom_gb < 0:
            return jsonify({"ok": False, "error": "HEADROOM_GB must be zero or greater."}), 400

        if cfg.get("REDLINE_GB") is not None:
            try:
                redline_gb = float(cfg.get("REDLINE_GB"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Enter a Redline value or disable it."}), 400
            if redline_gb <= 0:
                return jsonify({"ok": False, "error": "REDLINE_GB must be greater than zero, or disabled."}), 400
            # While Headroom is ticked, Redline must sit STRICTLY below its
            # value — a tie would enforce the full target on every check, which
            # is redline-only mode spelled wrong (untick Headroom for that).
            # At 0 any Redline trips this.
            if not _coerce_bool(cfg.get("REDLINE_ONLY_MODE")) and redline_gb >= headroom_gb:
                return jsonify({"ok": False, "error": "Redline must be lower than the Headroom "
                                                      "target — untick Headroom for redline-only "
                                                      "mode instead."}), 400

        # REDLINE_ONLY_MODE = the Headroom checkbox unticked (the headroom trigger is
        # off). Valid as long as a Redline floor and/or a Library Size Cap is armed to
        # drive cleanup, with the headroom value at 0. WITHOUT the flag, HEADROOM_GB 0
        # just means the headroom trigger is off — Redline and/or the cap may still be
        # armed on their own, and 0/null/null is the valid "no thresholds set" default.
        cfg["REDLINE_ONLY_MODE"] = _coerce_bool(cfg.get("REDLINE_ONLY_MODE"))
        if cfg["REDLINE_ONLY_MODE"] and (cfg.get("MONITOR_DIRS") or []):
            if cfg.get("REDLINE_GB") is None and cfg.get("MAX_LIBRARY_GB") is None:
                return jsonify({"ok": False, "error": "With Headroom off, arm a Redline floor "
                                                      "and/or a Library Size Cap — or re-tick "
                                                      "Headroom."}), 400
            if headroom_gb != 0:
                return jsonify({"ok": False, "error": "With Headroom off, its value must be 0 "
                                                      "(the headroom trigger is retired)."}), 400

        try:
            max_headroom_pct = float(cfg.get("MAX_HEADROOM_PCT", 15))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Enter a valid Headroom Safety Percentage."}), 400
        if max_headroom_pct <= 0:
            return jsonify({"ok": False, "error": "MAX_HEADROOM_PCT must be greater than zero."}), 400
        if max_headroom_pct > 100:
            # The percentage caps how much of the disk one run may free; past 100 it
            # neutralizes that guardrail entirely.
            return jsonify({"ok": False, "error": "The Headroom safety percentage cannot exceed 100."}), 400

        if cfg.get("MAX_LIBRARY_GB") is not None:
            try:
                cap_ok = float(cfg.get("MAX_LIBRARY_GB")) > 0
            except (TypeError, ValueError):
                cap_ok = False
            if not cap_ok:
                return jsonify({"ok": False, "error": "Enter a Library Size Cap value or disable it."}), 400

        # Deletion delay: whole days, minimum 1. A marked movie is never deleted
        # the same day it is marked — the earliest is the next day's daily run,
        # so 1 is the floor. Blank = 1.
        raw_delay = cfg.get("DELETE_DELAY_DAYS", 1)
        if _is_blank(raw_delay):
            raw_delay = 1
        try:
            delay_f = float(raw_delay)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Deletion delay must be a whole number of days."}), 400
        if not delay_f.is_integer() or not 1 <= delay_f <= 365:
            return jsonify({"ok": False, "error": "Deletion delay must be a whole number of days from 1 to 365."}), 400
        cfg["DELETE_DELAY_DAYS"] = int(delay_f)

        try:
            grace = int(float(cfg.get("GRACE_PERIOD_DAYS", 30)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Minimum age (grace period) must be a whole number of days."}), 400
        if grace < 0:
            return jsonify({"ok": False, "error": "Minimum age (grace period) must be zero or greater."}), 400
        cfg["GRACE_PERIOD_DAYS"] = grace
        try:
            cfg["SCORE_BALANCE"] = max(0, min(100, round(float(cfg.get("SCORE_BALANCE", 50)))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Score balance must be a number from 0 to 100."}), 400
        raw_cutoff = cfg.get("MAX_IMDB_RATING")
        if not _is_blank(raw_cutoff):
            try:
                if float(raw_cutoff) <= 0:
                    return jsonify({"ok": False, "error": "Maximum IMDb rating must be above 0 — disable it instead."}), 400
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Maximum IMDb rating must be a number above 0, up to 10, or disabled."}), 400
        cfg["MAX_IMDB_RATING"] = _clamp_max_imdb_rating(raw_cutoff)
    
        cfg["PROTECT_JELLYFIN_FAVORITES"] = _coerce_bool(cfg.get("PROTECT_JELLYFIN_FAVORITES"))
        cfg["SKIP_UNPLAYED_MOVIES"] = _coerce_bool(cfg.get("SKIP_UNPLAYED_MOVIES"))

        try:
            cfg["IMDB_RATINGS_URL"] = _validate_imdb_url(cfg.get("IMDB_RATINGS_URL"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        try:
            cfg["TIME_ZONE"] = _validate_time_zone(cfg.get("TIME_ZONE", "auto"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        cfg["DISPLAY_TIME_FORMAT"] = _validate_display_time_format(cfg.get("DISPLAY_TIME_FORMAT", "12h"))
        try:
            cfg["DAILY_RUN_TIME"] = _validate_daily_run_time(cfg.get("DAILY_RUN_TIME"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        if cfg.get("RUN_MODE") not in ("paused", "headroom"):
            return jsonify({"ok": False, "error": "Choose Paused or Automatic Cleanup before saving."}), 400
        cfg["RUN_MODE"] = _ui_run_mode(cfg.get("RUN_MODE"))

        # Optional Radarr cleanup is a plain on/off — a Radarr node maps to exactly one
        # Plex section, so the section ID is always auto-detected. Normalize any value
        # (including hand-edited IDs) to the cached detection, or "auto" for the
        # detection below / the engine to resolve.
        if cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None:
            cached_detected_section = str(cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip()
            cfg["RADARR_OVERSEERR_SECTION_ID"] = cached_detected_section or "auto"

        # Scheme-normalize the URL fields (blank stays blank — defaults apply at
        # connection time) and snapshot the current defaults for consumers without a
        # request context: the engine runs headless, and the address the browser used
        # for this save is the best-known server address.
        _normalize_saved_service_urls(cfg)
        cfg["_SERVICE_URL_DEFAULTS"] = {k: v for k, v in _connection_url_defaults(cfg).items() if v}

        # Every Config save refreshes API health: connections can fail without any field
        # changing, and dependent UI locks (Movie Library Paths, Space Thresholds,
        # protected collections) must reflect the current probe.
        save_health = _refresh_connection_health_cache(cfg, probe=True)

        # A selected server whose API didn't connect on this save (bad credentials,
        # unreachable, blank key) is deselected automatically; the user re-enables it
        # after fixing the connection. Health is recomputed for the deselected state: an
        # unchecked server is off, not an error, so its failure must not keep the UI red.
        server_software_auto_disabled = []
        if bool(cfg.get("USE_PLEX")) and not save_health.get("tautulli_connected"):
            cfg["USE_PLEX"] = False
            server_software_auto_disabled.append("Plex")
        if bool(cfg.get("USE_JELLYFIN")) and not save_health.get("jellyfin_connected"):
            cfg["USE_JELLYFIN"] = False
            server_software_auto_disabled.append("Jellyfin")
        if server_software_auto_disabled:
            save_health = _refresh_connection_health_cache(cfg, probe=True)
        # A save is never rejected over connection state — it lands, dependent fields
        # re-lock from the fresh probe, and Live is just never left armed on shaky ground:
        #   - API/connection edits while Live was on force a pause (the changed values
        #     invalidate what the scheduler relied on).
        #   - Monitored-path changes force a pause whenever Live would stay armed: the
        #     new paths change what gets measured, so the library size is stale until the
        #     rescan finishes. Re-enabling Live is a separate two-click-confirm save.
        #   - Live (kept or requested) with a failing connection forces a pause.
        forced_pause_for_monitor_change = False
        monitoring_changed = (
            _monitoring_summary_signature(cfg) != _monitoring_summary_signature(saved_cfg)
        )
        if api_config_changed and saved_was_cleanup:
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_api_change = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = "connection settings changed."
        if monitoring_changed and _is_cleanup_mode(cfg.get("RUN_MODE")):
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_monitor_change = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = "monitored paths changed — run Simulate under the new paths, then re-enable Automatic Cleanup."
        if threshold_change_while_cleanup and _is_cleanup_mode(cfg.get("RUN_MODE")):
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_threshold_change = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = ("space thresholds changed — review the rebuilt "
                                                 "deletion plan, then re-enable Automatic Cleanup.")
        if _is_cleanup_mode(cfg.get("RUN_MODE")) and not save_health.get("critical_ok", True):
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_tautulli = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = (save_health.get("required_tooltip")
                                                 or "the media server connection is not healthy.")

        if _is_cleanup_mode(cfg.get("RUN_MODE")):
            # candidate_cfg: the plan stamp must be judged against the config
            # BEING SAVED, not the old file it replaces — otherwise one save
            # could change thresholds AND arm automatic mode against a stamp
            # only the old thresholds match.
            thresholds = _space_threshold_state(cfg, disk_stats(), candidate_cfg=True)
            if not thresholds.get("ok_for_cleanup"):
                # Only reachable with UNCHANGED thresholds (a threshold change
                # while armed force-pauses above): point at the fix.
                _hint = ("" if not _is_cleanup_mode(saved_cfg.get("RUN_MODE"))
                         else " Adjust the thresholds — saving a threshold change "
                              "pauses Scheduler Mode so you can review with Simulate.")
                return jsonify({
                    "ok": False,
                    "error": (thresholds.get("cleanup_tooltip") or "Fix Space Thresholds first.") + _hint,
                }), 400
            # Arming automatic mode always requires that a Simulate has seen the
            # library: over breached limits that means a deletion plan stamped
            # under exactly these thresholds; within limits, proof that at least
            # one Simulate has run (the library snapshot). Without it the first
            # automatic run would act sight-unseen.
            if (not _is_cleanup_mode(saved_cfg.get("RUN_MODE"))
                    and thresholds.get("simulate_required")):
                return jsonify({
                    "ok": False,
                    "error": (thresholds.get("simulate_required_message")
                              or "Run Simulate first, then enable Automatic Cleanup."),
                }), 400

        # An empty MONITOR_DIRS is valid and means "manage nothing" until the
        # user explicitly adds at least one monitored library path.
        _normalize_library_paths(cfg)

        base = Path(FILESYSTEM_CHECK_PATH).resolve(strict=False)
        for monitored_path in cfg.get("MONITOR_DIRS", []) or []:
            try:
                target = Path(monitored_path).resolve(strict=False)
                target.relative_to(base)
            except ValueError:
                return jsonify({"ok": False, "error": f"Monitored directories must stay inside {FILESYSTEM_CHECK_PATH}."}), 400
            if not target.exists() or not target.is_dir():
                return jsonify({"ok": False, "error": f"Monitored directory does not exist: {target}"}), 400

        cfg["CHECK_PATH"] = FILESYSTEM_CHECK_PATH
        cfg["TAUTULLI_APPDATA"] = TAUTULLI_APPDATA_DIR
        cfg["RADARR_APPDATA"] = RADARR_APPDATA_DIR
        # OUTPUT_DIR is infrastructure, not a setting: it decides where app and engine
        # write their files, so it is never accepted from the request body — only the
        # value already on disk carries through.
        cfg.pop("OUTPUT_DIR", None)
        if "OUTPUT_DIR" in saved_cfg:
            cfg["OUTPUT_DIR"] = saved_cfg["OUTPUT_DIR"]

        radarr_cleanup_forced_disabled = False
        if (
            cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None
            and save_health
            and not save_health.get("radarr_connected")
        ):
            # Optional Radarr cleanup can't run without a verified Radarr API. If a save
            # breaks or removes that connection, turn cleanup off instead of keeping a
            # stale enabled value and highlighting the cleanup section.
            cfg["RADARR_OVERSEERR_SECTION_ID"] = None
            radarr_cleanup_forced_disabled = True
            save_health = dict(save_health)
            save_health["radarr_cleanup_forced_disabled"] = True

        if (
            (radarr_section_credentials_changed or radarr_section_cache_incomplete)
            and save_health
            and save_health.get("radarr_connected")
            and save_health.get("plex_connected")
        ):
            # One-shot section auto-detection: runs after saved Radarr/Plex credentials
            # connect, not when the cleanup checkbox is toggled. The cache-repair branch
            # re-runs detection when the cached entry has a section ID but is missing its
            # name/match metadata.
            radarr_section_detection = _detect_radarr_plex_section(cfg)
            detected_section_id = str(radarr_section_detection.get("section_id") or "").strip()
            if _store_radarr_section_detection_cache(cfg, radarr_section_detection):
                if str(cfg.get("RADARR_OVERSEERR_SECTION_ID") or "").strip().lower() == "auto":
                    cfg["RADARR_OVERSEERR_SECTION_ID"] = detected_section_id

            elif radarr_section_credentials_changed:
                _clear_radarr_section_detection_cache(cfg)

        # Logging settings (Advanced)
        try:
            cfg["LOG_RETENTION_DAYS"] = max(0, int(float(cfg.get("LOG_RETENTION_DAYS", 30))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Keep run logs for (days) must be a whole number (0 = keep forever)."}), 400
        cfg["KEEP_INTERRUPTED_LOGS"] = bool(cfg.get("KEEP_INTERRUPTED_LOGS"))
        cfg["DEBUG_MODE"] = bool(cfg.get("DEBUG_MODE"))

        # IMDb refresh interval: the file validator requires >= 1, so clamp here too —
        # otherwise a blank/0 saves fine, then the next load flags it and locks the app.
        try:
            cfg["IMDB_RATINGS_MAX_AGE_DAYS"] = max(1, int(float(cfg.get("IMDB_RATINGS_MAX_AGE_DAYS", 7))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "IMDb refresh interval must be a whole number of days (1 or more)."}), 400

        # Server software (Plex / Jellyfin). Missing/wrong credentials never block a
        # save — the health check flags them, dependent features lock, and runs stay
        # disabled until every selected server connects or the broken one is deselected.
        cfg["USE_PLEX"] = bool(cfg.get("USE_PLEX"))
        cfg["USE_JELLYFIN"] = bool(cfg.get("USE_JELLYFIN"))
        # Both may be off — the default onboarding state. Nothing is functional
        # until a server is enabled and its API connects.

        cfg["JELLYFIN_API_KEY"] = str(cfg.get("JELLYFIN_API_KEY") or "").strip()
        jf_prot = cfg.get("JELLYFIN_PROTECTED_COLLECTIONS", [])
        if isinstance(jf_prot, str):
            jf_prot = [p.strip() for p in jf_prot.split(",")]
        cfg["JELLYFIN_PROTECTED_COLLECTIONS"] = [p for p in (jf_prot or []) if str(p).strip()]

        should_abort_active_run = _is_cleanup_mode(saved_cfg.get("RUN_MODE")) and cfg.get("RUN_MODE") == "paused"
        # Refresh the carried Filtering & Scoring fields from the CURRENT file, not the
        # snapshot taken before the multi-second probes above — a /api/score-config save
        # that landed mid-probe would otherwise be reverted by this write.
        _latest_cfg = load_config()
        for key in _carried_score_fields:
            if key in _latest_cfg:
                cfg[key] = _latest_cfg[key]
        # Never persist a config the loader would flag. The form can't produce these,
        # but a scripted POST could (HEADROOM_GB=1e999, a non-list MOVIE_EXTENSIONS, a
        # non-string API key…), which used to save fine and then wedge every endpoint
        # behind the "config.json was edited outside MediaReducer" lockout on next load.
        _outgoing_issues = _config_file_issues(cfg)
        if _outgoing_issues:
            return jsonify({
                "ok": False,
                "error": "Save rejected — " + "; ".join(
                    f"{i['key']} {i['message']}" for i in _outgoing_issues),
                "invalid_config": _outgoing_issues,
            }), 400
        if not save_config(cfg):
            # A hand edit landed between the entry guard and the write.
            return _invalid_config_response() or (jsonify({
                "ok": False, "error": "Save was refused — config.json changed on disk. Reload the page.",
            }), 409)
        if save_health is not None:
            with _connection_health_cache_lock:
                _connection_health_cache.update({
                    "signature": _connection_health_signature(cfg),
                    "health": save_health,
                    "checked_at": time.time(),
                })
        if should_abort_active_run:
            stop_script()
        # Any Live<->Paused transition resets the background clock to a FULL interval.
        # The interval job keeps counting across mode changes, so without this, pausing
        # with 5s left and re-enabling Live later inherited that near-expired timer and
        # the first automatic run fired almost immediately. (Covers user changes and
        # every forced pause, which all mutate RUN_MODE before this save.)
        if _is_cleanup_mode(saved_cfg.get("RUN_MODE")) != _is_cleanup_mode(cfg.get("RUN_MODE")):
            _restart_schedule_clock()
        # Point the process clock at the saved zone. Moving the zone moves the midnight
        # boundary, so re-burn today's run window — like the startup burn, a clock change
        # must never grant an immediate run.
        _tz_changed = _apply_configured_time_zone(cfg)
        if _tz_changed:
            burn_daily_window_on_startup(reason="time zone changed")
        # Adjusting the daily run time within the same day, in the configured zone
        # (already applied above), matching the engine's own comparison:
        #   • moved to a slot already behind the clock (e.g. 3am → 1am at 2:20am):
        #     treat it as missed and burn today's window so the new time starts
        #     tomorrow — never an instant catch-up run.
        #   • moved to a slot still ahead when today's run already fired: reopen the
        #     window so it can run again today at the new time. Safe with the delay
        #     (a same-day re-run only re-marks). Skipped if the zone also changed —
        #     that burn is a deliberate safety reset we must not undo here.
        _old_rt, _new_rt = _daily_run_time(saved_cfg), _daily_run_time(cfg)
        _now_hhmm = time.strftime("%H:%M")
        if _run_time_moved_into_past(_old_rt, _new_rt, _now_hhmm):
            burn_daily_window_on_startup(reason="daily run time moved earlier")
        elif not _tz_changed and _run_time_moved_ahead_today(_old_rt, _new_rt, _now_hhmm):
            reopen_daily_window(reason="daily run time moved later")
        # Removing or lowering a space limit unschedules the marked prefix: the
        # running delay clocks were for a breach that no longer exists. The queue
        # itself stays — it is the standing eligible deletion order in every mode,
        # exactly matching the engine's own satisfied-limits upkeep
        # (_revalidate_pending_marks). Reconcile here ONLY when this save actually
        # changed a threshold: an unrelated save judging "satisfied" off a stale
        # cached library size would silently reset current delay clocks. Disk
        # figures are read fresh for the same reason (statvfs is cheap; only the
        # library walk is cached). Redline-only mode is exempt: its queue never
        # carries clocks in the first place. The whole check-and-write runs under
        # _run_lock: the entry guard's _run_active check is stale by now (the
        # connection probes above take seconds), and a run or Summary starting
        # mid-write does its own load→modify→save of the same file. Holding the
        # lock keeps runs from starting until the write lands; if one is already
        # active, the next Summary reconciles instead.
        pending_unscheduled = 0
        with _run_lock:
            if (not _run_active and not _summary_active and pending_count()
                    and not _redline_only_mode_cfg(cfg)
                    and _threshold_snapshot(cfg) != _threshold_snapshot(saved_cfg)):
                try:
                    _disk = disk_stats()
                    _lib_gb = library_stats().get("library_gb")
                except Exception:
                    _disk, _lib_gb = None, None
                if not _deletion_limits_exceeded(cfg, _disk, _lib_gb):
                    pending_unscheduled = _unschedule_pending_marks()

        # Storage stats only refresh when the saved config changes what the size scan
        # measures. Runs do their own precheck, so threshold-only saves stay quick and
        # don't kick off disk work.
        summary_started = False
        summary_message = "Summary refresh not needed."
        if _should_refresh_summary_after_config_save(saved_cfg, cfg):
            summary_started, summary_message = run_summary()

        # Rebuild the marked/eligible queue in place from the snapshot when this save
        # changed a threshold, protected collection, or favorites setting — no Simulate.
        # A collections/favorites change re-fetches the live sets; if the server it
        # needs is unreachable, the reconcile is held and the Connections check flags it.
        reconcile_status = _reconcile_after_save(saved_cfg, cfg, source="configuration")

        return jsonify({
            "ok": True,
            "config": cfg,
            "connection_health": save_health,
            "api_config_changed": api_config_changed,
            "server_software_auto_disabled": server_software_auto_disabled,
            "radarr_section_detection": radarr_section_detection,
            "reconcile": reconcile_status,
            "automatic_run_mode_paused": (forced_pause_for_api_change or forced_pause_for_tautulli
                                          or forced_pause_for_monitor_change
                                          or forced_pause_for_threshold_change),
            "automatic_run_mode_paused_reason": (
                "connection settings changed."
                if forced_pause_for_api_change else
                "monitored paths changed — run Simulate under the new paths, then re-enable Automatic Cleanup."
                if forced_pause_for_monitor_change else
                "space thresholds changed — review the rebuilt deletion plan, then re-enable Automatic Cleanup."
                if forced_pause_for_threshold_change else
                (save_health.get("required_tooltip") if save_health else "")
                or "the media server connection is not healthy."
                if forced_pause_for_tautulli else ""
            ),
            "radarr_cleanup_forced_disabled": radarr_cleanup_forced_disabled,
            "summary_refresh_started": summary_started,
            "pending_unscheduled": pending_unscheduled,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400



@app.route("/api/imdb/status")
def api_imdb_status():
    state = _imdb_download_state()
    try:
        state["url"] = str(load_config().get("IMDB_RATINGS_URL") or "").strip()
    except Exception:
        state["url"] = ""
    return jsonify(state)


@app.route("/api/imdb/download", methods=["POST"])
def api_imdb_download():
    """Force-download the latest IMDb ratings TSV, throttled to once every 24 hours."""
    blocked = _filesystem_write_block_response(_imdb_download_state())
    if blocked:
        return blocked
    if _run_active:
        return jsonify({
            "ok": False,
            "message": "A run is active. Try again when it finishes.",
            "status": _imdb_download_state(),
        }), 409

    status = _imdb_download_state()
    if status.get("exists") and status.get("download_locked"):
        return jsonify({
            "ok": False,
            "message": "IMDb ratings were already downloaded within the last 24 hours.",
            "status": status,
        }), 429

    try:
        cfg = load_config()
        url = _validate_imdb_url(cfg.get("IMDB_RATINGS_URL"))
        target = imdb_ratings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")

        tsv_data = _download_imdb_gz(url)
        if not tsv_data.startswith(b"tconst\taverageRating\tnumVotes"):
            raise ValueError("Downloaded file did not look like the IMDb title.ratings.tsv dataset.")

        tmp.write_bytes(tsv_data)
        tmp.replace(target)

        new_status = _imdb_download_state()
        return jsonify({
                "ok": True,
            "message": f"IMDb ratings downloaded and extracted ({new_status.get('size_mb')} MB).",
            "status": new_status,
        })
    except Exception as e:
        try:
            imdb_ratings_path().with_name(imdb_ratings_path().name + ".tmp").unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e), "status": _imdb_download_state()}), 400

@app.route("/api/cache/status")
def api_cache_status():
    return jsonify(_cache_clear_state())

@app.route("/api/cache/clear", methods=["POST"])
def api_clear_cache():
    """Delete the entire cache store, including daily cleanup and dashboard stats."""
    blocked = _filesystem_write_block_response(_cache_clear_state())
    if blocked:
        return blocked
    if _run_active:
        return jsonify({
            "ok": False,
            "message": "A run is active. Try again when it finishes.",
            "status": _cache_clear_state(),
        }), 409
    # A storage summary in flight merges its stats back into the store on exit —
    # a wipe racing that write gets silently resurrected. Same wait-then-refuse as
    # the full reset. (No run/summary is writing, so nothing else holds the DB.)
    for _ in range(100):  # up to ~10s
        if not _summary_active:
            break
        time.sleep(0.1)
    if _summary_active:
        return jsonify({
            "ok": False,
            "message": "A background storage refresh is still running — "
                       "try again in a moment.",
            "status": _cache_clear_state(),
        }), 409

    p = db_path()
    if not p.exists():
        return jsonify({
            "ok": False,
            "message": "No cache store exists yet.",
            "status": _cache_clear_state(),
        }), 404

    removed_movies = 0
    try:
        with db.connect(p) as conn:
            removed_movies = conn.execute(
                "SELECT COUNT(*) FROM metadata_cache").fetchone()[0]
    except Exception:
        # Corrupt/unreadable store still gets deleted below.
        pass

    # Full wipe — the library snapshot goes with it, so the Filtering & Scoring
    # table stays empty until the next Simulate or Cleanup rewrites it. reset_store
    # deletes the .db and its WAL/SHM sidecars under the init lock, so a concurrent
    # status-poll read never lands on a table-less DB.
    db.reset_store(p)

    # The wipe also lost the once-per-day window — re-stamp it so clearing the cache
    # never grants the scheduler a free immediate daily run.
    burn_daily_window_on_startup(reason="cache cleared")

    details = f"Cache cleared ({removed_movies} movie entr{'y' if removed_movies == 1 else 'ies'})."

    summary_started, summary_message = run_summary()
    if summary_started or _summary_active:
        details += " Refreshing storage stats…"
    else:
        details += f" Storage refresh not started: {summary_message}"
    summary_refresh_active = summary_started or _summary_active
    return jsonify({
        "ok": True,
        "message": details,
        "status": _cache_clear_state(),
        "summary_refresh_started": summary_refresh_active,
    })

@app.route("/api/run", methods=["POST"])
def api_run():
    if _run_active:
        return jsonify({"ok": False, "started": False,
                        "message": "A run is already active. Try again when it finishes."}), 409
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    mode = data.get("mode")

    # SAFETY: a missing/unparseable mode must never become a live deletion. Both
    # Dashboard buttons send an explicit mode ("debug_sim" for Simulate, "headroom" for
    # a manual Cleanup, which works even while the scheduler is paused). Anything
    # ambiguous (empty body, garbled JSON, a scripted request with no mode) falls back
    # to Simulate, the non-destructive path. debug_info is not user-facing; the
    # Dashboard's ↻ storage refresh goes through /api/summary/run.
    if mode is None:
        mode = "debug_sim"
    if mode not in ("debug_sim", "headroom", "debug_cleanup"):
        return jsonify({"ok": False, "message": f"Unknown run mode: {mode}"}), 400
    # Debug mode ↔ live are mutually exclusive. "debug_cleanup" is the no-delete
    # diagnostic run that replaces the manual Cleanup while Debug mode is on, so
    # it requires Debug mode; conversely a real live "headroom" run is refused
    # while Debug mode is on (the dashboard morphs the button to Debug Cleanup).
    _debug_on = bool(cfg.get("DEBUG_MODE"))
    if mode == "debug_cleanup" and not _debug_on:
        return jsonify({"ok": False, "message": "Turn on Debug mode in Configuration to use Debug Cleanup."}), 400
    if mode == "headroom" and _debug_on:
        return jsonify({"ok": False, "message": "Debug mode is on — use Debug Cleanup (no deletions)."}), 400
    effective_mode = mode

    health = _refresh_connection_health_cache(cfg, probe=True)
    if not health.get("critical_ok", True):
        return jsonify({
            "ok": False,
            "message": health.get("required_tooltip") or "Connect the selected media server first.",
        }), 400

    disk = disk_stats()
    thresholds = _space_threshold_state(cfg, disk)
    if effective_mode in ("debug_sim", "headroom", "debug_cleanup") and not _has_monitored_dirs(cfg):
        return jsonify({
            "ok": False,
            "message": NO_MONITORED_DIRS_MESSAGE,
        }), 400

    # debug_cleanup uses the SIMULATE gate, not the Live gate: like Simulate it runs
    # even past the 15% headroom safety cap (it deletes nothing), so it must not
    # be blocked by ok_for_cleanup — only by an actually-invalid config.
    if effective_mode in ("debug_sim", "debug_cleanup") and not thresholds.get("ok_for_simulate"):
        return jsonify({
            "ok": False,
            "message": thresholds.get("simulate_tooltip") or "Fix Space Thresholds first.",
        }), 400

    # Debug Cleanup replays the standing marked queue, so it needs a CURRENT plan.
    # Without one (no Simulate yet, or settings changed since), refuse with a hint
    # instead of spinning up a run that can only log "run Simulate first".
    if effective_mode == "debug_cleanup" and thresholds.get("simulate_required"):
        return jsonify({
            "ok": False,
            "message": (thresholds.get("simulate_required_message")
                        or "Run Simulate first — Debug Cleanup replays the marked "
                           "& eligible queue a Simulate builds."),
        }), 400

    if _is_cleanup_mode(effective_mode) and not thresholds.get("ok_for_cleanup"):
        return jsonify({
            "ok": False,
            "message": thresholds.get("cleanup_tooltip") or "Fix Space Thresholds first.",
        }), 400

    # A manual Cleanup deletes immediately (no delay, no daily window), so over
    # breached limits it needs a deletion plan computed under the current
    # thresholds. Within satisfied limits the run is a harmless no-op — the
    # fresh-stats check below reports "already satisfied" instead, which is the
    # same reason the dashboard ghosts the button; don't demand a Simulate here.
    if _is_cleanup_mode(effective_mode) and thresholds.get("simulate_required"):
        try:
            _breached_now = _deletion_limits_exceeded(cfg, disk, library_stats().get("library_gb"))
        except Exception:
            _breached_now = True   # can't tell — keep the safe gate
        if _breached_now:
            return jsonify({"ok": False, "message": thresholds.get("simulate_required_message")
                                                    or "Run Simulate first."}), 400

    # Same pre-check the automatic cleanup tick uses: if every space limit is already
    # satisfied (nothing over Headroom/Redline against the current filesystem, library
    # under the cap), a Simulate or Cleanup would delete nothing, so report it instead
    # of spinning one up. debug_info (status refresh) is exempt.
    if effective_mode in ("debug_sim", "headroom"):
        refresh_ok, refresh_msg, fresh_stats = run_summary_sync()
        if refresh_ok:
            fresh_disk = cached_disk_stats(fresh_stats)
            if fresh_disk:
                disk = fresh_disk
                thresholds = _space_threshold_state(cfg, disk)
                if effective_mode == "debug_sim" and not thresholds.get("ok_for_simulate"):
                    return jsonify({
                        "ok": False,
                        "message": thresholds.get("simulate_tooltip") or "Fix Space Thresholds first.",
                    }), 400
                if _is_cleanup_mode(effective_mode) and not thresholds.get("ok_for_cleanup"):
                    return jsonify({
                        "ok": False,
                        "message": thresholds.get("cleanup_tooltip") or "Fix Space Thresholds first.",
                    }), 400
            # A Simulate is exempt from the satisfied skip in EVERY mode: its job
            # includes building/refreshing the standing marked & eligible queue,
            # which exists regardless of breach state. Only cleanups skip —
            # and "already satisfied" outranks "run Simulate first", matching
            # the dashboard's ghost reason for the same state.
            if effective_mode != "debug_sim" and not _deletion_limits_exceeded(cfg, disk, fresh_stats.get("library_gb")):
                return jsonify({
                    "ok": True,
                    "started": False,
                    "message": "Space limits are already satisfied.",
                })
            if _is_cleanup_mode(effective_mode) and thresholds.get("simulate_required"):
                return jsonify({"ok": False, "message": thresholds.get("simulate_required_message")
                                                        or "Run Simulate first."}), 400
            # A manual Cleanup needs no daily-window pre-check: it prunes every breached
            # target immediately, ignoring the once-per-day schedule and deletion delay
            # (both pace automatic runs).
        else:
            # Degrade, don't block: this precheck only skips a pointless run. The engine
            # measures disk and library at run start and no-ops when every limit is
            # satisfied, so a summary that timed out or was busy must not make
            # Simulate/Live unstartable.
            print(f"Storage precheck skipped, starting the run anyway: {refresh_msg}", flush=True)

    ok, msg = run_script(mode_override=mode, manual=True)
    return jsonify({"ok": ok, "started": ok, "message": msg})

@app.route("/api/summary/run", methods=["POST"])
def api_summary_run():
    """Trigger a quiet background Summary (debug_info) to refresh dashboard stats. Never
    touches lastrun.log or the progress panel, and is mutually exclusive with real runs
    via the run lock."""
    ok, msg = run_summary()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/run/stop", methods=["POST"])
def api_stop():
    stopped = stop_script()
    return jsonify({"ok": stopped, "message":
                    ("Stopped — deletions already made are permanent." if stopped else "No active run.")})

_progress_file_memo_lock = threading.Lock()
_progress_file_memo = {"key": None, "data": {}}


@app.route("/api/run/progress")
def api_run_progress():
    """Structured run progress for the dashboard panel (see engine.emit_progress).
    Parsed once per file version ((path, mtime, size) memo): during a run every
    write changes the key, and on an idle server the polls all hit the memo."""
    p = progress_path()
    data = {}
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        with _progress_file_memo_lock:
            memo_hit = _progress_file_memo["key"] == key
            if memo_hit:
                data = _progress_file_memo["data"]
        if not memo_hit:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            with _progress_file_memo_lock:
                _progress_file_memo["key"] = key
                _progress_file_memo["data"] = data
        data = dict(data)   # the fallbacks below may mutate; never edit the memo copy
    except OSError:
        data = {}
    # If the process is gone but the file never reached a terminal state (crash/kill),
    # show the last phase as failed instead of a phantom running state.
    if not _run_active and data.get("status") in ("starting", "running"):
        data["status"] = "error"
        data["message"] = data.get("message") or "Run ended unexpectedly."
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

# ── Log section markers ───────────────────────────────────────────────────────
# Match the engine's exact log strings (sim and live) so the dashboard's stat tiles
# can deep-link into the run log. A section reports available only once the run has
# genuinely written its marker. Each stage opens with a one-line
# "====== <TITLE> ======" banner from the engine's log_stage(); the content markers
# are fallbacks in case a banner is ever missing from a partial log.
_LOG_SECTION_RES = {
    "scan":      re.compile(r"={3,} SCAN ={3,}|Processing [\d,]+ unique movie entries\."),
    # "Marked-queue re-verify" is the Debug Cleanup / fast-path analog of the full
    # scan's ELIGIBLE CANDIDATES stage — the eligible & marked queue, re-scored.
    "eligible":  re.compile(r"={3,} ELIGIBLE CANDIDATES ={3,}|Candidate stats:|candidates sorted by deletion priority|Marked-queue re-verify"),
    # A Debug Cleanup previews the deletions ("Delete-from-queue preview" banner →
    # the WOULD DELETE list); a real Cleanup logs "Deleted file:".
    "deletions": re.compile(r"={3,} (SIMULATION|DELETIONS) ={3,}|Simulating deletions — target:|DRY RUN DELETE #1:|Deleted file: |Delete-from-queue preview"),
    "summary":   re.compile(r"SUMMARY {1,2}\["),
    "errors":    re.compile(r"ERROR|ABORT|WARN(ING)?[ :]|SKIP identity_mismatch|COMPLETED WITH ERRORS"),
}
_LOG_SECTION_MAX_LINES = 30000


def _log_section_indexes(lines: list) -> dict:
    """First matching line index for each section marker (or None)."""
    idx = {k: None for k in _LOG_SECTION_RES}
    for i, line in enumerate(lines):
        for kind, rx in _LOG_SECTION_RES.items():
            if idx[kind] is None and rx.search(line):
                idx[kind] = i
        if all(v is not None for v in idx.values()):
            break
    return idx


# /api/logs/last is polled every 750 ms while a run streams and lastrun.log grows to
# many MB, so re-scanning the whole file per poll is O(file) forever. The section flags
# only need which markers EXIST, so scan incrementally: remember the byte position and
# found flags, read only appended bytes, and reset when the file shrinks or is replaced.
_log_scan_lock = threading.Lock()
_log_scan_state = {"key": None, "pos": 0, "found": {}, "partial": ""}


def _log_sections_found(p: Path) -> dict:
    """{kind: bool} for the section jump buttons, scanning only new bytes."""
    try:
        st = p.stat()
        key = (st.st_dev, st.st_ino)
    except OSError:
        return {k: False for k in _LOG_SECTION_RES}
    with _log_scan_lock:
        s = _log_scan_state
        if s["key"] != key or st.st_size < s["pos"]:
            s.update({"key": key, "pos": 0,
                      "found": {k: False for k in _LOG_SECTION_RES}, "partial": ""})
        if not all(s["found"].values()) and st.st_size > s["pos"]:
            with open(p, "rb") as f:
                f.seek(s["pos"])
                chunk = f.read()
                s["pos"] += len(chunk)
            lines = (s["partial"] + chunk.decode("utf-8", errors="replace")).split("\n")
            s["partial"] = lines.pop()  # the last piece may be mid-write
            for line in lines:
                for kind, rx in _LOG_SECTION_RES.items():
                    if not s["found"][kind] and rx.search(line):
                        s["found"][kind] = True
        return dict(s["found"])


def _read_tail_lines(p: Path, n: int) -> list:
    """Last n lines without reading the whole file: seek back in growing blocks from the
    end until enough newlines are covered."""
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        want = min(size, max(4096, n * 200))
        while True:
            f.seek(size - want)
            data = f.read(want)
            if want == size or data.count(b"\n") > n:
                break
            want = min(size, want * 2)
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    if want < size and lines:
        lines = lines[1:]  # first line is partial unless we read the whole file
    return lines[-n:]


def _error_banner_start(lines: list):
    """Index of the one-line "!!!!!! COMPLETED WITH ERRORS !!!!!!" banner that
    opens the run-end error report, or None when the run has none."""
    return next((i for i, ln in enumerate(lines) if "COMPLETED WITH ERRORS" in ln), None)


def _extract_errors_report(lines: list, idx: dict):
    """The errors view, in order of preference:

    1. The run-end error report — the "!!!!!" banner block the engine prints
       right before the summary, which lays out every skipped file with its
       explanation. Served whole, from the banner to the summary border.
    2. For runs that died early: everything from the first ABORT/ERROR line to
       the end of the file (the failure and its context).
    3. Fallback: every flagged line in the run (errors, warnings, identity
       mismatches) as a filtered list.
    """
    start = _error_banner_start(lines)
    if start is not None:
        summary_i = idx.get("summary")
        end = summary_i if (summary_i is not None and summary_i > start) else len(lines)
        return True, "".join(lines[start:end][:_LOG_SECTION_MAX_LINES])

    abort_re = re.compile(r"ABORT|ERROR")
    abort_i = next((i for i, ln in enumerate(lines) if abort_re.search(ln)), None)
    if abort_i is not None:
        # Present a run that died partway like the identity-mismatch report: a banner,
        # then the failure and its context.
        banner = "!!!!!! RUN FAILED — stopped before finishing !!!!!!\n\n"
        return True, banner + "".join(lines[abort_i:][:_LOG_SECTION_MAX_LINES])

    rx = _LOG_SECTION_RES["errors"]
    hits = [ln for ln in lines if rx.search(ln)]
    if not hits:
        return False, ""
    header = f"{len(hits)} flagged line(s) in this run (errors, warnings, identity mismatches):\n\n"
    return True, header + "".join(hits[:_LOG_SECTION_MAX_LINES])


def _extract_log_section(lines: list, kind: str):
    """Return (found, text) for one section of a run log.

    Sections are contiguous slices between the boundary markers above, except
    "errors" which is the filtered set of flagged lines from the whole run.
    """
    idx = _log_section_indexes(lines)
    if kind == "errors":
        return _extract_errors_report(lines, idx)

    if idx.get(kind) is None:
        return False, ""

    # Each stage opens on its one-line "====== TITLE ======" banner; a section
    # runs from its banner to the next later-ordered section's banner.
    starts = {k: idx.get(k) for k in ("scan", "eligible", "deletions", "summary")}
    start = starts[kind]

    if kind == "summary":
        end = len(lines)
    else:
        later = [v for k, v in starts.items()
                 if v is not None and v > start and _SECTION_ORDER[k] > _SECTION_ORDER[kind]]
        # The run-end error report sits between deletions and summary; it belongs
        # to the errors view, so cap content sections there instead of absorbing it.
        banner = _error_banner_start(lines)
        if banner is not None and banner > start:
            later.append(banner)
        end = min(later) if later else len(lines)
    return True, "".join(lines[start:end][:_LOG_SECTION_MAX_LINES])


_SECTION_ORDER = {"scan": 0, "eligible": 1, "deletions": 2, "summary": 3}


@app.route("/api/logs/last")
def api_logs_last():
    p = log_path()
    if not p.exists():
        resp = jsonify({"content": "No log file yet — run the script first.", "sections": {}})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    raw = request.args.get("lines", "500")
    if str(raw).strip().lower() == "all":
        n = 50000
    else:
        try:
            n = max(1, min(50000, int(raw)))
        except (TypeError, ValueError):
            n = 500
    tail = _read_tail_lines(p, n)
    resp = jsonify({
        "content": _format_log_text_for_display("".join(tail)),
        "sections": _log_sections_found(p),
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/logs/section")
def api_logs_section():
    kind = str(request.args.get("kind") or "").strip()
    if kind not in _LOG_SECTION_RES:
        return jsonify({"ok": False, "error": "Unknown log section."}), 400
    p = log_path()
    if not p.exists():
        return jsonify({"ok": False, "found": False, "content": ""})
    with open(p, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    found, text = _extract_log_section(all_lines, kind)
    resp = jsonify({"ok": True, "found": found,
                    "content": _format_log_text_for_display(text) if found else ""})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/logs/deleted")
def api_logs_deleted():
    try:
        raw_limit = request.args.get("limit")
        limit = None if raw_limit in (None, "", "all") else max(1, min(20000, int(raw_limit)))
    except (TypeError, ValueError):
        limit = 100
    entries = deleted_entries(limit)
    deleted = deleted_stats()
    # Newest first for display; marked-for-deletion entries ride along so the history
    # modal can pin them on top.
    entries = list(reversed(entries))
    marked = pending_deletion_entries()
    resp = jsonify({
        "count": deleted["count"],
        "reclaimed_bytes": deleted["reclaimed_bytes"],
        "reclaimed_label": deleted["reclaimed_label"],
        "entries": entries,
        "marked": marked,
        "marked_count": len(marked),
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/logs/deleted/clear", methods=["POST"])
def api_logs_deleted_clear():
    blocked = _filesystem_write_block_response()
    if blocked:
        return blocked
    if _run_active:
        return jsonify({
            "ok": False,
            "message": "A run is active. Try again when it finishes.",
            "count": deleted_count(),
            "reclaimed_bytes": deleted_stats()["reclaimed_bytes"],
            "reclaimed_label": deleted_stats()["reclaimed_label"],
        }), 409
    p = deleted_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        resp = jsonify({"ok": True, "message": "Deleted history erased.", "count": 0, "reclaimed_bytes": 0, "reclaimed_label": "0 GB", "entries": []})
    except OSError as e:
        deleted = deleted_stats()
        resp = jsonify({"ok": False, "message": f"Could not erase deleted.log: {e}", "count": deleted["count"], "reclaimed_bytes": deleted["reclaimed_bytes"], "reclaimed_label": deleted["reclaimed_label"]})
        resp.status_code = 500
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/logs/archived")
def api_logs_archived():
    d = logs_dir()
    if not d.exists():
        return jsonify({"files": []})
    try:
        files = sorted(
            [f.name for f in d.iterdir() if f.suffix == ".log"],
            reverse=True
        )
    except OSError:
        files = []
    return jsonify({"files": files[:50]})

@app.route("/api/logs/archived/<filename>")
def api_logs_archived_file(filename):
    d = logs_dir()
    try:
        return send_from_directory(d, filename, as_attachment=False)
    except Exception:
        return jsonify({"error": "File not found"}), 404


def _archived_logs_state() -> dict:
    """Count + total size of archived run logs, for the Clear-logs control."""
    d = logs_dir()
    count = 0
    total = 0
    try:
        files = list(d.glob("*.log")) if d.is_dir() else []
    except OSError:
        files = []
    for f in files:
        try:
            total += f.stat().st_size
            count += 1
        except OSError:
            continue
    if total >= 1024 * 1024:
        size_label = f"{total / (1024 * 1024):.1f} MB"
    elif total >= 1024:
        size_label = f"{total / 1024:.0f} KB"
    else:
        size_label = f"{total} B"
    return {
        "ok": True,
        "count": count,
        "empty": count == 0,
        "label": ("Archived log directory is empty."
                  if count == 0
                  else f"{count} archived run log{'s' if count != 1 else ''} · {size_label}"),
    }


@app.route("/api/logs/archived/status")
def api_logs_archived_status():
    resp = jsonify(_archived_logs_state())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/logs/archived/clear", methods=["POST"])
def api_logs_archived_clear():
    """Delete every archived run log in logs/ (keeps the folder, lastrun.log,
    and the deletion history)."""
    blocked = _filesystem_write_block_response(_archived_logs_state())
    if blocked:
        return blocked
    if _run_active:
        return jsonify({
            "ok": False,
            "message": "A run is active. Try again when it finishes.",
            "status": _archived_logs_state(),
        }), 409
    d = logs_dir()
    removed = 0
    errors = []
    if d.is_dir():
        for f in d.glob("*.log"):
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                errors.append(f"{f.name}: {e}")
    if errors:
        resp = jsonify({"ok": False,
                        "message": "Some logs could not be removed: " + "; ".join(errors),
                        "status": _archived_logs_state()})
        resp.status_code = 500
    else:
        resp = jsonify({"ok": True,
                        "message": f"Cleared {removed} archived run log{'s' if removed != 1 else ''}.",
                        "status": _archived_logs_state()})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

# ── Scheduler ─────────────────────────────────────────────────────────────────

def _deletion_limits_exceeded(cfg: dict, disk: dict | None, library_gb) -> bool:
    """True if any deletion trigger is currently breached, shared by the automatic Live
    tick and the manual Simulate/Live launch path so both decide the same way.

    Mirrors engine.py's trigger formula: over_limit (used ≥ total − Headroom),
    redline_hit (free ≤ Redline), library_cap_hit (library size > Library Size Cap).
    Disk figures are live; the library size is the last value the engine wrote to
    the store (kept fresh by the Summary after every Cleanup, on save, on startup, and
    on paused/idle ticks).

    ONLY an optimization to avoid spinning up a run that would delete nothing (e.g. the
    library already shrank back under the cap). It never authorizes a deletion — the
    engine independently recomputes all of this from fresh on-disk sizes first. So
    whenever a value can't be read cleanly, or a cap is set but its size is unknown, this
    returns True (don't skip; let the engine be the authority)."""
    disk = disk or {}
    try:
        used_gb  = float(disk.get("used_gb"))
        total_gb = float(disk.get("total_gb"))
        free_gb  = float(disk.get("free_gb"))
    except (TypeError, ValueError):
        return True  # can't read disk → don't skip

    headroom_gb, ok = _coerce_float(cfg.get("HEADROOM_GB"))
    if not ok or headroom_gb is None:
        return True  # malformed headroom → don't skip
    over_limit = used_gb >= (total_gb - headroom_gb)

    redline_hit = False
    if cfg.get("REDLINE_GB") is not None:
        redline_gb, ok = _coerce_float(cfg.get("REDLINE_GB"))
        if ok and redline_gb is not None:
            redline_hit = free_gb <= redline_gb

    library_cap_hit = False
    cap_value = cfg.get("MAX_LIBRARY_GB")
    if cap_value is not None:
        cap_gb, ok = _coerce_float(cap_value)
        if ok and cap_gb is not None and cap_gb > 0:
            if library_gb is None:
                return True  # cap active but size unknown → let the engine check
            try:
                library_cap_hit = float(library_gb) > cap_gb
            except (TypeError, ValueError):
                return True

    return over_limit or redline_hit or library_cap_hit


def _scheduled_tick():
    """The single background clock's callback.

    In Live mode it launches an automatic cleanup; otherwise (or when Live is on but
    connections/thresholds aren't currently safe to delete) it runs a quiet
    Summary/debug_info refresh so dashboard disk/library numbers stay fresh.
    run_script()/run_summary() each pause this clock while working and restart it from
    zero when done, so the guard below is just belt-and-suspenders against an overlap."""
    if _run_active or _summary_active:
        return

    cfg = load_config()
    # A reconcile held because a needed server was down: retry it now that the tick
    # is here — if the connection recovered (or the server was disabled) it fires and
    # IS this tick's work; otherwise it stays held for the next tick.
    if _retry_held_reconcile(cfg):
        return
    # No monitored library paths (onboarding, or the user removed them all): there
    # is nothing to scan, size, or clean up, so the scheduler stays truly idle —
    # no Summary, no maintenance Simulate. The dashboard shows "Scheduler paused"
    # and dashes the storage numbers. Adding a path resumes the ticks.
    if not _has_monitored_dirs(cfg):
        return

    if _is_cleanup_mode(cfg.get("RUN_MODE")):
        # Only launch a deletion pass when it's actually safe. If connections aren't
        # ready, fall back to a Summary so stats still refresh. Otherwise refresh storage
        # first, then decide whether a cleanup run is needed.
        if not _refresh_connection_health_cache(cfg, probe=True).get("critical_ok", True):
            run_summary()
            return
        refresh_ok, refresh_msg, fresh_stats = run_summary_sync()
        if not refresh_ok:
            print(f"Scheduled cleanup precheck failed: {refresh_msg}", flush=True)
            return
        disk = cached_disk_stats(fresh_stats) or disk_stats()
        threshold_state = _space_threshold_state(cfg, disk, fresh_stats.get("library_gb"))
        if not threshold_state.get("ok_for_cleanup"):
            # Thresholds safe when Live was armed can stop being safe later — e.g. files
            # copied in push the cap past the safety floor. Silently skipping every tick
            # left Live armed with nothing running and no explanation: pause with the
            # reason, like every other forced pause. Re-arming takes the two-click confirm.
            fresh_cfg = load_config()   # fresh: don't clobber a save that landed mid-summary
            if _is_cleanup_mode(fresh_cfg.get("RUN_MODE")):
                fresh_cfg["RUN_MODE"] = "paused"
                fresh_cfg["_RUN_MODE_AUTOPAUSE_REASON"] = (threshold_state.get("cleanup_tooltip")
                                                           or "Space Thresholds are no longer safe.")
                if save_config(fresh_cfg):
                    _restart_schedule_clock()
                    print("Scheduled tick: Space Thresholds unsafe — Automatic Cleanup paused "
                          f"({fresh_cfg['_RUN_MODE_AUTOPAUSE_REASON']})", flush=True)
            return
        # Nothing to delete? Don't launch a Cleanup. The Summary precheck above already
        # refreshed both filesystem capacity and media library size.
        if not _deletion_limits_exceeded(cfg, disk, fresh_stats.get("library_gb")):
            return
        # Daily-only breach (headroom/cap, no redline) with today's window already
        # used, or before the configured run time: the engine would do its full
        # startup — connection checks, IMDb check, another library walk — just to
        # log "waiting until tomorrow". Skip the launch; up to ~96 pointless engine
        # spins a day otherwise while marks wait out the delay. Redline breaches
        # always launch (they ignore the window), and any doubt fails toward
        # launching — the engine stays the authority.
        try:
            _free_gb = float((disk or {}).get("free_gb"))
            _redline = cfg.get("REDLINE_GB")
            _redline_hit = _redline is not None and _free_gb <= float(_redline)
        except (TypeError, ValueError):
            _redline_hit = True
        if not _redline_hit and (_headroom_window_used_today()
                                 or time.strftime("%H:%M") < _daily_run_time(cfg)):
            return
        # run_script() owns the lock and rejects overlaps.
        run_script()
    else:
        # Paused (or any non-Live mode). Once a day, after the configured daily run
        # time, run a maintenance Simulate (a full scan, no deletions) so the plan
        # and the "full scan within 48h" clock stay current even with no automatic
        # Cleanup armed to refresh them — but only when connections are healthy, so
        # a down API just falls back to the quiet Summary instead of spinning the
        # engine every tick. Every other tick is the quiet Summary (dashboard
        # disk/library numbers stay fresh without touching lastrun.log/progress).
        global _last_paused_scan_epoch
        run_epoch = _todays_daily_run_epoch(cfg)
        if (_paused_daily_scan_due(cfg)
                and _last_paused_scan_epoch != run_epoch
                and _refresh_connection_health_cache(cfg, probe=True).get("critical_ok", False)):
            # Stamp the attempt BEFORE launching: a successful Simulate refreshes the
            # snapshot (clearing _paused_daily_scan_due on its own), but one that fails
            # past the probe would otherwise respin every tick — this holds it to one
            # attempt per daily window (a new day, a run-time change, a restart, or a
            # manual Simulate re-arms it).
            _last_paused_scan_epoch = run_epoch
            print("Scheduled tick: paused — running a daily maintenance Simulate to "
                  "keep the deletion plan current.", flush=True)
            run_script(mode_override="debug_sim")   # Simulate: full scan, never deletes
        else:
            run_summary()

def validate_store_on_startup() -> None:
    """PRESERVE the store across a restart — an unchanged, recent plan stays
    usable with no re-scan. Nothing is invalidated here; whether the saved plan
    can still be trusted is decided LIVE:
      • monitored-path or scoring/threshold changes → the plan's stamp no longer
        matches (_pending_plan_current / _simulate_evidence), so Cleanup + arming
        ghost until a fresh Simulate;
      • a full scan older than two days → _full_scan_overdue() ghosts them the
        same way (a daily scan — the armed Cleanup, or the paused-mode maintenance
        Simulate — normally refreshes it);
      • vanished files / newly-protected movies → the 15-minute upkeep drops those
        marks, and every deletion re-verifies protection fresh.
    deleted.log and the logs/ archive are kept, and the progress panel + last-run
    log carry over."""
    print("Startup: cache store preserved — its saved plan stays usable unless the "
          "config changed or its last full scan is over two days old (then run Simulate).",
          flush=True)


# Always start safe: preserve the store but re-validate its plan (locking Cleanup +
# arming if it went stale or the library changed), adopt the configured time zone
# before anything reads the clock, never resume a saved Live mode after a restart,
# never leave an undersized Library Size Cap armed, and burn today's daily-run window
# so a restart can't grant an immediate run.
validate_store_on_startup()
_apply_configured_time_zone()
force_paused_run_mode_on_startup()
disable_undersized_library_cap_on_startup()
burn_daily_window_on_startup()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    _scheduled_tick, "interval", minutes=SCHEDULE_INTERVAL_MINUTES,
    id="engine", max_instances=1,
    next_run_time=datetime.now() + timedelta(minutes=SCHEDULE_INTERVAL_MINUTES),
)
scheduler.start()
_kick_startup_health_check_and_summary(load_config())

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Initialise config on first boot
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        import shutil as _sh
        _sh.copy(DEFAULT_CFG_PATH, CONFIG_PATH)
        print(f"Created default config at {CONFIG_PATH}")

    # Registered on the main thread (signal.signal requires it) and only when run
    # directly, never on import (tests). Lets a `docker stop` finish an in-flight
    # deletion cleanly instead of SIGKILLing the engine with the container.
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _graceful_shutdown)
        except (ValueError, OSError):
            pass

    app.run(host="0.0.0.0", port=7474, debug=False, threaded=True)
