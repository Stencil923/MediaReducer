"""Deletion delay plumbing: whole-day validation on config saves, and the
marked-for-deletion queue composition the history modal displays."""
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import atexit
import shutil
import tempfile
# Private per-run OUTPUT_DIR: a fixed shared path leaks window/pending
# state between test files and suite runs (rest of the suite uses tempdirs).
_OUT_DIR = tempfile.mkdtemp(prefix="mr-test-out.")
atexit.register(shutil.rmtree, _OUT_DIR, True)
import app as A

_real_load_config = A.load_config  # capture before the fake replaces it

_state = {"cfg": {}}

def fake_load_config():
    return dict(_state["cfg"])
def fake_save_config(cfg, **k):
    _state["cfg"] = dict(cfg)
    return True

A.load_config = fake_load_config
A.save_config = fake_save_config
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

BASE = {
    "RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
    "USE_PLEX": False, "USE_JELLYFIN": False,
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
    "OUTPUT_DIR": _OUT_DIR,
}

def save(payload_over):
    _state["cfg"] = dict(BASE)
    p = {"RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
         "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
         "USE_PLEX": False, "USE_JELLYFIN": False,
         "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
    p.update(payload_over)
    r = client.post("/api/config", json=p, headers={"X-MediaReducer": "1"})
    return r.status_code, dict(_state["cfg"])

code, cfg = save({"DELETE_DELAY_DAYS": 7})
check("whole days accepted", code == 200 and cfg.get("DELETE_DELAY_DAYS") == 7)
code, cfg = save({"DELETE_DELAY_DAYS": 1})
check("minimum 1 accepted", code == 200 and cfg.get("DELETE_DELAY_DAYS") == 1)
code, cfg = save({"DELETE_DELAY_DAYS": None})
check("blank saves as the 1-day minimum", code == 200 and cfg.get("DELETE_DELAY_DAYS") == 1)
code, cfg = save({"DELETE_DELAY_DAYS": 0})
check("0 rejected (never same day)", code == 400)
code, cfg = save({"DELETE_DELAY_DAYS": 3.5})
check("decimals rejected", code == 400)
code, cfg = save({"DELETE_DELAY_DAYS": 400})
check("400 days rejected", code == 400)
code, cfg = save({"DELETE_DELAY_DAYS": -1})
check("negative rejected", code == 400)

check("file validator flags decimals",
      any(i["key"] == "DELETE_DELAY_DAYS" for i in A._config_file_issues({"DELETE_DELAY_DAYS": 2.5})))
# 0 has no valid meaning for a min-1 field, so a hand-edited 0 is flagged like any
# other out-of-range edit (the GUI never writes it); a real value passes.
check("file validator flags a hand-edited 0, accepts 30",
      any(i["key"] == "DELETE_DELAY_DAYS" for i in A._config_file_issues({"DELETE_DELAY_DAYS": 0}))
      and not A._config_file_issues({"DELETE_DELAY_DAYS": 30}))
check("engine floors an out-of-range 0 to the 1-day minimum (defensive)",
      A._delete_delay_days({"DELETE_DELAY_DAYS": 0}) == 1)

# A config.json holding a below-1 value locks the app out via the standard hand-edit
# guard, rather than being silently reinterpreted at load.
with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td, "config.json")
    cfg_path.write_text(json.dumps({"DELETE_DELAY_DAYS": 0}), encoding="utf-8")
    _orig_cfg_path = A.CONFIG_PATH
    A.CONFIG_PATH = cfg_path
    try:
        _real_load_config()
    finally:
        A.CONFIG_PATH = _orig_cfg_path
    check("a hand-edited 0 in config.json locks out",
          any(i["key"] == "DELETE_DELAY_DAYS" for i in A._CONFIG_FILE_ISSUES))

