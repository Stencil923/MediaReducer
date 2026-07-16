"""Operating time zone: the TIME_ZONE setting points the process clock at the
configured zone (daily-run midnight, deletion-delay aging, log timestamps all
follow local time), with auto meaning the container clock."""
import json
import os
import subprocess
import sys
import tempfile
import time
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

_ORIG_TZ = os.environ.get("TZ")
def restore_tz():
    if _ORIG_TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _ORIG_TZ
    time.tzset()

BASE = {
    "RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
    "USE_PLEX": False, "USE_JELLYFIN": False,
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
    "OUTPUT_DIR": "/tmp/mr-test-out",
}

def save(payload_over, base_over=None):
    _state["cfg"] = dict(BASE, **(base_over or {}))
    p = {"RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": None,
         "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15, "MONITOR_DIRS": [],
         "USE_PLEX": False, "USE_JELLYFIN": False,
         "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
    p.update(payload_over)
    r = client.post("/api/config", json=p, headers={"X-MediaReducer": "1"})
    return r.status_code, dict(_state["cfg"])

try:
    # ── Validation ────────────────────────────────────────────────────────────
    check("auto normalizes", A._validate_time_zone("auto") == "auto"
          and A._validate_time_zone("") == "auto" and A._validate_time_zone(None) == "auto")
    check("IANA zone passes through", A._validate_time_zone("America/Phoenix") == "America/Phoenix")
    try:
        A._validate_time_zone("Not/AZone")
        check("garbage zone rejected", False)
    except ValueError:
        check("garbage zone rejected", True)
    check("file validator flags a bad zone",
          any(i["key"] == "TIME_ZONE" for i in A._config_file_issues({"TIME_ZONE": "Not/AZone"})))
    check("file validator accepts auto and a real zone",
          not A._config_file_issues({"TIME_ZONE": "auto"})
          and not A._config_file_issues({"TIME_ZONE": "Europe/Berlin"}))

    # ── Applying the zone moves the process clock ─────────────────────────────
    _state["cfg"] = dict(BASE, TIME_ZONE="UTC")
    A._apply_configured_time_zone()
    check("UTC applied", time.strftime("%z") == "+0000")
    _state["cfg"] = dict(BASE, TIME_ZONE="Pacific/Kiritimati")   # +14:00, no DST
    changed = A._apply_configured_time_zone()
    check("zone change reported", changed is True)
    check("process clock follows the configured zone", time.strftime("%z") == "+1400")
    check("unchanged zone reports no change", A._apply_configured_time_zone() is False)
    _state["cfg"] = dict(BASE, TIME_ZONE="auto")
    A._apply_configured_time_zone()
    check("auto restores the host clock", os.environ.get("TZ") == A._HOST_TZ
          or (A._HOST_TZ is None and "TZ" not in os.environ))

    # ── Config save applies the zone and burns the daily window ──────────────
    with tempfile.TemporaryDirectory() as td:
        code, cfg = save({"TIME_ZONE": "Pacific/Kiritimati"}, base_over={"OUTPUT_DIR": td})
        check("valid zone saves", code == 200 and cfg.get("TIME_ZONE") == "Pacific/Kiritimati")
        check("save re-points the process clock", time.strftime("%z") == "+1400")
        cache = json.loads(Path(td, "cache.json").read_text())
        check("zone change burns the daily window",
              cache.get("last_cleanup_date") == time.strftime("%Y-%m-%d"))
        code, _ = save({"TIME_ZONE": "Not/AZone"}, base_over={"OUTPUT_DIR": td})
        check("invalid zone rejected on save", code == 400)

    # ── Daily run time: 24h HH:MM, blank = midnight ───────────────────────────
    code, cfg = save({"DAILY_RUN_TIME": "03:30"})
    check("daily run time saves", code == 200 and cfg.get("DAILY_RUN_TIME") == "03:30")
    code, cfg = save({"DAILY_RUN_TIME": None})
    check("blank daily run time means midnight", code == 200 and cfg.get("DAILY_RUN_TIME") == "00:00")
    for bad in ("3:30", "24:00", "12:60", "noonish"):
        code, _ = save({"DAILY_RUN_TIME": bad})
        check(f"daily run time rejects {bad!r}", code == 400)
    check("file validator flags a bad run time",
          any(i["key"] == "DAILY_RUN_TIME" for i in A._config_file_issues({"DAILY_RUN_TIME": "25:00"})))
    check("file validator accepts a real run time",
          not A._config_file_issues({"DAILY_RUN_TIME": "23:30"}))

    # ── A running engine locks the clock: the global save guard covers the
    # time zone too (the UI also ghosts the field while a run is active) ──────
    with tempfile.TemporaryDirectory() as td:
        A._run_active = True
        code, _ = save({"TIME_ZONE": "Europe/Berlin"}, base_over={"OUTPUT_DIR": td})
        check("zone change refused while a run is active", code == 409)
        A._run_active = False
        code, cfg = save({"TIME_ZONE": "Europe/Berlin"}, base_over={"OUTPUT_DIR": td})
        check("zone change allowed once the run ends",
              code == 200 and cfg.get("TIME_ZONE") == "Europe/Berlin")

    # ── Context processor exposes the clock for the UI skew check ─────────────
    _state["cfg"] = dict(BASE)
    with A.app.test_request_context("/"):
        ctx = A.inject_display_time_settings()
    check("context exposes server epoch and zone",
          isinstance(ctx.get("server_epoch"), float) and bool(ctx.get("server_time_zone"))
          and bool(ctx.get("host_time_zone")))

    # ── First-launch browser time-zone auto-detect ───────────────────────────
    A._run_active = False
    A._apply_configured_time_zone = lambda c=None: False
    A.burn_daily_window_on_startup = lambda *a, **k: None
    def _tz_init(tz, cfg):
        _state["cfg"] = dict(cfg)
        r = client.post("/api/timezone/init", json={"tz": tz}, headers={"X-MediaReducer": "1"})
        return r.get_json(), dict(_state["cfg"])

    d, c1 = _tz_init("America/Phoenix", {"TIME_ZONE": "auto"})
    check("fresh install adopts the browser zone",
          d.get("time_zone") == "America/Phoenix" and c1.get("TIME_ZONE") == "America/Phoenix"
          and c1.get("_TIME_ZONE_AUTODETECTED") is True)
    d, c2 = _tz_init("Europe/Paris", {"TIME_ZONE": "America/Phoenix", "_TIME_ZONE_AUTODETECTED": True})
    check("already-detected is a no-op", d.get("already") is True and c2.get("TIME_ZONE") == "America/Phoenix")
    _d, c3 = _tz_init("America/Phoenix", {"TIME_ZONE": "auto", "_CONNECTIONS_EVER_CONFIGURED": True})
    check("existing install is never overwritten", c3.get("TIME_ZONE") == "auto")
    _d, c4 = _tz_init("America/Phoenix", {"TIME_ZONE": "America/New_York"})
    check("a deliberate zone is never overwritten", c4.get("TIME_ZONE") == "America/New_York")
    _d, c5 = _tz_init("Not/AZone", {"TIME_ZONE": "auto"})
    check("an invalid browser zone stays auto but flags done",
          c5.get("TIME_ZONE") == "auto" and c5.get("_TIME_ZONE_AUTODETECTED") is True)
    A._run_active = True
    d6, c6 = _tz_init("America/Phoenix", {"TIME_ZONE": "auto"})
    check("no config write during a run", d6.get("deferred") is True and c6.get("TIME_ZONE") == "auto")
    A._run_active = False
    with A.app.test_request_context("/"):
        _state["cfg"] = {"TIME_ZONE": "auto"}
        fresh_needs = A.inject_display_time_settings().get("time_zone_needs_init")
        _state["cfg"] = {"TIME_ZONE": "auto", "_CONNECTIONS_EVER_CONFIGURED": True}
        existing_needs = A.inject_display_time_settings().get("time_zone_needs_init")
    check("needs-init is true only on a fresh install", bool(fresh_needs) and not existing_needs)

    # ── Engine adopts the zone from config.json at load ───────────────────────
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td, "config.json")
        cfg_file.write_text(json.dumps({"TIME_ZONE": "Pacific/Kiritimati"}), encoding="utf-8")
        env = dict(os.environ, MEDIAREDUCER_CONFIG=str(cfg_file))
        env.pop("TZ", None)
        out = subprocess.run(
            [sys.executable, "-c",
             "import engine, time; engine._load_config_from_file(); print(time.strftime('%z'))"],
            env=env, cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True, timeout=60)
        check("engine adopts the configured zone", out.stdout.strip().endswith("+1400"))
finally:
    restore_tz()

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
