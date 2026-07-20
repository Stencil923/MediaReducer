"""Deletion delay plumbing: whole-day validation on config saves, and the
marked-for-deletion queue composition the history modal displays."""
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import atexit
import shutil
import tempfile
# Private per-run OUTPUT_DIR: a fixed shared path leaks window/pending
# state between test files and suite runs (rest of the suite uses tempdirs).
_OUT_DIR = tempfile.mkdtemp(prefix="mr-test-out.")
atexit.register(shutil.rmtree, _OUT_DIR, True)
import app as A

_real_load_config = A.load_config  # capture before the fake replaces it

_state = {"cfg": {}}

def fake_load_config():
    return dict(_state["cfg"])
def fake_save_config(cfg, **k):
    _state["cfg"] = dict(cfg)
    return True

A.load_config = fake_load_config
A.save_config = fake_save_config
A.refresh_sample_pool = lambda *a, **k: (True, "ok")
A.run_summary = lambda *a, **k: (False, "skip")
A._invalid_config_response = lambda: None
A._refresh_connection_health_cache = lambda cfg, probe=True: {
    "critical_ok": False, "tautulli_connected": False,
    "jellyfin_connected": False, "radarr_connected": False,
}

client = A.app.test_client()

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

BASE = {
    "RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
    "USE_PLEX": False, "USE_JELLYFIN": False,
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
    "OUTPUT_DIR": _OUT_DIR,
}

def save(payload_over):
    _state["cfg"] = dict(BASE)
    p = {"RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
         "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
         "USE_PLEX": False, "USE_JELLYFIN": False,
         "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
    p.update(payload_over)
    r = client.post("/api/config", json=p, headers={"X-MediaReducer": "1"})
    return r.status_code, dict(_state["cfg"])

code, cfg = save({"DELETE_DELAY_DAYS": 7})
check("whole days accepted", code == 200 and cfg.get("DELETE_DELAY_DAYS") == 7)
code, cfg = save({"DELETE_DELAY_DAYS": 1})
check("minimum 1 accepted", code == 200 and cfg.get("DELETE_DELAY_DAYS") == 1)
code, cfg = save({"DELETE_DELAY_DAYS": None})
check("blank saves as the 1-day minimum", code == 200 and cfg.get("DELETE_DELAY_DAYS") == 1)
code, cfg = save({"DELETE_DELAY_DAYS": 0})
check("0 rejected (never same day)", code == 400)
code, cfg = save({"DELETE_DELAY_DAYS": 3.5})
check("decimals rejected", code == 400)
code, cfg = save({"DELETE_DELAY_DAYS": 400})
check("400 days rejected", code == 400)
code, cfg = save({"DELETE_DELAY_DAYS": -1})
check("negative rejected", code == 400)

check("file validator flags decimals",
      any(i["key"] == "DELETE_DELAY_DAYS" for i in A._config_file_issues({"DELETE_DELAY_DAYS": 2.5})))
# 0 has no valid meaning for a min-1 field, so a hand-edited 0 is flagged like any
# other out-of-range edit (the GUI never writes it); a real value passes.
check("file validator flags a hand-edited 0, accepts 30",
      any(i["key"] == "DELETE_DELAY_DAYS" for i in A._config_file_issues({"DELETE_DELAY_DAYS": 0}))
      and not A._config_file_issues({"DELETE_DELAY_DAYS": 30}))
check("engine floors an out-of-range 0 to the 1-day minimum (defensive)",
      A._delete_delay_days({"DELETE_DELAY_DAYS": 0}) == 1)

# A config.json holding a below-1 value locks the app out via the standard hand-edit
# guard, rather than being silently reinterpreted at load.
with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td, "config.json")
    cfg_path.write_text(json.dumps({"DELETE_DELAY_DAYS": 0}), encoding="utf-8")
    _orig_cfg_path = A.CONFIG_PATH
    A.CONFIG_PATH = cfg_path
    try:
        _real_load_config()
    finally:
        A.CONFIG_PATH = _orig_cfg_path
    check("a hand-edited 0 in config.json locks out",
          any(i["key"] == "DELETE_DELAY_DAYS" for i in A._CONFIG_FILE_ISSUES))

