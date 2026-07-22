"""The shared RUN CONTEXT header (log_run_context).

Every executable run — Simulate, Debug Cleanup, live Cleanup — opens its log with
ONE consistent, greppable block: the configured space mode, each target's armed value
and breach deficit, the selection rules, and which breached target set the run's
free-space goal. These logs are the primary diagnosis surface for headroom / redline /
library-cap / combo / redline-only, so this test pins the contract each mode must keep.
"""
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

_OUT = Path(tempfile.mkdtemp(prefix="mr-runctx."))
E.OUTPUT_DIR = _OUT
E.LOGFILE = _OUT / "lastrun.log"

def render(**ctx):
    """Run log_run_context in isolation and return its log text."""
    E.LOGFILE.write_text("", encoding="utf-8")
    E.log_run_context(**ctx)
    return E.LOGFILE.read_text(encoding="utf-8")

# Common selection defaults.
E.NEAR_TIE_PTS = 2.0
E.DELETE_DELAY_DAYS = 3

# ── Headroom over: goal is attributed to headroom ─────────────────────────────
E.HEADROOM_GB, E.REDLINE_GB, E.MAX_LIBRARY_GB, E.REDLINE_ONLY_MODE = 1000, 200, None, False
txt = render(run_mode="debug_sim", is_sim=True, used_gb=9500, free_gb=500, total_gb=10000,
             library_gb=8200, max_gb=9000, over_limit=True, redline_hit=False,
             library_cap_hit=False, cap_active=False, headroom_deficit_gb=500.0,
             redline_deficit_gb=0.0, library_deficit_gb=0.0, to_free_gb=500.0,
             trigger="scheduled daily")
check("headroom: banner present", "RUN CONTEXT [DRY RUN]" in txt)
check("headroom: names the configured space mode", "space mode: headroom" in txt)
check("headroom: shows the headroom deficit", "OVER by 500.0 GB" in txt)
check("headroom: redline reads OK (armed, not hit)", "redline:     OK" in txt)
check("headroom: library cap reads off (disarmed)", "library cap: off" in txt)
check("headroom: goal attributed to headroom", "Target to free: 500.0 GB (set by headroom)" in txt)

# ── Redline emergency while headroom is ALSO over: goal is redline's, not headroom's ─
E.HEADROOM_GB, E.REDLINE_GB, E.MAX_LIBRARY_GB, E.REDLINE_ONLY_MODE = 1000, 200, None, False
txt = render(run_mode="headroom", is_sim=False, used_gb=9850, free_gb=150, total_gb=10000,
             library_gb=8200, max_gb=9000, over_limit=True, redline_hit=True,
             library_cap_hit=False, cap_active=False, headroom_deficit_gb=850.0,
             redline_deficit_gb=50.0, library_deficit_gb=0.0, to_free_gb=50.0,
             trigger="REDLINE")
check("redline: live banner (not a dry run)", "RUN CONTEXT [CLEANUP]" in txt)
check("redline: headroom still shows OVER", "headroom:    OVER by 850.0 GB" in txt)
check("redline: shows HIT with the amount to the floor", "redline:     HIT" in txt and "need 50.0 GB" in txt)
check("redline: goal attributed to redline only (emergency restores the floor)",
      "Target to free: 50.0 GB (set by redline)" in txt)

# ── Combo headroom + library cap: bigger deficit wins the goal ─────────────────
E.HEADROOM_GB, E.REDLINE_GB, E.MAX_LIBRARY_GB, E.REDLINE_ONLY_MODE = 1000, 200, 8000, False
txt = render(run_mode="debug_cleanup", is_sim=False, used_gb=9200, free_gb=800, total_gb=10000,
             library_gb=8500, max_gb=9000, over_limit=True, redline_hit=False,
             library_cap_hit=True, cap_active=True, headroom_deficit_gb=200.0,
             redline_deficit_gb=0.0, library_deficit_gb=500.0, to_free_gb=500.0,
             trigger="HEADROOM + LIBRARY CAP")
check("combo: debug-cleanup banner", "RUN CONTEXT [DEBUG CLEANUP]" in txt)
check("combo: both breached targets shown", "headroom:    OVER by 200.0 GB" in txt and "library cap: OVER by 500.0 GB" in txt)
check("combo: goal attributed to the larger (library cap) deficit",
      "Target to free: 500.0 GB (set by library cap)" in txt)

# ── Redline-only, above the floor: standing preview, nothing to free ───────────
E.HEADROOM_GB, E.REDLINE_GB, E.MAX_LIBRARY_GB, E.REDLINE_ONLY_MODE = 0, 200, None, True
txt = render(run_mode="debug_sim", is_sim=True, used_gb=8000, free_gb=2000, total_gb=10000,
             library_gb=7000, max_gb=10000, over_limit=False, redline_hit=False,
             library_cap_hit=False, cap_active=False, headroom_deficit_gb=0.0,
             redline_deficit_gb=0.0, library_deficit_gb=0.0, to_free_gb=0.0,
             trigger="REDLINE ORDER PREVIEW")
check("redline-only: mode labelled redline-only", "space mode: redline-only" in txt)
check("redline-only: headroom target reads off (redline-only)", "headroom off (redline-only)" in txt)
check("redline-only: within limits, nothing to free", "within all active limits" in txt)

# ── Selection line reflects file-size optimization on/off ─────────────────────
E.HEADROOM_GB, E.REDLINE_GB, E.MAX_LIBRARY_GB, E.REDLINE_ONLY_MODE = 1000, None, None, False
E.NEAR_TIE_PTS = None
txt = render(run_mode="debug_sim", is_sim=True, used_gb=9500, free_gb=500, total_gb=10000,
             library_gb=None, max_gb=9000, over_limit=True, redline_hit=False,
             library_cap_hit=False, cap_active=False, headroom_deficit_gb=500.0,
             redline_deficit_gb=0.0, library_deficit_gb=0.0, to_free_gb=500.0,
             trigger="scheduled daily")
check("selection: file-size optimization reads off when NEAR_TIE_PTS is unset",
      "file-size optimization off" in txt)
check("state: an unreadable library size is reported, not crashed",
      "library unavailable" in txt)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
