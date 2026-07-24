"""Saving a threshold change while Automatic Cleanup is on drops it to Monitor Only
ONLY when the change leaves the library actually over a limit (a run would delete).
Within all limits the reconcile rebuilds the plan in place and Automatic Cleanup
keeps running. Covers both "the change pushes it over" and "already over" via the
shared _deletion_limits_exceeded breach signal."""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-thr-pause-out.")
_LIB = tempfile.mkdtemp(prefix="mr-thr-pause-lib.")
atexit.register(shutil.rmtree, _OUT, True)
atexit.register(shutil.rmtree, _LIB, True)
(Path(_LIB) / "movies").mkdir(parents=True, exist_ok=True)
os.environ["MEDIAREDUCER_LIBRARY"] = _LIB
os.environ.setdefault("MEDIAREDUCER_CONFIG", str(Path(_OUT) / "config.json"))
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

_state = {"cfg": {}}
_breached = {"v": False}

A.load_config = lambda: dict(_state["cfg"])
def _save(cfg, **k):
    _state["cfg"] = dict(cfg)
    return True
A.save_config = _save
A._invalid_config_response = lambda: None
A._refresh_connection_health_cache = lambda cfg, probe=True: {
    "critical_ok": True, "tautulli_connected": True, "jellyfin_connected": True,
    "radarr_connected": False, "severity": "ok", "required_tooltip": ""}
A.run_summary = lambda *a, **k: (False, "skip")
A.run_summary_sync = lambda *a, **k: (False, "skip", {})
A._reconcile_after_save = lambda *a, **k: "none"
A.disk_stats = lambda: {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}
A.library_stats = lambda: {"library_gb": 100.0}
A._simulate_evidence = lambda cfg: True
A._space_threshold_state = lambda cfg, disk=None, library_gb=None, candidate_cfg=False, **k: {
    "ok_for_cleanup": True, "ok_for_simulate": True, "simulate_required": False,
    "cleanup_tooltip": "", "simulate_tooltip": "", "simulate_required_message": ""}
A._deletion_limits_exceeded = lambda cfg, disk, lib: _breached["v"]
# Isolate the THRESHOLD-change pause: neutralize the connection-change signature so
# that force-pause path never fires for this synthetic config (its normalized URL/key
# signature would otherwise read as "connection settings changed"). Monitoring fields
# are held identical, so monitoring_changed stays false on its own.
A._api_config_signature = lambda cfg: "const"

client = A.app.test_client()

DIRS = ["/library/movies"]
ARMED = {
    "RUN_MODE": "headroom", "HEADROOM_GB": 500, "REDLINE_GB": 200, "REDLINE_ONLY_MODE": False,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 60, "MONITOR_DIRS": DIRS,
    "USE_PLEX": False, "USE_JELLYFIN": True,
    "JELLYFIN_URL": "http://jf.test:8096", "JELLYFIN_API_KEY": "k",
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz", "OUTPUT_DIR": _OUT,
    "DELETE_DELAY_DAYS": 3,
}


def save(payload_updates, *, breached):
    _state["cfg"] = dict(ARMED)          # armed baseline before each save
    _breached["v"] = breached
    payload = dict(ARMED)
    payload.pop("OUTPUT_DIR")
    payload.update(payload_updates)
    resp = client.post("/api/config", json=payload, headers={"X-MediaReducer": "1"})
    return resp.status_code, _state["cfg"].get("RUN_MODE")


# 1. Threshold change, library WITHIN all limits → stays armed (plan reconciles in place).
code, mode = save({"HEADROOM_GB": 450}, breached=False)
check("within-limits threshold change saves ok", code == 200)
check("within-limits threshold change keeps Automatic Cleanup ON", mode == "headroom")

# 2. Threshold change that leaves the library OVER a limit → dropped to Monitor Only.
code, mode = save({"HEADROOM_GB": 450}, breached=True)
check("over-limit threshold change saves ok", code == 200)
check("over-limit threshold change drops to Monitor Only", mode == "paused")

# 3. A change that PUSHES it over (arming a cap below the library) → paused. The breach
#    signal is what matters, so model it with the cap set + breached True.
code, mode = save({"MAX_LIBRARY_GB": 50}, breached=True)
check("cap that pushes it over a limit → Monitor Only", code == 200 and mode == "paused")

# 4. A NON-threshold save (identical thresholds) does NOT pause, even while over a limit
#    — only a threshold change is gated on breach.
code, mode = save({}, breached=True)
check("non-threshold save stays armed even while over a limit", code == 200 and mode == "headroom")

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
