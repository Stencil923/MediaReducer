"""Startup cache invalidation.

Files can change while the app is down, so every startup drops the cached library
snapshot/metadata (cache.json) and the standing marked & eligible queue
(pending_deletions.json). A fresh Simulate must rebuild them — which is also what
ghosts the manual and Debug Cleanup buttons (they replay the cached queue) until
that Simulate runs. The wiped plan no longer describes what's on disk, so the last
run's log is archived into logs/ (never lost) and the progress panel is reset to
"no runs yet". Deletion history (deleted.log) and the logs/ archive are kept.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-startup.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# Lay down a full set of state files, then invalidate.
cache = Path(_OUT, "cache.json");            cache.write_text('{"movies": [1, 2, 3]}')
pending = Path(_OUT, "pending_deletions.json"); pending.write_text('{"entries": {"/x": {}}}')
deleted = Path(_OUT, "deleted.log");         deleted.write_text("kept history\n")
lastrun = Path(_OUT, "lastrun.log");         lastrun.write_text("last run log\n")
progress = Path(_OUT, "progress.json");      progress.write_text('{"status": "done", "phase": "done"}')
logs = Path(_OUT, "logs")

A.invalidate_cache_on_startup()

check("startup drops the library cache (cache.json)", not cache.exists())
check("startup drops the marked & eligible queue (pending_deletions.json)", not pending.exists())
check("startup keeps deletion history (deleted.log)", deleted.exists())
# The last run no longer describes the wiped plan: its log moves out of the run panel
# into logs/ (preserved), and the progress panel resets to the stock "no runs yet".
check("startup moves lastrun.log out of the run panel", not lastrun.exists())
check("startup archives the last run's log into logs/",
      logs.is_dir() and any(f.read_text() == "last run log\n" for f in logs.glob("*.log")))
check("startup clears the progress panel to stock/unrun (progress.json gone)", not progress.exists())

# Idempotent / safe when the files are already gone (a brand-new install).
try:
    A.invalidate_cache_on_startup()
    check("invalidation is safe when nothing is cached yet", True)
except Exception as e:
    check(f"invalidation is safe when nothing is cached yet (raised {e})", False)

# With no pending queue, a Simulate is required — the gate the buttons read to ghost.
# (Redline-only: simulate_required is purely 'no current plan'.)
A.load_config = lambda: {"OUTPUT_DIR": _OUT, "REDLINE_ONLY_MODE": True,
                         "HEADROOM_GB": 0, "REDLINE_GB": 100, "MAX_HEADROOM_PCT": 15}
A.disk_stats = lambda: {"total_gb": 1000, "free_gb": 50}      # below the 100 GB floor
A.library_stats = lambda: {"library_gb": 500}
ts = A._space_threshold_state(A.load_config(), A.disk_stats())
check("after invalidation, simulate_required is True (buttons ghost until Simulate)",
      ts.get("simulate_required") is True)
lb = A._cleanup_button_state(A.load_config(), A.disk_stats())
check("the Debug Cleanup button is ghosted (debug_disabled) with no cached plan",
      lb.get("debug_disabled") is True)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