# ── Queue composition for the history modal ─────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, OUTPUT_DIR=td, DELETE_DELAY_DAYS=7)
    now = time.time()
    Path(td, "pending_deletions.json").write_text(json.dumps({"schema": 1, "entries": {
        "/library/movies/A/A.mkv": {"title": "Movie A", "size_bytes": 2_000_000_000,
                                    "score": 1.5, "marked_at": now - 86400},        # 1 day ago
        "/library/movies/B/B.mkv": {"title": "Movie B", "size_bytes": 1_000_000_000,
                                    "score": 2.0, "marked_at": now - 9 * 86400},    # expired
        "/library/movies/C/C.mkv": {"title": "Movie C", "size_bytes": 500_000_000,
                                    "score": 3.0, "marked_at": now},                # just now
    }}), encoding="utf-8")
    entries = A.pending_deletion_entries()
    check("queue count", A.pending_count() == 3 and len(entries) == 3)
    check("newest mark first", entries[0]["title"] == "Movie C" and entries[-1]["title"] == "Movie B")
    by_title = {e["title"]: e for e in entries}
    check("days remaining honors the current delay", by_title["Movie A"]["days_remaining"] == 6)
    check("expired mark reads as deletable now",
          by_title["Movie B"]["days_remaining"] == 0 and "deletable now" in by_title["Movie B"]["line"])
    check("pending mark carries its eligibility date",
          f"deletable from {by_title['Movie A']['delete_on']}" in by_title["Movie A"]["line"])
    # Waiting-mark ages feed the Config page's "lowering the delay deletes more"
    # warning: Movie C (marked now) age 0, Movie A (1 day ago) age 1; Movie B is
    # already ripe under the 7-day delay, so it is not waiting.
    check("forecast reports ages of still-waiting marks",
          A.pending_delete_forecast().get("waiting_ages") == [0, 1])
    # Shortening the delay moves pending deletions up.
    _state["cfg"]["DELETE_DELAY_DAYS"] = 2
    entries = {e["title"]: e for e in A.pending_deletion_entries()}
    check("shorter delay shrinks the countdown", entries["Movie A"]["days_remaining"] == 1)

# ── A save that CHANGES a threshold and satisfies every limit clears the queue ─
# Removing/lowering a cap leaves marks a run would never act on, and Simulate is
# disabled once limits are satisfied — so the save must clear them itself. An
# unrelated save must NOT: it may be judging "satisfied" off a stale cached
# library size, and clearing would silently reset the marks' delay clocks.
_orig_limits = A._deletion_limits_exceeded
_orig_disk = A.disk_stats
_orig_lib = A.library_stats
A.disk_stats = lambda: {"total_gb": 1000, "used_gb": 100, "free_gb": 900, "pct_used": 10}
A.library_stats = lambda: {"library_gb": 100.0}
try:
    with tempfile.TemporaryDirectory() as td:
        def _write_queue():
            Path(td, "pending_deletions.json").write_text(json.dumps({"schema": 1, "entries": {
                "/library/movies/A/A.mkv": {"title": "Movie A", "marked_at": time.time()},
                "/library/movies/B/B.mkv": {"title": "Movie B", "marked_at": time.time()},
            }}), encoding="utf-8")

        def _save_over(payload_over, saved_over=None):
            _state["cfg"] = dict(BASE, OUTPUT_DIR=td, **(saved_over or {}))
            p = {"RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
                 "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
                 "USE_PLEX": False, "USE_JELLYFIN": False,
                 "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
            p.update(payload_over)
            r = client.post("/api/config", json=p, headers={"X-MediaReducer": "1"})
            return r.status_code, r.get_json()

        # Removing the saved cap while limits are satisfied → marks clear.
        A._deletion_limits_exceeded = lambda *a, **k: False
        _write_queue()
        code, body = _save_over({"MAX_LIBRARY_GB": None}, saved_over={"MAX_LIBRARY_GB": 50})
        check("threshold-removing save clears the orphaned queue",
              code == 200 and body.get("pending_cleared") == 2 and A.pending_count() == 0)

        # An unrelated save (thresholds unchanged) keeps the marks even while satisfied.
        _write_queue()
        code, body = _save_over({})
        check("unrelated save never clears the marks",
              code == 200 and body.get("pending_cleared") == 0 and A.pending_count() == 2)

        # A limit still breached on save → marks are preserved (a re-Simulate refreshes them).
        A._deletion_limits_exceeded = lambda *a, **k: True
        _write_queue()
        code, body = _save_over({"MAX_LIBRARY_GB": 50})
        check("save keeps marks while a limit is still breached",
              code == 200 and body.get("pending_cleared") == 0 and A.pending_count() == 2)
finally:
    A._deletion_limits_exceeded = _orig_limits
    A.disk_stats = _orig_disk
    A.library_stats = _orig_lib

# ── Plan currency: EVERY deletion-affecting key must match the stamp ─────────
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, OUTPUT_DIR=td, HEADROOM_GB=500, REDLINE_GB=None,
                         MAX_LIBRARY_GB=2000, MONITOR_DIRS=["/library/movies"],
                         GRACE_PERIOD_DAYS=30, SCORE_BALANCE=50,
                         PROTECTED_COLLECTIONS=["Keepers"])
    p = Path(td, "pending_deletions.json")
    entry = {"/library/movies/A/A.mkv": {"title": "Movie A", "marked_at": 0}}
    stamp = {k: _state["cfg"].get(k) for k in A._PLAN_CONFIG_KEYS}
    p.write_text(json.dumps({"schema": 1, "entries": entry, "plan_config": stamp,
                             "monitor_dirs": ["/library/movies"]}))
    check("plan current when the stamps match", A._pending_plan_current(_state["cfg"]))
    check("int/float spelling does not stale a plan",
          A._pending_plan_current(dict(_state["cfg"], HEADROOM_GB=500.0)))
    for key, value in (("HEADROOM_GB", 400), ("REDLINE_GB", 50), ("MAX_LIBRARY_GB", None),
                       ("GRACE_PERIOD_DAYS", 7), ("SCORE_BALANCE", 80),
                       ("SKIP_UNPLAYED_MOVIES", True), ("MAX_IMDB_RATING", 7.5),
                       ("NEAR_TIE_PTS", 5), ("MAX_STALENESS_MONTHS", 12),
                       ("PROTECTED_COLLECTIONS", ["Keepers", "Kids"])):
        check(f"changed {key} stales the plan",
              not A._pending_plan_current(dict(_state["cfg"], **{key: value})))
    check("collection reordering does not stale a plan",
          A._pending_plan_current(dict(_state["cfg"], PROTECTED_COLLECTIONS=["Keepers"])))
    check("changed monitored paths stale the plan",
          not A._pending_plan_current(dict(_state["cfg"], MONITOR_DIRS=["/library/other"])))
    p.write_text(json.dumps({"schema": 1, "entries": entry, "plan_config": stamp}))
    check("path-unstamped plan reads as stale", not A._pending_plan_current(_state["cfg"]))
    p.write_text(json.dumps({"schema": 1, "entries": entry,
                             "thresholds": {"HEADROOM_GB": 500},
                             "monitor_dirs": ["/library/movies"]}))
    check("old-format / partial stamp reads as stale", not A._pending_plan_current(_state["cfg"]))
    p.write_text(json.dumps({"schema": 1, "entries": {}, "plan_config": stamp,
                             "monitor_dirs": ["/library/movies"]}))
    check("empty plan reads as stale", not A._pending_plan_current(_state["cfg"]))
    p.unlink()
    check("missing plan reads as stale", not A._pending_plan_current(_state["cfg"]))

