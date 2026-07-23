"""Radarr health gates only REAL Cleanups, not dry runs. Radarr's forget happens
only when a real Cleanup deletes a movie, so a Simulate / Debug Cleanup (which never
touch Radarr) must not abort when Radarr is unreachable — while a real Cleanup still
fails closed (deleting with Radarr down would let it re-download the movie)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
E.emit_progress = lambda *a, **k: None
# Focus on the Radarr branch: no media servers, media-path check stubbed.
E.USE_PLEX = False
E.USE_JELLYFIN = False
E.RADARR_OVERSEERR_SECTION_ID = "1"
E.RADARR_URL = "http://radarr.local"
E.RADARR_API_KEY = "key"
E.verify_media_path_compatibility = lambda: None

def _radarr_down(*a, **k):
    raise RuntimeError("connection refused")

def aborts():
    try:
        E.verify_runtime_api_health()
        return False
    except SystemExit:
        return True

# ── Radarr unreachable ────────────────────────────────────────────────────────
E._radarr_json = _radarr_down
E.RUN_MODE = "headroom"
check("a real Cleanup fails closed when Radarr is down", aborts() is True)
E.RUN_MODE = "debug_sim"
check("a Simulate is NOT blocked by an unreachable Radarr", aborts() is False)
E.RUN_MODE = "debug_cleanup"
check("a Debug Cleanup is NOT blocked by an unreachable Radarr", aborts() is False)

# ── Radarr reachable → a real Cleanup passes the pre-flight ───────────────────
E._radarr_json = lambda *a, **k: {"version": "5"}
E.RUN_MODE = "headroom"
check("a real Cleanup passes the check when Radarr is reachable", aborts() is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
