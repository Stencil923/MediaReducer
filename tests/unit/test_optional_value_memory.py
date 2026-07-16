"""Disabled optional fields keep their last entered value (the _<key>_LAST
memory), headroom saves blank as 0, and zero is rejected for the fields whose
off switch is "disable" (Redline, Library cap, Max IMDb rating, staleness,
file-size optimization)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

_state = {"cfg": {}}

def fake_load_config():
    return dict(_state["cfg"])
def fake_save_config(cfg, **k):
    _state["cfg"] = dict(cfg)
    return True

A.load_config = fake_load_config
A.save_config = fake_save_config
A.refresh_sample_pool = lambda *a, **k: (True, "ok")
A.run_summary = lambda *a, **k: (False, "skip")
A._invalid_config_response = lambda: None
A._refresh_connection_health_cache = lambda cfg, probe=True: {
    "critical_ok": False, "tautulli_connected": False,
    "jellyfin_connected": False, "radarr_connected": False,
}

client = A.app.test_client()

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

BASE_SAVED = {
    "RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": 200,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
    "USE_PLEX": False, "USE_JELLYFIN": False,
    "IMDB_RATINGS_URL": "https://example.test/ratings.tsv.gz",
    "OUTPUT_DIR": "/tmp/mr-test-out",
}

def base_payload(**over):
    p = {
        "RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": 200,
        "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
        "USE_PLEX": False, "USE_JELLYFIN": False,
        "IMDB_RATINGS_URL": "https://example.test/ratings.tsv.gz",
    }
    p.update(over)
    return p

def save_cfg(saved, payload):
    _state["cfg"] = dict(saved)
    r = client.post("/api/config", json=payload, headers={"X-MediaReducer": "1"})
    return r.status_code, (r.get_json() or {}), dict(_state["cfg"])

# ── /api/config: Redline / Library cap memory ────────────────────────────────

# Disabling redline with the field's text posted keeps that text as memory.
code, body, cfg = save_cfg(BASE_SAVED, base_payload(REDLINE_GB=None, _REDLINE_GB_LAST=300))
check("disable redline keeps posted last value",
      code == 200 and cfg.get("REDLINE_GB") is None and cfg.get("_REDLINE_GB_LAST") == 300)

# Disabling without posting the text falls back to the value being disabled.
code, body, cfg = save_cfg(BASE_SAVED, base_payload(REDLINE_GB=None))
check("disable redline falls back to the disabled value",
      code == 200 and cfg.get("_REDLINE_GB_LAST") == 200)

# The memory rides along across later disabled saves (form omits underscore keys).
saved = dict(BASE_SAVED, REDLINE_GB=None, _REDLINE_GB_LAST=300)
code, body, cfg = save_cfg(saved, base_payload(REDLINE_GB=None))
check("redline memory carried while disabled",
      code == 200 and cfg.get("_REDLINE_GB_LAST") == 300)

# Saving an enabled redline clears the memory.
code, body, cfg = save_cfg(saved, base_payload(REDLINE_GB=250))
check("enabling redline clears memory",
      code == 200 and cfg.get("REDLINE_GB") == 250 and "_REDLINE_GB_LAST" not in cfg)

# Same for the Library Size Cap.
saved = dict(BASE_SAVED, MAX_LIBRARY_GB=15000)
code, body, cfg = save_cfg(saved, base_payload(MAX_LIBRARY_GB=None, _MAX_LIBRARY_GB_LAST=15000))
check("disable cap keeps memory",
      code == 200 and cfg.get("MAX_LIBRARY_GB") is None and cfg.get("_MAX_LIBRARY_GB_LAST") == 15000)
saved = dict(BASE_SAVED, MAX_LIBRARY_GB=None, _MAX_LIBRARY_GB_LAST=15000)
code, body, cfg = save_cfg(saved, base_payload(MAX_LIBRARY_GB=16000))
check("enabling cap clears memory",
      code == 200 and cfg.get("MAX_LIBRARY_GB") == 16000 and "_MAX_LIBRARY_GB_LAST" not in cfg)

# Garbage never lands in the memory key (it bypasses the file validator).
code, body, cfg = save_cfg(BASE_SAVED, base_payload(REDLINE_GB=None, _REDLINE_GB_LAST="junk"))
check("junk last value falls back to the disabled value",
      code == 200 and cfg.get("_REDLINE_GB_LAST") == 200)

# ── /api/config: headroom blank = 0, silently ────────────────────────────────

code, body, cfg = save_cfg(BASE_SAVED, base_payload(HEADROOM_GB=None, REDLINE_GB=None))
check("blank headroom saves as 0", code == 200 and cfg.get("HEADROOM_GB") == 0)
code, body, cfg = save_cfg(BASE_SAVED, base_payload(HEADROOM_GB="abc"))
check("non-numeric headroom rejected", code == 400)
# Redline can't sit above a 0 headroom, so blank headroom + redline errors.
code, body, cfg = save_cfg(BASE_SAVED, base_payload(HEADROOM_GB=None, REDLINE_GB=200))
check("blank headroom with redline enabled rejected", code == 400)

# ── /api/config: score-field memory carried through config saves ─────────────

saved = dict(BASE_SAVED, MAX_IMDB_RATING=None, _MAX_IMDB_RATING_LAST=7.5,
             NEAR_TIE_PTS=None, _NEAR_TIE_PTS_LAST=2.5)
code, body, cfg = save_cfg(saved, base_payload())
check("explorer memory keys survive a config save",
      code == 200 and cfg.get("_MAX_IMDB_RATING_LAST") == 7.5
      and cfg.get("_NEAR_TIE_PTS_LAST") == 2.5)

# ── /api/score-config: zero rejected, memory kept ────────────────────────────

def save_score(saved, payload):
    _state["cfg"] = dict(saved)
    r = client.post("/api/score-config", json=payload, headers={"X-MediaReducer": "1"})
    return r.status_code, (r.get_json() or {}), dict(_state["cfg"])

SCORE_SAVED = dict(BASE_SAVED, SCORE_BALANCE=50, MAX_IMDB_RATING=7.5, NEAR_TIE_PTS=2,
                   MAX_STALENESS_MONTHS=36, GRACE_PERIOD_DAYS=0)

code, body, cfg = save_score(SCORE_SAVED, {"SCORE_BALANCE": 50, "MAX_IMDB_RATING": 0})
check("rating 0 rejected", code == 400)
code, body, cfg = save_score(SCORE_SAVED, {"SCORE_BALANCE": 50, "NEAR_TIE_PTS": 0})
check("tie window 0 rejected", code == 400)
code, body, cfg = save_score(SCORE_SAVED, {"SCORE_BALANCE": 50, "MAX_STALENESS_MONTHS": 0})
check("staleness 0 rejected", code == 400)
code, body, cfg = save_score(SCORE_SAVED, {"SCORE_BALANCE": 50, "MAX_STALENESS_MONTHS": 121})
check("staleness 121 rejected", code == 400)

# Disabling the cutoff keeps the field text as memory and reports it back.
code, body, cfg = save_score(SCORE_SAVED, {"SCORE_BALANCE": 50, "MAX_IMDB_RATING": None,
                                           "_MAX_IMDB_RATING_LAST": 7.5})
check("disable cutoff keeps memory",
      code == 200 and cfg.get("MAX_IMDB_RATING") is None
      and cfg.get("_MAX_IMDB_RATING_LAST") == 7.5
      and (body.get("config") or {}).get("_MAX_IMDB_RATING_LAST") == 7.5)

# Falls back to the value being disabled when no text is posted.
code, body, cfg = save_score(SCORE_SAVED, {"SCORE_BALANCE": 50, "MAX_IMDB_RATING": None})
check("disable cutoff falls back to the disabled value",
      code == 200 and cfg.get("_MAX_IMDB_RATING_LAST") == 7.5)

# Re-enabling clears the memory.
saved = dict(SCORE_SAVED, MAX_IMDB_RATING=None, _MAX_IMDB_RATING_LAST=7.5)
code, body, cfg = save_score(saved, {"SCORE_BALANCE": 50, "MAX_IMDB_RATING": 6})
check("enabling cutoff clears memory",
      code == 200 and cfg.get("MAX_IMDB_RATING") == 6 and "_MAX_IMDB_RATING_LAST" not in cfg)

# Same for the file-size-optimization window.
code, body, cfg = save_score(SCORE_SAVED, {"SCORE_BALANCE": 50, "NEAR_TIE_PTS": None,
                                           "_NEAR_TIE_PTS_LAST": 3})
check("disable tie window keeps memory",
      code == 200 and cfg.get("NEAR_TIE_PTS") is None and cfg.get("_NEAR_TIE_PTS_LAST") == 3)
saved = dict(SCORE_SAVED, NEAR_TIE_PTS=None, _NEAR_TIE_PTS_LAST=3)
code, body, cfg = save_score(saved, {"SCORE_BALANCE": 50, "NEAR_TIE_PTS": 2})
check("enabling tie window clears memory",
      code == 200 and cfg.get("NEAR_TIE_PTS") == 2 and "_NEAR_TIE_PTS_LAST" not in cfg)

# ── validation rules ─────────────────────────────────────────────────────────

# MAX_IMDB_RATING: 0 must NOT trip the hand-edit lockout — a cutoff of 0 matches
# nothing, so it reads as disabled (the same as null).
check("file validator accepts rating 0 as disabled",
      not A._config_file_issues({"MAX_IMDB_RATING": 0}))
check("file validator accepts rating 7.5", not A._config_file_issues({"MAX_IMDB_RATING": 7.5}))
check("file validator flags rating 11",
      any(i["key"] == "MAX_IMDB_RATING" for i in A._config_file_issues({"MAX_IMDB_RATING": 11})))
check("clamp reads 0 as disabled", A._clamp_max_imdb_rating(0) is None)
check("clamp keeps a real cutoff", A._clamp_max_imdb_rating(7.5) == 7.5)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