# ── Queue composition for the history modal ─────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, OUTPUT_DIR=td, DELETE_DELAY_DAYS=7)
    now = time.time()
    Path(td, "cache.json").write_text(json.dumps({"pending": {"schema": 1, "entries": {
        "/library/movies/A/A.mkv": {"title": "Movie A", "size_bytes": 2_000_000_000,
                                    "score": 1.5, "marked_at": now - 86400},        # 1 day ago
        "/library/movies/B/B.mkv": {"title": "Movie B", "size_bytes": 1_000_000_000,
                                    "score": 2.0, "marked_at": now - 9 * 86400},    # expired
        "/library/movies/C/C.mkv": {"title": "Movie C", "size_bytes": 500_000_000,
                                    "score": 3.0, "marked_at": now},                # just now
    }}}), encoding="utf-8")
    entries = A.pending_deletion_entries()
    check("queue count", A.pending_count() == 3 and len(entries) == 3)
    # Marked entries display SOONEST deletion first: B is expired (0 days), A has
    # 6 left, C (marked just now) has the full 7 — so the order is B, A, C.
    check("marked shown soonest-deletion first",
          [e["title"] for e in entries] == ["Movie B", "Movie A", "Movie C"])
    by_title = {e["title"]: e for e in entries}
    check("days remaining honors the current delay", by_title["Movie A"]["days_remaining"] == 6)
    check("expired mark reads as deletable now",
          by_title["Movie B"]["days_remaining"] == 0 and "deletable now" in by_title["Movie B"]["line"])
    check("pending mark carries its eligibility date",
          f"deletable from {by_title['Movie A']['delete_on']}" in by_title["Movie A"]["line"])
    # Waiting-mark ages feed the Config page's "lowering the delay deletes more"
    # warning: Movie C (marked now) age 0, Movie A (1 day ago) age 1; Movie B is
    # already ripe under the 7-day delay, so it is not waiting.
    check("forecast reports ages of still-waiting marks",
          A.pending_delete_forecast().get("waiting_ages") == [0, 1])
    # Shortening the delay moves pending deletions up.
    _state["cfg"]["DELETE_DELAY_DAYS"] = 2
    entries = {e["title"]: e for e in A.pending_deletion_entries()}
    check("shorter delay shrinks the countdown", entries["Movie A"]["days_remaining"] == 1)

