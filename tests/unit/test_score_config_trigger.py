"""Verify /api/score-config rebuilds the sample when the save crosses the
IMDb-needed line (SCORE_BALANCE / MAX_IMDB_RATING), and NOT otherwise."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

calls = {"refresh": 0}
_state = {"cfg": {}}

def fake_load_config():
    return dict(_state["cfg"])
def fake_save_config(cfg):
    _state["cfg"] = dict(cfg)
    return True
def fake_refresh(*a, **k):
    calls["refresh"] += 1
    return True, "ok"

A.load_config = fake_load_config
A.save_config = fake_save_config
A.refresh_sample_pool = fake_refresh
A._invalid_config_response = lambda: None          # connections considered valid
A._score_page_config = lambda cfg: {}

client = A.app.test_client()

def run(start_cfg, payload):
    _state["cfg"] = dict(start_cfg)
    calls["refresh"] = 0
    r = client.post("/api/score-config", json=payload, headers={"X-MediaReducer": "1"})
    return r.status_code, calls["refresh"], dict(_state["cfg"])

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# 1. Balance 0 -> 50 : crosses into IMDb-needed -> rebuild
code, n, cfg = run({"SCORE_BALANCE": 0, "MAX_IMDB_RATING": None}, {"SCORE_BALANCE": 50})
check("balance 0->50 rebuilds", code == 200 and n == 1)

# 2. Balance 50 -> 0 : crosses OUT of IMDb-needed -> rebuild (stale ratings)
code, n, cfg = run({"SCORE_BALANCE": 50, "MAX_IMDB_RATING": None}, {"SCORE_BALANCE": 0})
check("balance 50->0 rebuilds", code == 200 and n == 1)

# 3. Balance 30 -> 70 : both IMDb-needed, no crossing -> NO rebuild
code, n, cfg = run({"SCORE_BALANCE": 30, "MAX_IMDB_RATING": None}, {"SCORE_BALANCE": 70})
check("balance 30->70 no rebuild", code == 200 and n == 0)

# 4. Balance 0 with a NEW cutoff set : 0 stays but cutoff crosses -> rebuild
code, n, cfg = run({"SCORE_BALANCE": 0, "MAX_IMDB_RATING": None},
                   {"SCORE_BALANCE": 0, "MAX_IMDB_RATING": "6.0"})
check("cutoff none->6.0 at balance 0 rebuilds", code == 200 and n == 1)

# 5. Balance 0, unrelated field (grace) changes, still no IMDb -> NO rebuild
code, n, cfg = run({"SCORE_BALANCE": 0, "MAX_IMDB_RATING": None},
                   {"SCORE_BALANCE": 0, "GRACE_PERIOD_DAYS": 14})
check("unrelated change at balance 0 no rebuild", code == 200 and n == 0)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
