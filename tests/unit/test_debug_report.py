"""The sanitized debug report must (1) carry the decision-state sections that
make a run diagnosable — scheduler/clock, space verdict, deletion plan &
currency, recent errors — and (2) never leak private values: no movie/
collection names, no filesystem paths, no IP addresses. Free-form text (the
marked queue, run-log error lines, the progress message) is the risky part,
so this pins that those are de-identified."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

tmp = Path(tempfile.mkdtemp())

CFG = {
    "RUN_MODE": "paused", "TIME_ZONE": "auto", "DAILY_RUN_TIME": "03:30",
    "DELETE_DELAY_DAYS": 3, "HEADROOM_GB": 1000, "REDLINE_GB": 200,
    "MAX_LIBRARY_GB": None, "USE_PLEX": True, "USE_JELLYFIN": True,
    "PROTECTED_COLLECTIONS": ["Kids Movies"],
    "JELLYFIN_PROTECTED_COLLECTIONS": ["Favorites"],
    "MONITOR_DIRS": ["/library/movies"], "IMDB_RATINGS_MAX_AGE_DAYS": 7,
    "OUTPUT_DIR": str(tmp),
    # Blank connections so the live-API sections skip (no network in tests).
    "TAUTULLI_URL": "", "TAUTULLI_API_KEY": "", "JELLYFIN_URL": "",
    "JELLYFIN_API_KEY": "", "PLEX_URL": "", "PLEX_TOKEN": "",
    "RADARR_URL": "", "RADARR_API_KEY": "",
}

A.load_config = lambda: dict(CFG)
A.output_dir = lambda: tmp
A._connection_health_state = lambda cfg, probe=False: {
    "critical_ok": False, "severity": "error",
    "plex_connected": False, "tautulli_connected": False,
    "jellyfin_connected": False, "radarr_connected": False,
    "errors": [], "warnings": [], "media_path_compatibility": [], "appdata": {}}
A.disk_stats = lambda: {"used_gb": 800.0, "total_gb": 1000.0, "free_gb": 50.0}
A.cached_disk_stats = lambda stats=None: {"used_gb": 800.0, "total_gb": 1000.0, "free_gb": 50.0}
A.library_stats = lambda: {"library_gb": 400.0, "updated_at": time.time()}
A._deletion_limits_exceeded = lambda cfg, disk, lib: True

# Connect Tautulli and make its API raise an error that echoes a media path —
# a remote error body can do this. The report must run it through redact(), not
# print it raw (regression for the sanitizer-bypass security finding).
API_ERR_PATH = "/library/Movies HD/Blade Runner 2049/film.mkv"
A._effective_connection_values = lambda cfg: {"tautulli_url": "http://tautulli.test", "tautulli_key": "k"}
def _boom(*a, **k):
    raise RuntimeError(f"Tautulli API error: cannot read {API_ERR_PATH}")
A._tautulli_api_request = _boom

# A marked queue whose entries embed a private title and path — the report
# must surface counts/scores/dates but NONE of those identifying strings.
now = time.time()
PRIVATE_TITLE = "Secret Title"
PRIVATE_PATH_SEG = "Another Private Movie"
pend = {
    "monitor_dirs": ["/library/movies"],
    "plan_config": {},   # wrong shape → plan reported stale
    "entries": {
        f"/library/movies/{PRIVATE_TITLE} (2001)/{PRIVATE_TITLE}.mkv":
            {"marked_at": now - 2 * 86400, "title": PRIVATE_TITLE, "size_bytes": 5_000_000_000, "score": 12.5},
        f"/library/movies/{PRIVATE_PATH_SEG}/file.mkv":
            {"marked_at": now - 10 * 86400, "title": PRIVATE_PATH_SEG, "size_bytes": 8_200_000_000, "score": 7.0},
    },
}
(tmp / "cache.json").write_text(json.dumps({"pending": pend}), encoding="utf-8")

# A run log with flagged lines embedding a private collection name and — the
# regression that matters — an UNQUOTED absolute path whose folder/file names
# contain spaces (a real movie title). A naive path regex that stops at the
# first space would leak everything after it.
PRIVATE_COLLECTION = "Keep Forever"
SPACED_TITLE = "The Grey (2012)"
SPACED_FOLDER = "Movies LQ"
(tmp / "lastrun.log").write_text(
    "2026-07-15 06:59:11 - Scanning library (info line).\n"
    f"2026-07-15 06:59:11 - ABORT: Plex protected collection(s) ['{PRIVATE_COLLECTION}'] not found.\n"
    f"2026-07-15 06:59:11 - SKIP identity_mismatch | path=/library/{SPACED_FOLDER}/{SPACED_TITLE}/{SPACED_TITLE}.avi\n",
    encoding="utf-8")

report = A._build_debug_report()

# (1) Every decision-state section is present.
for section in ("SCHEDULER & CLOCK", "SPACE VERDICT & FORECAST",
                "PROTECTED COLLECTIONS", "DELETION PLAN & CURRENCY",
                "RECENT ERRORS"):
    check(f"section present: {section}", section in report)
check("app version line present", "mediareducer:" in report)

# (2) The plan section reports currency + a de-identified sample row.
check("plan reported stale (Live locked)", "Automatic Cleanup is LOCKED" in report)
check("marked queue count shown", "marked queue: 2 total" in report)
check("sample row shows score", "score=12.5" in report)
check("sample row shows size, not title", "size=5.00 GB" in report)

# (3) No private value leaks anywhere in the report.
check("no private movie title leaks", PRIVATE_TITLE not in report)
check("no private path segment leaks", PRIVATE_PATH_SEG not in report)
check("no raw monitored path leaks", "/library/movies/" not in report)
check("no private collection name leaks", PRIVATE_COLLECTION not in report)

# The flagged ABORT line still appears — just de-identified.
check("recent-errors keeps the ABORT, redacted",
      "ABORT" in report and PRIVATE_COLLECTION not in report)

# Spaced-title path must be fully tokenized — no leak after the first space.
check("no spaced movie title leaks from an unquoted path", SPACED_TITLE not in report)
check("no 'Grey' fragment leaks", "Grey" not in report)
check("no spaced folder name leaks", SPACED_FOLDER not in report and "LQ" not in report)

# A raw API-error string carrying a media path must be redacted, and the error
# still shown (so support can see what failed).
check("API error surfaces in the report", "Tautulli API error" in report)
check("but the path inside the API error is redacted",
      API_ERR_PATH not in report and "Blade Runner 2049" not in report)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
