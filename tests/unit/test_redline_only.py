"""Redline-only mode (the explicit REDLINE_ONLY_MODE flag — the Headroom
checkbox unticked — with a Redline floor): validation rules,
the always-on Simulate/plan gate, the standing preview queue that never
auto-clears, and its "deletes when Redline hits" display."""
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A
import engine

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

RL_CFG = {"HEADROOM_GB": 0, "REDLINE_GB": 200, "REDLINE_ONLY_MODE": True, "MAX_LIBRARY_GB": None}
HR_CFG = {"HEADROOM_GB": 500, "REDLINE_GB": 200, "MAX_LIBRARY_GB": None}

# ── Mode detection ───────────────────────────────────────────────────────────
check("mode on: flag + redline", A._redline_only_mode_cfg(RL_CFG) is True)
check("mode off: ticked-0 with redline is NOT the mode",
      A._redline_only_mode_cfg({"HEADROOM_GB": 0, "REDLINE_GB": 200}) is False)
check("mode off: headroom set", A._redline_only_mode_cfg(HR_CFG) is False)
check("mode off: flag without redline",
      A._redline_only_mode_cfg({"HEADROOM_GB": 0, "REDLINE_GB": None, "REDLINE_ONLY_MODE": True}) is False)
check("mode off: headroom off WITH a cap is NOT redline-only (the cap drives the daily run)",
      A._redline_only_mode_cfg({"HEADROOM_GB": 0, "REDLINE_GB": 200,
                                "REDLINE_ONLY_MODE": True, "MAX_LIBRARY_GB": 5000}) is False)

# ── File validator cross-rules (hand edits) ──────────────────────────────────
# The rules only bind once monitored directories exist: 0/null with none is the
# ordinary onboarding state (the locked form posts it), never a lockout.
_DIRS = {"MONITOR_DIRS": ["/library/movies"]}
check("file validator: headroom 0 without redline is the valid no-thresholds state",
      not any(i["key"] == "HEADROOM_GB"
              for i in A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=None))))
check("file validator: headroom off WITH a cap and redline is now valid",
      not A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=200,
                                     REDLINE_ONLY_MODE=True, MAX_LIBRARY_GB=5000)))
check("file validator: headroom off with a cap and NO redline is valid",
      not A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=None,
                                     REDLINE_ONLY_MODE=True, MAX_LIBRARY_GB=5000)))
check("file validator: headroom off with neither redline nor cap locks out",
      any(i["key"] == "REDLINE_ONLY_MODE"
          for i in A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=None,
                                              REDLINE_ONLY_MODE=True, MAX_LIBRARY_GB=None))))
check("file validator: ticked-0 with a cap is a valid cap-only config",
      not A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=None, MAX_LIBRARY_GB=5000)))
check("file validator: clean redline-only passes",
      not A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=200,
                                     REDLINE_ONLY_MODE=True, MAX_LIBRARY_GB=None)))
check("file validator: onboarding 0/null with no dirs is NOT a lockout",
      not A._config_file_issues({"HEADROOM_GB": 0, "REDLINE_GB": None, "MONITOR_DIRS": []}))
check("file validator: redline above a 0 headroom needs the mode",
      any(i["key"] == "REDLINE_GB"
          for i in A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=900, MAX_LIBRARY_GB=None)))
      and not A._config_file_issues(dict(_DIRS, HEADROOM_GB=0, REDLINE_GB=900,
                                         REDLINE_ONLY_MODE=True, MAX_LIBRARY_GB=None)))
check("file validator: redline above an ENABLED headroom still locks out",
      any(i["key"] == "REDLINE_GB"
          for i in A._config_file_issues({"HEADROOM_GB": 500, "REDLINE_GB": 900})))

# ── Onboarding: the locked form's 0/null spelling must save (fresh install) ──
# A fresh install saves its first media connection BEFORE any monitored dir can
# exist; the locked Space Thresholds post HEADROOM 0 + REDLINE null. Rejecting
# that as "broken redline-only" made onboarding impossible.
_state["cfg"] = {"RUN_MODE": "paused", "HEADROOM_GB": 1000, "REDLINE_GB": None,
                 "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
                 "USE_PLEX": True, "USE_JELLYFIN": False,
                 "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
                 "OUTPUT_DIR": "/tmp/mr-rl-out"}
r = client.post("/api/config", json={
    "RUN_MODE": "paused", "HEADROOM_GB": 0, "REDLINE_GB": None,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
    "USE_PLEX": True, "USE_JELLYFIN": False,
    "TAUTULLI_URL": "http://tautulli.test", "TAUTULLI_API_KEY": "k",
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
}, headers={"X-MediaReducer": "1"})
check("onboarding save with no dirs accepts the 0/null spelling", r.status_code == 200)