# ── A save that CHANGES a threshold and satisfies every limit unschedules ────
# Removing/lowering a cap leaves delay clocks running for a breach that no
# longer exists — the save nulls them, but the queue itself stays (it is the
# standing eligible deletion order, exactly like the engine's satisfied-limits
# upkeep). An unrelated save must NOT touch the clocks: it may be judging
# "satisfied" off a stale cached library size.
_orig_limits = A._deletion_limits_exceeded
_orig_disk = A.disk_stats
_orig_lib = A.library_stats
A.disk_stats = lambda: {"total_gb": 1000, "used_gb": 100, "free_gb": 900, "pct_used": 10}
A.library_stats = lambda: {"library_gb": 100.0}
try:
    with tempfile.TemporaryDirectory() as td:
        def _write_queue():
            Path(td, "cache.json").write_text(json.dumps({"pending": {"schema": 1, "entries": {
                "/library/movies/A/A.mkv": {"title": "Movie A", "marked_at": time.time()},
                "/library/movies/B/B.mkv": {"title": "Movie B", "marked_at": time.time()},
            }}}), encoding="utf-8")

        def _save_over(payload_over, saved_over=None):
            _state["cfg"] = dict(BASE, OUTPUT_DIR=td, **(saved_over or {}))
            p = {"RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
                 "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
                 "USE_PLEX": False, "USE_JELLYFIN": False,
                 "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
            p.update(payload_over)
            r = client.post("/api/config", json=p, headers={"X-MediaReducer": "1"})
            return r.status_code, r.get_json()

        def _clocks():
            return [e.get("marked_at") for e in A._pending_raw().values()]

        # Removing the saved cap while limits are satisfied → clocks null, queue stays.
        A._deletion_limits_exceeded = lambda *a, **k: False
        _write_queue()
        code, body = _save_over({"MAX_LIBRARY_GB": None}, saved_over={"MAX_LIBRARY_GB": 50})
        check("threshold-removing save unschedules but keeps the queue",
              code == 200 and body.get("pending_unscheduled") == 2
              and A.pending_count() == 2 and all(c is None for c in _clocks()))

        # An unrelated save (thresholds unchanged) keeps the clocks even while satisfied.
        _write_queue()
        code, body = _save_over({})
        check("unrelated save never touches the clocks",
              code == 200 and body.get("pending_unscheduled") == 0
              and A.pending_count() == 2 and all(c is not None for c in _clocks()))

        # A limit still breached on save → clocks are preserved (a re-Simulate refreshes them).
        A._deletion_limits_exceeded = lambda *a, **k: True
        _write_queue()
        code, body = _save_over({"MAX_LIBRARY_GB": 50})
        check("save keeps clocks while a limit is still breached",
              code == 200 and body.get("pending_unscheduled") == 0
              and A.pending_count() == 2 and all(c is not None for c in _clocks()))
finally:
    A._deletion_limits_exceeded = _orig_limits
    A.disk_stats = _orig_disk
    A.library_stats = _orig_lib

# ── Plan currency: EVERY deletion-affecting key must match the stamp ─────────
# _pending_plan_current compares the stamp against the RAW config.json values
# (like the engine); these checks feed the "raw file" through a stub that
# mirrors whatever cfg dict each check passes, plus one explicit raw-vs-raw
# check at the end.
_orig_rscf = A._read_saved_config_file
_raw_file = {"value": None}   # None → the function falls back to the passed cfg
A._read_saved_config_file = lambda: _raw_file["value"]
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, OUTPUT_DIR=td, HEADROOM_GB=500, REDLINE_GB=None,
                         MAX_LIBRARY_GB=2000, MONITOR_DIRS=["/library/movies"],
                         GRACE_PERIOD_DAYS=30, SCORE_BALANCE=50,
                         PROTECTED_COLLECTIONS=["Keepers"])
    p = Path(td, "cache.json")
    entry = {"/library/movies/A/A.mkv": {"title": "Movie A", "marked_at": 0}}
    stamp = {k: _state["cfg"].get(k) for k in A._PLAN_CONFIG_KEYS}
    p.write_text(json.dumps({"pending": {"schema": 1, "entries": entry, "plan_config": stamp,
                             "monitor_dirs": ["/library/movies"]}}))
    check("plan current when the stamps match", A._pending_plan_current(_state["cfg"]))
    check("int/float spelling does not stale a plan",
          A._pending_plan_current(dict(_state["cfg"], HEADROOM_GB=500.0)))
    for key, value in (("HEADROOM_GB", 400), ("REDLINE_GB", 50), ("MAX_LIBRARY_GB", None),
                       ("GRACE_PERIOD_DAYS", 7), ("SCORE_BALANCE", 80),
                       ("SKIP_UNPLAYED_MOVIES", True), ("MAX_IMDB_RATING", 7.5),
                       ("NEAR_TIE_PTS", 5), ("MAX_STALENESS_MONTHS", 12),
                       ("PROTECTED_COLLECTIONS", ["Keepers", "Kids"])):
        check(f"changed {key} stales the plan",
              not A._pending_plan_current(dict(_state["cfg"], **{key: value})))
    check("collection reordering does not stale a plan",
          A._pending_plan_current(dict(_state["cfg"], PROTECTED_COLLECTIONS=["Keepers"])))
    check("changed monitored paths stale the plan",
          not A._pending_plan_current(dict(_state["cfg"], MONITOR_DIRS=["/library/other"])))
    p.write_text(json.dumps({"pending": {"schema": 1, "entries": entry, "plan_config": stamp}}))
    check("path-unstamped plan reads as stale", not A._pending_plan_current(_state["cfg"]))
    p.write_text(json.dumps({"pending": {"schema": 1, "entries": entry,
                             "thresholds": {"HEADROOM_GB": 500},
                             "monitor_dirs": ["/library/movies"]}}))
    check("old-format / partial stamp reads as stale", not A._pending_plan_current(_state["cfg"]))
    # A stamped EMPTY queue is a real plan — a completed Simulate that found
    # nothing eligible (all filtered/protected). It must read as current or the
    # user loops through "run Simulate" forever.
    p.write_text(json.dumps({"pending": {"schema": 1, "entries": {}, "plan_config": stamp,
                             "monitor_dirs": ["/library/movies"]}}))
    check("stamped empty plan reads as current", A._pending_plan_current(_state["cfg"]))
    # The candidate-config comparison (the save handler's view): the same stamp
    # must read stale against a cfg with different thresholds even though the
    # file on disk still matches it.
    check("candidate cfg with new thresholds stales the plan (arming gate)",
          not A._pending_plan_current(dict(_state["cfg"], HEADROOM_GB=999),
                                      use_saved_file=False))
    check("candidate cfg matching the stamp stays current",
          A._pending_plan_current(dict(_state["cfg"]), use_saved_file=False))
    # Hand-edited off-grid value: the raw file says 50.4, load_config's
    # normalization would round the passed cfg to 50 — the RAW comparison must
    # still match the stamp (else every fresh Simulate instantly reads stale).
    _raw_file["value"] = dict(_state["cfg"], SCORE_BALANCE=50.4)
    stamp_raw = {k: _raw_file["value"].get(k) for k in A._PLAN_CONFIG_KEYS}
    p.write_text(json.dumps({"pending": {"schema": 1, "entries": entry, "plan_config": stamp_raw,
                             "monitor_dirs": ["/library/movies"]}}))
    check("hand-edited off-grid value matches its own stamp (raw vs raw)",
          A._pending_plan_current(dict(_state["cfg"], SCORE_BALANCE=50)))
    _raw_file["value"] = None
    p.unlink()
    check("missing plan reads as stale", not A._pending_plan_current(_state["cfg"]))
