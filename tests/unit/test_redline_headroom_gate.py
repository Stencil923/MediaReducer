"""The Redline-below-Headroom ceiling on the dashboard (_space_threshold_state)
must only apply while Headroom is TICKED. Unticking Headroom retires that ceiling —
even when a Library Size Cap is ALSO armed (redline + cap), which is not a
"redline-only" config but still has no Headroom target to sit under. Regression for
the false "Redline must be lower than Headroom" error shown with Headroom off +
Redline + Cap."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# Neutralize the DB/plan-stamp parts — this exercises the hard-error validation only.
A._full_scan_overdue = lambda: False
A._pending_plan_current = lambda *a, **k: True
A._deletion_limits_exceeded = lambda *a, **k: False
A._simulate_evidence = lambda cfg: True
A._pending_raw = lambda: None

DISK = {"total_gb": 1000, "used_gb": 400, "free_gb": 600, "pct_used": 40}

def tooltip(cfg):
    return A._space_threshold_state(cfg, DISK, library_gb=100).get("cleanup_tooltip", "")

CEIL = "lower than Headroom"

# 1. THE BUG: Headroom off (redline-only flag) + Redline + Library Size Cap.
cfg = {"REDLINE_ONLY_MODE": True, "HEADROOM_GB": 0, "REDLINE_GB": 500,
       "MAX_LIBRARY_GB": 10000, "MAX_HEADROOM_PCT": 15}
check("headroom off + redline + cap: no false 'Redline must be lower than Headroom'",
      CEIL not in tooltip(cfg))

# 2. Headroom off + Redline only (no cap): also no ceiling.
cfg2 = dict(cfg, MAX_LIBRARY_GB=None)
check("headroom off + redline only: no ceiling error", CEIL not in tooltip(cfg2))

# 3. Headroom TICKED with Redline at/above it: the ceiling error DOES still fire.
cfg3 = {"REDLINE_ONLY_MODE": False, "HEADROOM_GB": 100, "REDLINE_GB": 200,
        "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15}
check("headroom ticked + redline above it: ceiling error still fires", CEIL in tooltip(cfg3))

# 4. Headroom TICKED with Redline below it: valid.
cfg4 = dict(cfg3, HEADROOM_GB=300, REDLINE_GB=200)
check("headroom ticked + redline below it: no ceiling error", CEIL not in tooltip(cfg4))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
