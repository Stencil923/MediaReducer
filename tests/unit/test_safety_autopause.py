"""A cleanup tick whose Space Thresholds are no longer safe (e.g. the library
grew past the cap's safety floor) pauses Live with a reason instead of
silently skipping ticks forever."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

calls = {"clock": 0, "run": 0, "summary": 0}
_state = {"cfg": {}}
_threshold = {"ok_for_cleanup": True, "cleanup_tooltip": ""}

A.load_config = lambda: dict(_state["cfg"])
def _save(cfg, **k):
    _state["cfg"] = dict(cfg)
    return True
A.save_config = _save
A._refresh_connection_health_cache = lambda cfg, probe=True: {"critical_ok": True}
A.run_summary_sync = lambda: (True, "ok", {"library_gb": 100.0})
A.run_summary = lambda: (calls.__setitem__("summary", calls["summary"] + 1), (True, "ok"))[1]
A.cached_disk_stats = lambda s=None: {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}
A._space_threshold_state = lambda cfg, disk, library_gb=None: dict(_threshold)
A._deletion_limits_exceeded = lambda cfg, disk, lib: False
A.run_script = lambda *a, **k: calls.__setitem__("run", calls["run"] + 1)
A._restart_schedule_clock = lambda: calls.__setitem__("clock", calls["clock"] + 1)
A._run_active = False
A._summary_active = False
# The tick skips daily-only breaches once today's window is used; these cases
# test the launch flow itself, so hold the window open.
A._headroom_window_used_today = lambda: False

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# 1. Live + unsafe thresholds -> paused, with the tooltip as the reason,
#    and the schedule clock reset (a Live<->Paused transition).
_state["cfg"] = {"RUN_MODE": "headroom"}
_threshold.update({"ok_for_cleanup": False,
                   "cleanup_tooltip": "Library Size Cap would delete more than the safety percentage of the library."})
A._scheduled_tick()
check("unsafe tick pauses Live",
      _state["cfg"].get("RUN_MODE") == "paused")
check("reason recorded for the UI",
      "safety percentage" in _state["cfg"].get("_RUN_MODE_AUTOPAUSE_REASON", ""))
check("clock reset on the forced transition", calls["clock"] == 1)
check("no run was launched", calls["run"] == 0)

# 2. Already paused: the unsafe state changes nothing (no reason churn).
_state["cfg"] = {"RUN_MODE": "paused"}
before = dict(_state["cfg"])
A._scheduled_tick()
check("paused mode untouched", _state["cfg"] == before)

# 3. Live + safe thresholds + limits satisfied -> stays Live, nothing runs.
_state["cfg"] = {"RUN_MODE": "headroom"}
_threshold.update({"ok_for_cleanup": True, "cleanup_tooltip": ""})
A._scheduled_tick()
check("safe satisfied tick keeps Live armed",
      _state["cfg"].get("RUN_MODE") == "headroom" and calls["run"] == 0)

# 4. Live + safe thresholds + limits exceeded -> run launches, still Live.
A._deletion_limits_exceeded = lambda cfg, disk, lib: True
A._scheduled_tick()
check("safe breached tick launches the run", calls["run"] == 1
      and _state["cfg"].get("RUN_MODE") == "headroom")

# 5. Daily-only breach with today's window already used: the engine would only
#    say "waiting until tomorrow", so the tick skips the launch entirely.
A._headroom_window_used_today = lambda: True
A._scheduled_tick()
check("used window skips the pointless engine launch", calls["run"] == 1)

# 6. Same used window, but Redline is breached (free 500 <= 600): redline
#    ignores the window, so the run launches.
_state["cfg"] = {"RUN_MODE": "headroom", "REDLINE_GB": 600}
A._scheduled_tick()
check("redline breach launches despite the used window", calls["run"] == 2)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