# ── Plan gate: Simulate is ALWAYS required before Live in the mode ───────────
# Safety pct 60 keeps the 500 GB headroom and 200 GB redline of the test
# configs under the cap on the 1000 GB test disk (cap = 600 GB).
BASE = {"RUN_MODE": "paused", "MAX_HEADROOM_PCT": 60, "MONITOR_DIRS": ["/library/movies"],
        "USE_PLEX": False, "USE_JELLYFIN": False,
        "TAUTULLI_URL": "http://tautulli.test", "TAUTULLI_API_KEY": "test-key",
        "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz", "OUTPUT_DIR": "/tmp/mr-rl-out"}
_disk = {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}

_orig_limits = A._deletion_limits_exceeded
_orig_plan = A._pending_plan_current
_orig_evidence = A._simulate_evidence
A._deletion_limits_exceeded = lambda *a, **k: False    # everything satisfied
A._pending_plan_current = lambda cfg, **k: False            # no plan yet
A._simulate_evidence = lambda *a, **k: False                   # no snapshot yet either
try:
    st = A._space_threshold_state(dict(BASE, **RL_CFG), _disk, library_gb=100.0)
    check("mode: simulate required even within limits", st["simulate_required"] is True)
    st = A._space_threshold_state(dict(BASE, **HR_CFG), _disk, library_gb=100.0)
    check("normal: within limits still needs first-Simulate proof", st["simulate_required"] is True)
    # A completed scan (library snapshot) is that proof in normal modes…
    A._simulate_evidence = lambda *a, **k: True
    st = A._space_threshold_state(dict(BASE, **HR_CFG), _disk, library_gb=100.0)
    check("normal: library snapshot proves a Simulate ran", st["simulate_required"] is False)
    # …but never in redline-only, which needs the current standing plan itself.
    st = A._space_threshold_state(dict(BASE, **RL_CFG), _disk, library_gb=100.0)
    check("mode: snapshot alone is not a standing plan", st["simulate_required"] is True)
    A._simulate_evidence = lambda *a, **k: False
    A._pending_plan_current = lambda cfg, **k: True
    st = A._space_threshold_state(dict(BASE, **RL_CFG), _disk, library_gb=100.0)
    check("mode: current plan satisfies the gate", st["simulate_required"] is False)

    # Button states: satisfied never ghosts Simulate in any mode (it builds
    # the standing queue); Live still ghosts while satisfied.
    # Needs a healthy connection probe — an unhealthy one disables everything
    # before the threshold logic is reached.
    A._pending_plan_current = lambda cfg, **k: False
    _orig_health = A._refresh_connection_health_cache
    A._refresh_connection_health_cache = lambda cfg, probe=True: {
        "critical_ok": True, "tautulli_connected": True,
        "jellyfin_connected": True, "radarr_connected": False,
    }
    _orig_lib_stats = A.library_stats
    A.library_stats = lambda: {"library_gb": 100.0}
    # A media server must be selected or the connection gate ghosts everything
    # before the threshold logic runs.
    lb = A._cleanup_button_state(dict(BASE, USE_PLEX=True, **RL_CFG), _disk)
    check("mode: Simulate stays enabled while satisfied", lb["simulate_disabled"] is False)
    check("mode: Live ghosts pending the preview plan", lb["cleanup_disabled"] is True)
    check("mode: tooltip names the preview requirement", "preview" in lb["cleanup_tooltip"])
    lb = A._cleanup_button_state(dict(BASE, USE_PLEX=True, **HR_CFG), _disk)
    check("normal: satisfied keeps Simulate enabled, ghosts Live",
          lb["simulate_disabled"] is False and lb["cleanup_disabled"] is True)
    A.library_stats = _orig_lib_stats
    A._refresh_connection_health_cache = _orig_health
finally:
    A._deletion_limits_exceeded = _orig_limits
    A._pending_plan_current = _orig_plan
    A._simulate_evidence = _orig_evidence

