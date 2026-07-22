"""Debug mode + Debug Cleanup.

Debug mode is a no-delete diagnostic state: it can only be on while Automatic
Run Mode is Paused (and blocks Live), and it turns the dashboard's Live button
into a yellow "Debug Cleanup" that fires the `debug_cleanup` engine mode. Guards
here:
  • config validation — DEBUG_MODE and Live are mutually exclusive
  • engine safety — debug_cleanup NEVER deletes: delete_candidate dry-runs and the
    redline fast path (which unlinks directly, no per-file gate) bails in any
    debug mode; debug_cleanup archives its log like a real run
  • /api/run gating — debug_cleanup needs Debug mode, a manual live headroom run is
    refused while Debug mode is on, and debug_cleanup uses the Simulate gate (so it
    runs past the 15% safety cap)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-debug.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import engine as E
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# ── Config validation: Debug mode ⇔ NOT Live ─────────────────────────────────
def _debug_issue(saved):
    return any(i.get("key") == "DEBUG_MODE" for i in A._config_file_issues(saved))

check("Debug mode + Live is rejected",
      _debug_issue({"DEBUG_MODE": True, "RUN_MODE": "headroom"}))
check("Debug mode + Paused is allowed",
      not _debug_issue({"DEBUG_MODE": True, "RUN_MODE": "paused"}))
check("Live without Debug mode is fine",
      not _debug_issue({"DEBUG_MODE": False, "RUN_MODE": "headroom"}))
check("Debug mode defaults to Paused (no RUN_MODE) — allowed",
      not _debug_issue({"DEBUG_MODE": True}))

# ── Engine: debug_cleanup is a run mode that can never delete ────────────────────
check("debug_cleanup is an executable, archivable run mode",
      "debug_cleanup" in E.EXECUTABLE_RUN_MODES and "debug_cleanup" in E.ARCHIVABLE_RUN_MODES)

E.log = lambda *a, **k: None
lib = Path(tempfile.mkdtemp(prefix="mr-debug-lib."))
mov = lib / "movies"; mov.mkdir(parents=True)
f = mov / "Film.mkv"; f.write_bytes(b"x" * 2048)
E.LIBRARY_ROOT = lib
E.MONITOR_DIRS = [str(mov)]
E._RESOLVED_MONITORED_ROOTS = None
E.RADARR_OVERSEERR_SECTION_ID = None

# The redline fast path unlinks directly (no per-file dry-run gate), so it MUST
# bail in any debug mode — otherwise debug_cleanup (not _is_sim) would delete for real.
E.RUN_MODE = "debug_cleanup"
check("redline fast path bails in debug mode (never reaches its raw unlink)",
      E._redline_fast_path(1_000_000) is False and f.exists())

# delete_candidate dry-runs on the debug_ prefix: it reports the would-delete and
# returns False WITHOUT unlinking.
did = E.delete_candidate({"path": f, "title": "Film", "file_size": 2048,
                          "retention_score": 1.0, "play_count": 0, "last_played": 0,
                          "imdb_rating": None, "imdb_votes": 0, "release_year": 2001,
                          "added_at": 0})
check("delete_candidate dry-runs in debug_cleanup (file untouched)",
      did is False and f.exists())

# ── Dashboard button: Debug Cleanup ignores the safety percentage ────────────
# The morphed yellow button binds to simulate_disabled / simulate_tooltip (NOT the
# live_* fields). A Redline/Headroom target over the 15% safety percentage blocks a
# REAL Cleanup, but must never disable Debug Cleanup or surface the safety reason
# on it. Uses the real _space_threshold_state (stubbed only below, for /api/run).
_safety_cfg = {"OUTPUT_DIR": _OUT, "DEBUG_MODE": True, "RUN_MODE": "paused",
               "REDLINE_ONLY_MODE": True, "HEADROOM_GB": 0, "REDLINE_GB": 500,
               "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [str(mov)]}
A.load_config = lambda: dict(_safety_cfg)
A.disk_stats = lambda: {"total_gb": 1000, "free_gb": 100}
A.library_stats = lambda: {"library_gb": 800}
A._connection_health_for_ui = lambda cfg=None: {"critical_ok": True}
A._has_monitored_dirs = lambda cfg=None: True
_lb = A._cleanup_button_state(_safety_cfg, A.disk_stats())
check("safety percentage over target blocks the real Cleanup",
      _lb["cleanup_disabled"] and "safety percentage" in _lb["cleanup_tooltip"])
check("Debug Cleanup button (Simulate gate) is NOT disabled by the safety percentage",
      not _lb["simulate_disabled"])
check("Debug Cleanup button surfaces no safety-percentage reason",
      "safety percentage" not in _lb["simulate_tooltip"])

# ── /api/run gating ──────────────────────────────────────────────────────────
_launched = {}
A.run_script = lambda mode_override=None, manual=False: (_launched.update(mode=mode_override) or (True, "started"))
A._refresh_connection_health_cache = lambda cfg=None, probe=True: {"critical_ok": True}
A.disk_stats = lambda: {}
A._has_monitored_dirs = lambda cfg=None: True
A.run_summary_sync = lambda timeout=600: (False, "skip", {})   # degrade-don't-block → straight to launch
A._run_active = False
# ok_for_simulate True but ok_for_cleanup False (over the 15% cap): debug_cleanup must
# still run (it uses the Simulate gate); a real headroom run would be blocked.
A._space_threshold_state = lambda cfg=None, disk=None, **k: {
    "ok_for_simulate": True, "ok_for_cleanup": False, "simulate_required": False,
    "cleanup_tooltip": "over the safety cap"}
client = A.app.test_client()
HDR = {"X-MediaReducer": "1"}

A.load_config = lambda: {"DEBUG_MODE": True, "OUTPUT_DIR": _OUT}
_launched.clear()
r = client.post("/api/run", json={"mode": "debug_cleanup"}, headers=HDR)
check("debug_cleanup runs past the 15% cap (uses the Simulate gate)",
      r.status_code == 200 and _launched.get("mode") == "debug_cleanup")
r = client.post("/api/run", json={"mode": "headroom"}, headers=HDR)
check("a real live run is refused while Debug mode is on", r.status_code == 400)

# Debug Cleanup replays the standing queue, so it needs a CURRENT plan: with
# simulate_required True (no Simulate yet, or settings moved) it is refused with a
# "run Simulate first" hint instead of launching a run that can only say so.
A.load_config = lambda: {"DEBUG_MODE": True, "OUTPUT_DIR": _OUT}
A._space_threshold_state = lambda cfg=None, disk=None, **k: {
    "ok_for_simulate": True, "ok_for_cleanup": False, "simulate_required": True,
    "simulate_required_message": "Run Simulate first."}
_launched.clear()
r = client.post("/api/run", json={"mode": "debug_cleanup"}, headers=HDR)
check("debug_cleanup is refused when no current plan exists (simulate_required)",
      r.status_code == 400 and _launched.get("mode") is None)
A._space_threshold_state = lambda cfg=None, disk=None, **k: {
    "ok_for_simulate": True, "ok_for_cleanup": False, "simulate_required": False,
    "cleanup_tooltip": "over the safety cap"}

A.load_config = lambda: {"DEBUG_MODE": False, "OUTPUT_DIR": _OUT}
r = client.post("/api/run", json={"mode": "debug_cleanup"}, headers=HDR)
check("debug_cleanup is refused when Debug mode is off", r.status_code == 400)
_launched.clear()
r = client.post("/api/run", json={"mode": "headroom"}, headers=HDR)
check("a real live run past the cap is still blocked (no Debug mode)",
      r.status_code == 400 and _launched.get("mode") is None)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