# ── Arming Live over breached limits requires a CURRENT Simulate plan ────────
# Healthy connections: an unhealthy probe force-pauses Live before the gate.
A._refresh_connection_health_cache = lambda cfg, probe=True: {
    "critical_ok": True, "tautulli_connected": True,
    "jellyfin_connected": True, "radarr_connected": False,
}
A._restart_schedule_clock = lambda: None
# Mirrors the real simulate_required contract (breached + no current plan)
# without depending on this sandbox's actual disk numbers.
A._space_threshold_state = lambda cfg, disk=None, library_gb=None: {
    "ok_for_live": True, "live_tooltip": "", "safety_blocked": False,
    "simulate_required": (A._deletion_limits_exceeded(cfg, disk, library_gb)
                          and not A._pending_plan_current(cfg)),
}
A._deletion_limits_exceeded = lambda *a, **k: True
A.library_stats = lambda: {"library_gb": 100.0}
A.cached_disk_stats = lambda s=None: {"total_gb": 1000, "used_gb": 900, "free_gb": 100, "pct_used": 90}
A._pending_plan_current = lambda cfg: False
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("arming Live over limits without a current plan is refused", code == 400)
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 0})
check("delay 0 over limits also requires a plan (manual Live deletes now)", code == 400)
A._pending_plan_current = lambda cfg: True
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("arming Live with a current plan is allowed",
      code == 200 and cfg.get("RUN_MODE") == "headroom")
A._pending_plan_current = lambda cfg: False
A._deletion_limits_exceeded = lambda *a, **k: False
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("within limits arms without a plan", code == 200)

# ── Startup burn of the daily window ─────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, OUTPUT_DIR=td)
    today = time.strftime("%Y-%m-%d")
    # No cache at all: burn creates one with today stamped.
    A.burn_daily_window_on_startup()
    cache = json.loads(Path(td, "cache.json").read_text())
    check("burn stamps today into a missing cache", cache.get("last_cleanup_date") == today)
    # Existing cache with other keys: stamped without losing them.
    Path(td, "cache.json").write_text(json.dumps({"movies": {"x": 1}, "last_cleanup_date": "2020-01-01"}))
    A.burn_daily_window_on_startup()
    cache = json.loads(Path(td, "cache.json").read_text())
    check("burn re-stamps a stale date and keeps other keys",
          cache.get("last_cleanup_date") == today and cache.get("movies") == {"x": 1})

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
