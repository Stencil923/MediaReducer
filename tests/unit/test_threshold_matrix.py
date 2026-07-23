"""Exhaustive space-threshold combination matrix: every (mode, headroom,
redline, cap) state is checked against ALL THREE validators — the /api/config
save handler, the hand-edit file validator, and the engine validator — which
must agree exactly on what is saveable. Valid states additionally check the
Live/Simulate gating direction. Guards the contract:

  mode=False: redline must sit STRICTLY below the headroom VALUE (ties and
              0 included — at-or-above needs the mode); cap free.
  mode=True:  headroom value must be 0, and a Redline floor and/or a Library Size
              Cap must be armed (either or both) to drive cleanup.
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import atexit
import shutil
import tempfile
_OUT_DIR = tempfile.mkdtemp(prefix="mr-test-out.")
atexit.register(shutil.rmtree, _OUT_DIR, True)
# Hermetic library root: point MEDIAREDUCER_LIBRARY at a temp dir with a "movies"
# subfolder (created BEFORE importing app/engine, which read the library root once
# at import). The save handler validates that monitored dirs exist on disk; the
# hardcoded DIRS "/library/movies" normalizes to <root>/movies, so the test no
# longer depends on a real /library mount.
_LIB_DIR = tempfile.mkdtemp(prefix="mr-test-lib.")
atexit.register(shutil.rmtree, _LIB_DIR, True)
(Path(_LIB_DIR) / "movies").mkdir(parents=True, exist_ok=True)
os.environ["MEDIAREDUCER_LIBRARY"] = _LIB_DIR
import app as A
import engine

_state = {"cfg": {}}
def fake_load_config():
    return dict(_state["cfg"])
def fake_save_config(cfg, **k):
    _state["cfg"] = dict(cfg)
    return True

A.load_config = fake_load_config
A.save_config = fake_save_config
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

DIRS = ["/library/movies"]
BASE_SAVED = {
    "RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": 200,
    "REDLINE_ONLY_MODE": False,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 60, "MONITOR_DIRS": DIRS,
    "USE_PLEX": False, "USE_JELLYFIN": False,
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
    "OUTPUT_DIR": _OUT_DIR,
}

def expected_valid(mode, h, r, c):
    """The single source of truth all three validators must reproduce."""
    if mode:
        # Headroom off: valid when its value is 0 AND something else drives cleanup
        # — a Redline floor and/or a Library Size Cap (either or both).
        return h == 0 and (r is not None or c is not None)
    return r is None or r < h

def api_status_for(mode, h, r, c):
    _state["cfg"] = dict(BASE_SAVED)
    payload = dict(BASE_SAVED)
    payload.pop("OUTPUT_DIR")
    payload.update({"HEADROOM_GB": h, "REDLINE_GB": r,
                    "REDLINE_ONLY_MODE": mode, "MAX_LIBRARY_GB": c})
    resp = client.post("/api/config", json=payload, headers={"X-MediaReducer": "1"})
    return resp.status_code

_eng_saved = (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE,
              engine.MAX_LIBRARY_GB, engine.MONITOR_DIRS, engine.CONFIG_ERRORS)
try:
    engine.MONITOR_DIRS = list(DIRS)
    engine.CONFIG_ERRORS = []
    for mode in (False, True):
        for h in (0, 500):
            for r in (None, 200, 500, 900):
                for c in (None, 5000):
                    label = f"mode={mode} H={h} R={r} C={c}"
                    want = expected_valid(mode, h, r, c)

                    save_ok = api_status_for(mode, h, r, c) == 200
                    file_ok = not A._config_file_issues(
                        {"MONITOR_DIRS": DIRS, "HEADROOM_GB": h, "REDLINE_GB": r,
                         "REDLINE_ONLY_MODE": mode, "MAX_LIBRARY_GB": c})
                    engine.HEADROOM_GB, engine.REDLINE_GB = h, r
                    engine.REDLINE_ONLY_MODE, engine.MAX_LIBRARY_GB = mode, c
                    errs, _t, _m = engine._space_threshold_errors()
                    eng_ok = errs == []

                    check(f"{label}: save={'ok' if save_ok else '400'} "
                          f"file={'ok' if file_ok else 'flag'} "
                          f"engine={'ok' if eng_ok else 'flag'} — expect "
                          f"{'valid' if want else 'invalid'}",
                          save_ok == want and file_ok == want and eng_ok == want)

                    # Mode detection only for the one true mode shape.
                    check(f"{label}: mode detection",
                          A._redline_only_mode_cfg(
                              {"HEADROOM_GB": h, "REDLINE_GB": r,
                               "REDLINE_ONLY_MODE": mode}) is (mode and r is not None))
finally:
    (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE,
     engine.MAX_LIBRARY_GB, engine.MONITOR_DIRS, engine.CONFIG_ERRORS) = _eng_saved

# ── Gating direction for each VALID state (60% safety cap on a 1 TB disk) ────
_disk = {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}
def gate(mode, h, r, c):
    cfg = dict(BASE_SAVED, HEADROOM_GB=h, REDLINE_GB=r,
               REDLINE_ONLY_MODE=mode, MAX_LIBRARY_GB=c)
    return A._space_threshold_state(cfg, _disk, library_gb=100.0)

st = gate(False, 0, None, None)
check("no-thresholds: Live blocked with setup message, Simulate open",
      st["ok_for_cleanup"] is False and "Set a Headroom target" in st["cleanup_tooltip"]
      and st["ok_for_simulate"] is True)
st = gate(False, 0, None, 5000)
check("cap-only: Live allowed", st["ok_for_cleanup"] is True and st["ok_for_simulate"] is True)
st = gate(False, 500, 200, None)
check("normal + redline: Live allowed", st["ok_for_cleanup"] is True)
st = gate(False, 500, None, 5000)
check("normal + cap: Live allowed", st["ok_for_cleanup"] is True)
st = gate(True, 0, 200, None)
check("redline-only: Live allowed (plan gate handled separately)",
      st["ok_for_cleanup"] is True)

# ── Onboarding (no dirs): the locked form's saved-through values stay valid ──
for mode, h, r, c in ((False, 0, None, None), (False, 500, 200, None)):
    issues = A._config_file_issues({"MONITOR_DIRS": [], "HEADROOM_GB": h,
                                    "REDLINE_GB": r, "REDLINE_ONLY_MODE": mode,
                                    "MAX_LIBRARY_GB": c})
    check(f"onboarding (no dirs) mode={mode} H={h} R={r}: no lockout", not issues)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