A._read_saved_config_file = _orig_rscf

# ── Arming Live over breached limits requires a CURRENT Simulate plan ────────
# Healthy connections: an unhealthy probe force-pauses Live before the gate.
A._refresh_connection_health_cache = lambda cfg, probe=True: {
    "critical_ok": True, "tautulli_connected": True,
    "jellyfin_connected": True, "radarr_connected": False,
}
A._restart_schedule_clock = lambda: None
# Mirrors the real simulate_required contract — breached: a current stamped
# plan; within limits: proof any Simulate has run (plan or library snapshot) —
# without depending on this sandbox's actual disk numbers.
A._space_threshold_state = lambda cfg, disk=None, library_gb=None, **kw: {
    "ok_for_cleanup": True, "cleanup_tooltip": "", "safety_blocked": False,
    "simulate_required": (not A._pending_plan_current(cfg)
                          if A._deletion_limits_exceeded(cfg, disk, library_gb)
                          else not (A._pending_plan_current(cfg) or A._simulate_evidence())),
    "simulate_required_message": "Run Simulate first.",
}
A._deletion_limits_exceeded = lambda *a, **k: True
A.library_stats = lambda: {"library_gb": 100.0}
A.cached_disk_stats = lambda s=None: {"total_gb": 1000, "used_gb": 900, "free_gb": 100, "pct_used": 90}
A._pending_plan_current = lambda cfg, **k: False
A._simulate_evidence = lambda *a, **k: False
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("arming Automatic Cleanup over limits without a current plan is refused", code == 400)
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 0})
check("delay 0 over limits also requires a plan (manual Live deletes now)", code == 400)
A._pending_plan_current = lambda cfg, **k: True
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("arming Automatic Cleanup with a current plan is allowed",
      code == 200 and cfg.get("RUN_MODE") == "headroom")
