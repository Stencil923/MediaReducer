"""Simulate the client doRefresh() sequence after moving the dial to
watch-history without a prior Save: POST /api/score-config (balance 0),
then POST /api/score-sample/refresh. Assert no IMDb download happens and
exactly one build runs."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

state = {"cfg": {"SCORE_BALANCE": 50, "MAX_IMDB_RATING": None}}  # SAVED at balance 50
dl = {"download": 0, "builds": 0}

A.load_config = lambda: dict(state["cfg"])
def _save(cfg):
    state["cfg"] = dict(cfg); return True
A.save_config = _save
A._invalid_config_response = lambda: None
A._score_page_config = lambda cfg: {}
A._has_monitored_dirs = lambda cfg=None: True
A._summary_active = False; A._run_active = False
A._ensure_sample_imdb_dataset = lambda: (dl.__setitem__("download", dl["download"]+1) or True)
def _subproc(cp, t, timeout=600):
    dl["builds"] += 1
    time.sleep(0.4)   # a real sample build takes seconds; keep the dedup window open
    return True, "ok", None
A._run_sample_pool_subprocess = _subproc

client = A.app.test_client()
HDR = {"X-MediaReducer": "1"}

def wait_idle():
    for _ in range(60):
        if not A._sample_pool_active: return
        time.sleep(0.05)

# Reset sample-pool state
A._sample_pool_last = {"ok": None, "message": "", "error_code": None, "failed_at": None}
A._sample_pool_active = False
dl["download"] = 0; dl["builds"] = 0

# 1. Client saves the moved dial (balance 50 -> 0) — crossing out of IMDb-needed
r1 = client.post("/api/score-config", json={"SCORE_BALANCE": 0}, headers=HDR)
# 2. Client immediately refreshes the sample batch
r2 = client.post("/api/score-sample/refresh", json={"n": 10}, headers=HDR)
wait_idle()

ok = True
def check(name, cond):
    global ok; print(("PASS " if cond else "FAIL ")+name); ok = ok and cond

check("score-config 200", r1.status_code == 200)
check("refresh 200", r2.status_code == 200)
check("saved balance is 0", state["cfg"]["SCORE_BALANCE"] == 0)
check("NO imdb download at watch-history", dl["download"] == 0)
check("exactly one build ran (deduped)", dl["builds"] == 1)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