# ── The preview queue: display + never auto-cleared while satisfied ──────────
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, **RL_CFG, OUTPUT_DIR=td)
    now = time.time()
    Path(td, "cache.json").write_text(json.dumps({"pending": {"schema": 1, "entries": {
        "/library/movies/A/A.mkv": {"title": "Worst", "size_bytes": 1000, "marked_at": now - 86400},
        "/library/movies/B/B.mkv": {"title": "Next", "size_bytes": 1000, "marked_at": now},
    }}}), encoding="utf-8")
    # Pin the disk read: the marked/eligible split keys on real free space vs
    # the Redline floor, and the test machine's disk must not decide the case.
    _orig_ds = A.disk_stats
    A.disk_stats = lambda check=None: {"total_gb": 1000.0, "used_gb": 500.0,
                                       "free_gb": 500.0, "pct_used": 50.0}
    entries = A.pending_deletion_entries()
    check("mode: entries keep the queue's own (deletion) order",
          [e["title"] for e in entries] == ["Worst", "Next"])
    check("mode: entries read as Redline-order, not dates",
          entries[0]["when"] == "#1 — deletes when Redline hits"
          and entries[0]["marked"] is False
          and entries[0]["days_remaining"] is None and entries[0]["delete_on"] is None)

    # Below the floor, the deficit-covering prefix reads as marked-to-delete-now
    # and the rest stays queued (deficit of 500 bytes < the first 1000-byte entry).
    A.disk_stats = lambda check=None: {"total_gb": 1000.0, "used_gb": 800.0,
                                       "free_gb": 200.0 - 5e-7, "pct_used": 80.0}
    entries = A.pending_deletion_entries()
    check("mode breached: deficit prefix is marked, rest queued",
          entries[0]["marked"] is True
          and entries[0]["when"] == "#1 — deletes on the next Cleanup (Redline breached)"
          and entries[1]["marked"] is False
          and entries[1]["when"] == "#2 — deletes when Redline hits")
    A.disk_stats = _orig_ds
    f = A.pending_delete_forecast()
    check("mode: forecast schedules nothing",
          f["count"] == 2 and f["ripe"] == 0 and f["event_on"] is None
          and f["event_count"] == 0 and f["waiting_ages"] == [])

    # A config save while within limits must keep the standing preview.
    _orig_limits = A._deletion_limits_exceeded
    _orig_disk_stats = A.cached_disk_stats
    _orig_lib_stats = A.library_stats
    A._deletion_limits_exceeded = lambda *a, **k: False
    A.cached_disk_stats = lambda s=None: dict(_disk)
    A.library_stats = lambda: {"library_gb": 100.0}
    try:
        p = {"RUN_MODE": "paused", "HEADROOM_GB": 0, "REDLINE_GB": 200,
             "REDLINE_ONLY_MODE": True,
             "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
             "USE_PLEX": False, "USE_JELLYFIN": False,
             "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
        r = client.post("/api/config", json=p, headers={"X-MediaReducer": "1"})
        body = r.get_json()
        check("mode: save keeps the preview queue",
              r.status_code == 200 and body.get("pending_unscheduled") == 0 and A.pending_count() == 2)
    finally:
        A._deletion_limits_exceeded = _orig_limits
        A.cached_disk_stats = _orig_disk_stats
        A.library_stats = _orig_lib_stats

# ── Engine: validator + mark upkeep + plan-queue order in the mode ───────────
_eng_saved = (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE,
              engine.MAX_LIBRARY_GB, engine.MONITOR_DIRS)
try:
    engine.HEADROOM_GB, engine.REDLINE_GB, engine.MAX_LIBRARY_GB = 0, 200, None
    engine.REDLINE_ONLY_MODE = True
    engine.MONITOR_DIRS = ["/library/movies"]   # the 0-rules bind only with dirs
    check("engine mode helper", engine._redline_only_mode() is True)
    engine.MAX_LIBRARY_GB = 5000
    check("engine mode helper: headroom off + cap is NOT redline-only",
          engine._redline_only_mode() is False)
    engine.MAX_LIBRARY_GB = None
    errs, _t, _m = engine._space_threshold_errors()
    check("engine validator: clean redline-only passes", errs == [])
    engine.REDLINE_GB = None
    engine.REDLINE_ONLY_MODE = False
    errs, _t, _m = engine._space_threshold_errors()
    check("engine validator: headroom 0 without redline is valid (no thresholds set)",
          errs == [])
    engine.REDLINE_ONLY_MODE = True
    errs, _t, _m = engine._space_threshold_errors()
    check("engine validator: headroom off with neither redline nor cap flagged",
          any("Redline floor" in e and "Library Size Cap" in e for e in errs))
    # Headroom off WITH a cap (no redline) is now valid — the cap drives cleanup.
    engine.MAX_LIBRARY_GB = 5000
    errs, _t, _m = engine._space_threshold_errors()
    check("engine validator: headroom off with a cap (no redline) is valid", errs == [])
    # Headroom off with BOTH a redline and a cap is valid too.
    engine.REDLINE_GB = 200
    errs, _t, _m = engine._space_threshold_errors()
    check("engine validator: headroom off with redline + cap is valid", errs == [])
    engine.MAX_LIBRARY_GB = None
    # The safety cap now bounds the REDLINE floor (15% of 1000 GB = 150 GB).
    errs, _t, _m = engine._space_threshold_errors(usage_info={"total": 1000 * 1_000_000_000})
    check("engine validator: redline over the safety cap flagged",
          any("REDLINE_GB=200" in e and "safety cap" in e for e in errs))

    # Mark upkeep keeps the standing preview while limits are satisfied.
    _store = {"/x/A.mkv": {"title": "A"}, "/x/B.mkv": {"title": "B"}}
    _saved_pending = {}
    engine.REDLINE_GB = 900  # keep mode active, away from the safety-cap case
    _orig_lp, _orig_sp = engine.load_pending, engine.save_pending
    engine.load_pending = lambda: dict(_store)
    engine.save_pending = lambda entries, **k: _saved_pending.update({"v": dict(entries)})
    _orig_fpp = engine.fetch_protected_paths
    _orig_jpi = engine._jellyfin_protected_items
    engine.fetch_protected_paths = lambda: ([], set(), [], 0)
    engine._jellyfin_protected_items = lambda: ([], [], [], 0)
    _orig_exists = engine.Path.exists
    try:
        engine.Path.exists = lambda self: True   # queue files all still on disk
        engine._revalidate_pending_marks(0)   # deficit 0 = within limits
        check("engine upkeep: satisfied does NOT clear the preview", "v" not in _saved_pending)
        # Normal mode: satisfied stops any running delay clocks but KEEPS the
        # queue — it is the standing eligible deletion order in every mode now.
        engine.HEADROOM_GB, engine.REDLINE_ONLY_MODE = 500, False
        _store["/x/A.mkv"]["marked_at"] = 123.0   # a running clock
        engine._revalidate_pending_marks(0)   # deficit 0 = within limits
        _v = _saved_pending.get("v") or {}
        check("engine upkeep: normal mode unschedules clocks but keeps the queue",
              set(_v) == {"/x/A.mkv", "/x/B.mkv"}
              and _v["/x/A.mkv"].get("marked_at") is None)
    finally:
        engine.Path.exists = _orig_exists
        engine.load_pending, engine.save_pending = _orig_lp, _orig_sp
        engine.fetch_protected_paths = _orig_fpp
        engine._jellyfin_protected_items = _orig_jpi
finally:
    (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE,
     engine.MAX_LIBRARY_GB, engine.MONITOR_DIRS) = _eng_saved

# ── Rebuild decision: a live run that consumed queue entries tops it back up ──
_orig_pp = A.progress_path
with tempfile.TemporaryDirectory() as td:
    _state["cfg"] = dict(BASE, **RL_CFG, OUTPUT_DIR=td)
    A.progress_path = lambda: Path(td, "progress.json")
    try:
        Path(td, "progress.json").write_text(json.dumps({"deleted": 3}))
        check("live run that deleted rebuilds", A._preview_rebuild_needed(True) is True)
        check("sim run never rebuilds (it IS the rebuild)", A._preview_rebuild_needed(False) is False)
        Path(td, "progress.json").write_text(json.dumps({"deleted": 0}))
        check("live run that deleted nothing needs no rebuild", A._preview_rebuild_needed(True) is False)
        Path(td, "progress.json").write_text(json.dumps({"queue_rebuild": True}))
        check("fast-path flag rebuilds regardless", A._preview_rebuild_needed(False) is True)
        Path(td, "progress.json").write_text(json.dumps({"deleted": 3}))
        _state["cfg"] = dict(BASE, **HR_CFG, OUTPUT_DIR=td)   # normal mode: no standing queue
        check("normal mode never rebuilds", A._preview_rebuild_needed(True) is False)
    finally:
        A.progress_path = _orig_pp

# ── No-thresholds state (headroom 0, no redline): valid but Live-blocked ─────
_uncfg = dict(BASE, HEADROOM_GB=0, REDLINE_GB=None, MAX_LIBRARY_GB=None,
              MONITOR_DIRS=["/library/movies"])
_state_uncfg = A._space_threshold_state(_uncfg, disk={"total_gb": 1000.0}, library_gb=100.0)
check("no-thresholds state: Simulate stays available",
      _state_uncfg["ok_for_simulate"] is True)
check("no-thresholds state: Live blocked with the setup message",
      _state_uncfg["ok_for_cleanup"] is False
      and "Set a Headroom target" in _state_uncfg["cleanup_tooltip"])
check("no-thresholds state is not redline-only mode",
      A._redline_only_mode_cfg(_uncfg) is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