# Over limits, the snapshot alone is NOT enough — the plan must be current.
A._pending_plan_current = lambda cfg, **k: False
A._simulate_evidence = lambda *a, **k: True
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("over limits the snapshot alone does not arm Live", code == 400)
# Within limits arming still requires PROOF a Simulate has run: the library
# snapshot (any completed scan) counts; nothing at all does not.
A._deletion_limits_exceeded = lambda *a, **k: False
A._simulate_evidence = lambda *a, **k: False
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("within limits arming without any Simulate proof is refused", code == 400)
A._simulate_evidence = lambda *a, **k: True
code, cfg = save({"RUN_MODE": "headroom", "DELETE_DELAY_DAYS": 7})
check("within limits the library snapshot proves a Simulate ran and arms", code == 200)

# ── Changing thresholds while ARMED force-pauses Live instead of rejecting ───
with tempfile.TemporaryDirectory() as td:
    # Arm through the handler itself so the saved config's connection fields
    # match the payload exactly — otherwise the API-change force-pause fires
    # first and masks the threshold pause under test.
    _state["cfg"] = dict(BASE, OUTPUT_DIR=td)
    # The full connection-field set the real form posts — a partial payload
    # reads as an API change and the API force-pause would mask this test.
    p0 = {"RUN_MODE": "headroom", "HEADROOM_GB": 500, "REDLINE_GB": None,
          "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
          "USE_PLEX": False, "USE_JELLYFIN": False,
          "TAUTULLI_URL": "", "TAUTULLI_API_KEY": "", "PLEX_URL": "", "PLEX_TOKEN": "",
          "JELLYFIN_URL": "", "JELLYFIN_API_KEY": "", "RADARR_URL": "", "RADARR_API_KEY": "",
          "RADARR_OVERSEERR_SECTION_ID": None,
          "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
    r = client.post("/api/config", json=p0, headers={"X-MediaReducer": "1"})
    assert r.status_code == 200 and _state["cfg"].get("RUN_MODE") == "headroom", "arming setup failed"
    _state["cfg"]["OUTPUT_DIR"] = td

    r = client.post("/api/config", json=dict(p0, HEADROOM_GB=400), headers={"X-MediaReducer": "1"})
    body = r.get_json()
    check("threshold change while armed saves and force-pauses",
          r.status_code == 200 and _state["cfg"].get("RUN_MODE") == "paused"
          and body.get("automatic_run_mode_paused") is True
          and "thresholds changed" in (body.get("automatic_run_mode_paused_reason") or ""))

    # Re-arm, then an unrelated save while armed must NOT pause anything.
    _state["cfg"] = dict(_state["cfg"], RUN_MODE="headroom", HEADROOM_GB=500, OUTPUT_DIR=td)
    r = client.post("/api/config", json=dict(p0, LOG_RETENTION_DAYS=14), headers={"X-MediaReducer": "1"})
    body = r.get_json()
    check("unrelated save while armed keeps Live armed",
          r.status_code == 200 and _state["cfg"].get("RUN_MODE") == "headroom"
          and not body.get("automatic_run_mode_paused"))

# ── Startup burn of the daily window ─────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, OUTPUT_DIR=td)
    today = time.strftime("%Y-%m-%d")
    # No cache at all: burn creates one with today stamped.
    A.burn_daily_window_on_startup()
    cache = json.loads(Path(td, "cache.json").read_text())
    check("burn stamps today into a missing cache", cache.get("last_cleanup_date") == today)
    # Existing cache with other keys: stamped without losing them.
    Path(td, "cache.json").write_text(json.dumps({"movies": {"x": 1}, "last_cleanup_date": "2020-01-01"}))
    A.burn_daily_window_on_startup()
    cache = json.loads(Path(td, "cache.json").read_text())
    check("burn re-stamps a stale date and keeps other keys",
          cache.get("last_cleanup_date") == today and cache.get("movies") == {"x": 1})

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
