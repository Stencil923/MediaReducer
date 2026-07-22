"""Dashboard run buttons: Live ghosts when every space limit is satisfied
(Simulate stays available in every mode — it maintains the standing marked &
eligible queue), unknown values fail OPEN, and a real threshold problem never
gets masked."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

A._connection_health_for_ui = lambda cfg: {"critical_ok": True, "required_tooltip": ""}
A._has_monitored_dirs = lambda cfg=None: True
A._space_threshold_state = lambda cfg, disk=None: {
    "ok_for_simulate": True, "ok_for_cleanup": True,
    "simulate_tooltip": "", "cleanup_tooltip": "", "has_library_cap": False}
A.library_stats = lambda: {"library_gb": 500}

cfg = {"HEADROOM_GB": 100, "REDLINE_GB": None, "MAX_LIBRARY_GB": None}
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# Satisfied: total 1000, used 800 -> limit 900, under it.
st = A._cleanup_button_state(cfg, {"used_gb": 800.0, "total_gb": 1000.0, "free_gb": 200.0})
check("satisfied keeps simulate enabled (queue upkeep)", st["simulate_disabled"] is False)
check("satisfied ghosts live", st["cleanup_disabled"] is True)
check("satisfied tooltip says why on Live", "satisfied" in st["cleanup_tooltip"])
check("space_satisfied flag", st["space_satisfied"] is True)
check("summary stays enabled", st["summary_disabled"] is False)

# Breached: used 950 > 900 limit.
st = A._cleanup_button_state(cfg, {"used_gb": 950.0, "total_gb": 1000.0, "free_gb": 50.0})
check("breached enables simulate", st["simulate_disabled"] is False)
check("breached enables live", st["cleanup_disabled"] is False)
check("no stale tooltip when enabled", st["simulate_tooltip"] == "")

# Unknown disk: fail open (the engine is the authority).
st = A._cleanup_button_state(cfg, None)
check("unknown disk fails open", st["simulate_disabled"] is False)

# A real threshold problem keeps its own tooltip.
A._space_threshold_state = lambda cfg, disk=None: {
    "ok_for_simulate": True, "ok_for_cleanup": False,
    "simulate_tooltip": "",
    "cleanup_tooltip": "Set a Headroom target, Redline, or Library Size Cap to enable Automatic Cleanup.",
    "has_library_cap": False}
st = A._cleanup_button_state(cfg, {"used_gb": 800.0, "total_gb": 1000.0, "free_gb": 200.0})
check("threshold tooltip wins for live", "Headroom target" in st["cleanup_tooltip"])
check("simulate stays enabled alongside a live threshold problem", st["simulate_disabled"] is False)

# Breached without a plan for the CURRENT thresholds: Live ghosts (a manual
# Cleanup deletes immediately), Simulate stays available to build the plan.
A._space_threshold_state = lambda cfg, disk=None: {
    "ok_for_simulate": True, "ok_for_cleanup": True, "simulate_required": True,
    "simulate_required_message": "Over space limits — run Simulate to review the deletion plan first.",
    "simulate_tooltip": "", "cleanup_tooltip": "", "has_library_cap": False}
st = A._cleanup_button_state(cfg, {"used_gb": 950.0, "total_gb": 1000.0, "free_gb": 50.0})
check("stale plan ghosts live", st["cleanup_disabled"] is True)
check("stale plan keeps simulate enabled", st["simulate_disabled"] is False)
check("stale plan tooltip names Simulate", "Simulate" in st["cleanup_tooltip"])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
