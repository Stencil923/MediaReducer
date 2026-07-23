"""Startup store preservation + the "full scan within a day" policy.

The store is PRESERVED across a restart (an unchanged, recent plan stays usable
with no re-scan). Nothing is wiped on startup — whether the saved plan can still
be trusted is decided LIVE by the space-threshold gate:

  • a full library scan must have completed within the last day
    (_full_scan_overdue, keyed off the snapshot's built_at) — the automatic daily
    Cleanup normally keeps this satisfied; if it lapses, Cleanup + arming ghost
    until a manual Simulate;
  • monitored-path / scoring / threshold changes stale the plan stamp;
  • protected-collection changes and Radarr on/off do NOT — they're honored from
    the standing cache (the 15-min upkeep re-fetches protection; deletes re-verify).

Startup wipes nothing; deletion history (deleted.log) and logs/ are kept.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate
_OUT = tempfile.mkdtemp(prefix="mr-startup.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

_lib = Path(_OUT, "library", "movies")
_lib.mkdir(parents=True, exist_ok=True)
_paths = [str(_lib / f"Movie {n}.mkv") for n in ("A", "B")]
for p in _paths:
    Path(p).write_bytes(b"\0")

CFG = {"OUTPUT_DIR": _OUT, "MONITOR_DIRS": [str(_lib)]}
A.load_config = lambda: dict(CFG)
_NDIRS = A._normalized_monitor_dirs(CFG)
A.disk_stats = lambda: {"total_gb": 1000, "used_gb": 100, "free_gb": 900, "pct_used": 10}
A.library_stats = lambda: {"library_gb": 100.0, "updated_at": time.time()}


def _seed(built_at):
    """A preserved store: a snapshot built at `built_at` + a stamped current plan."""
    _dbstate.seed(A.db_path(), {
        "code_checksum": "x",
        "dashboard_stats": {"updated_at": time.time()},
        "library_snapshot": {"built_at": int(built_at), "monitor_dirs": _NDIRS,
                             "movies": [{"path": p, "title": Path(p).stem} for p in _paths]},
        "pending": {"schema": 1, "monitor_dirs": _NDIRS,
                    "plan_config": {k: CFG.get(k) for k in A._PLAN_CONFIG_KEYS},
                    "entries": {p: {"title": Path(p).stem, "marked_at": None} for p in _paths}},
    })


def _threshold(**over):
    return A._space_threshold_state(dict(CFG, HEADROOM_GB=100, REDLINE_GB=None,
                                         MAX_HEADROOM_PCT=15, **over))


now = time.time()

# ── A recent, unchanged store is PRESERVED, no re-Simulate ────────────────────
deleted = Path(_OUT, "deleted.log"); deleted.write_text("kept history\n")
_seed(now - 3600)   # scanned an hour ago
A.validate_store_on_startup()
check("the store is preserved across a restart (not wiped)", A.db_path().exists())
check("deletion history is kept", deleted.exists())
check("a recent full scan needs no re-Simulate", _threshold().get("simulate_required") is False)

# ── A scan a day old is still fine (48h limit gives a full day of slack) ──────
_seed(now - 30 * 3600)
check("a 30h-old scan is NOT overdue (48h limit)", A._full_scan_overdue() is False)

# ── A full scan older than two days → locked until a Simulate ─────────────────
_seed(now - 49 * 3600)
A.validate_store_on_startup()
check("the store is still preserved when the scan is stale", A.db_path().exists())
ts = _threshold()
check("a scan older than two days forces simulate_required",
      ts.get("simulate_required") is True and "over two days" in ts.get("simulate_required_message"))
lb = A._cleanup_button_state(dict(CFG, HEADROOM_GB=100, REDLINE_GB=None, MAX_HEADROOM_PCT=15),
                             A.disk_stats())
check("the Debug Cleanup button ghosts while the scan is overdue", lb.get("debug_disabled") is True)

# A completed scan (fresh built_at) lifts the lock on its own — no flag to clear.
_seed(now - 60)
check("a fresh scan re-enables Cleanup + arming", _threshold().get("simulate_required") is False)

# ── Monitored-path change still requires a Simulate ──────────────────────────
_seed(now - 3600)
check("changing the monitored paths requires a Simulate",
      _threshold(MONITOR_DIRS=["/somewhere/else"]).get("simulate_required") is True)

# ── Protected-collection change does NOT require a Simulate ───────────────────
check("changing protected collections keeps the plan (no re-Simulate)",
      _threshold(PROTECTED_COLLECTIONS=["Newly Protected"]).get("simulate_required") is False)

# ── Paused mode keeps the store current with a once-a-day maintenance Simulate ─
# After the daily run time, if no full scan has happened since then, a paused
# schedule is "due" to run a Simulate (the scheduler tick launches it).
_run_epoch = A._todays_daily_run_epoch(dict(CFG, DAILY_RUN_TIME="00:01"))  # earlier today
_seed(_run_epoch - 3600)   # last scan predates today's run time
check("paused: a daily maintenance Simulate is due after the run time",
      A._paused_daily_scan_due(dict(CFG, DAILY_RUN_TIME="00:01")) is True)
_seed(_run_epoch + 60)     # already scanned since today's run time
check("paused: not due once a scan ran after the run time",
      A._paused_daily_scan_due(dict(CFG, DAILY_RUN_TIME="00:01")) is False)
_seed(now - 3600)
check("paused: not due before today's run time",
      A._paused_daily_scan_due(dict(CFG, DAILY_RUN_TIME="23:59")) is False)

# ── Fresh install (nothing scanned): a Simulate is required, no crash ─────────
_dbstate.reset(A.db_path())
check("paused: a never-scanned install is not auto-Simulated (onboarding stays manual)",
      A._paused_daily_scan_due(dict(CFG, DAILY_RUN_TIME="00:01")) is False)
A.validate_store_on_startup()
check("with nothing scanned, _full_scan_overdue is False (first-time path owns it)",
      A._full_scan_overdue() is False)
ts = A._space_threshold_state(dict(CFG, HEADROOM_GB=0, REDLINE_GB=100,
                                   REDLINE_ONLY_MODE=True, MAX_HEADROOM_PCT=15))
check("a first-time install still requires a Simulate", ts.get("simulate_required") is True)

# Idempotent / safe to re-run.
try:
    A.validate_store_on_startup()
    check("startup validation is safe to re-run", True)
except Exception as e:
    check(f"startup validation is safe to re-run (raised {e})", False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
