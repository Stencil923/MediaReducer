"""The sample build freezes the background clock like runs/summaries do,
the tick defers while it's active, and the clock restarts afterwards."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

calls = {"pause": 0, "reschedule": 0, "summary": 0, "run": 0}
class FakeSched:
    def pause_job(self, jid): calls["pause"] += 1
    def reschedule_job(self, jid, **kw): calls["reschedule"] += 1
A.scheduler = FakeSched()

A.load_config = lambda: {"SCORE_BALANCE": 0, "MAX_IMDB_RATING": None,
                         "MONITOR_DIRS": ["/library/movies"], "RUN_MODE": "paused"}
A._has_monitored_dirs = lambda cfg=None: True
A._run_active = False
A._summary_active = False
A._sample_pool_last = {"ok": None, "message": "", "error_code": None, "failed_at": None}
A._sample_pool_active = False

def slow_build(cp, t, timeout=600):
    time.sleep(0.5)
    return True, "ok", None
A._run_sample_pool_subprocess = slow_build

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

started, _ = A.refresh_sample_pool(manual=True)
check("refresh started", started)
time.sleep(0.15)
check("clock paused while building",
      calls["pause"] == 1 and calls["reschedule"] == 0 and A._sample_pool_active)

A.run_summary = lambda: (calls.__setitem__("summary", calls["summary"] + 1) or (True, ""))
A.run_script = lambda **kw: (calls.__setitem__("run", calls["run"] + 1) or (True, ""))
A._scheduled_tick()
check("tick defers during sample build", calls["summary"] == 0 and calls["run"] == 0)

for _ in range(30):
    if not A._sample_pool_active:
        break
    time.sleep(0.1)
time.sleep(0.1)
check("clock restarted after build", calls["reschedule"] == 1 and not A._sample_pool_active)

A._scheduled_tick()
check("tick works again after build", calls["summary"] == 1)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
