"""Redline emergency fast path: with a CURRENT plan, an emergency deletes from
the marked queue in plan order — no full library rescan — re-verifying only the
cheap high-stakes facts (monitored root, fresh protection fetch). Any doubt
falls back to the full scan."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import engine

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

MB = 1_000_000

def setup(td):
    """Temp library with 4 real 2 MB movies, a matching current plan, quiet logs."""
    lib = Path(td, "library"); movies = lib / "movies"
    paths = []
    for name in ("A", "B", "C", "D"):
        d = movies / f"Movie {name}"; d.mkdir(parents=True)
        f = d / f"Movie {name}.mkv"; f.write_bytes(b"\0" * (2 * MB))
        paths.append(f)
    out = Path(td, "out"); out.mkdir()
    engine.LIBRARY_ROOT = lib
    engine.MONITOR_DIRS = [str(movies)]
    engine._RESOLVED_MONITORED_ROOTS = None
    engine.OUTPUT_DIR = out
    engine.LOGFILE = out / "lastrun.log"
    engine.DELETED_LOG = out / "deleted.log"
    engine.PROGRESS_FILE = out / "progress.json"
    engine.PENDING_FILE = out / "pending_deletions.json"
    engine._PLAN_CONFIG_RAW = {k: None for k in engine._PLAN_CONFIG_KEYS}
    engine._PLAN_CONFIG_RAW.update({"HEADROOM_GB": 0, "REDLINE_GB": 200,
                                    "REDLINE_ONLY_MODE": True})
    entries = {str(p): {"title": p.stem, "score": i + 1.0, "size_bytes": 2 * MB,
                        "marked_at": 1000000000 + i}
               for i, p in enumerate(paths)}
    engine.PENDING_FILE.write_text(json.dumps({
        "schema": 1, "entries": entries,
        "plan_config": dict(engine._PLAN_CONFIG_RAW),
        "monitor_dirs": sorted(str(d) for d in engine.MONITOR_DIRS),
    }), encoding="utf-8")
    return paths

engine.fetch_protected_paths = lambda: ([], None, None, None)
engine._jellyfin_protected_items = lambda: (set(), set(), set(), set())
engine.RUN_MODE = "headroom"
engine.REDLINE_ONLY_MODE = True   # the fixture plan is a redline-only preview

# ── Plan-currency stamp ──────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    setup(td)
    check("current stamp passes", engine._plan_stamp_current() is True)
    engine._PLAN_CONFIG_RAW["SCORE_BALANCE"] = 80        # rules changed since sim
    check("changed rule stales the stamp", engine._plan_stamp_current() is False)
    engine._PLAN_CONFIG_RAW["SCORE_BALANCE"] = None
    engine.MONITOR_DIRS = ["/somewhere/else"]
    check("changed paths stale the stamp", engine._plan_stamp_current() is False)

# ── Fast path: deletes in plan order, stops at the target, trims marks ───────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    handled = engine._redline_fast_path(5 * MB)          # needs 3 of the 2 MB files
    deleted = [p.name for p in paths if not p.exists()]
    kept    = [p.name for p in paths if p.exists()]
    check("emergency handled from the queue", handled is True)
    check("first three deleted in plan order",
          deleted == ["Movie A.mkv", "Movie B.mkv", "Movie C.mkv"] and kept == ["Movie D.mkv"])
    data = json.loads(engine.PENDING_FILE.read_text())
    check("deleted marks trimmed, survivor kept",
          list(data["entries"]) == [str(paths[3])])
    check("trim preserves the plan stamp", isinstance(data.get("plan_config"), dict))
    dlog = engine.DELETED_LOG.read_text()
    check("deleted.log records all three",
          all(f"Movie {n}" in dlog for n in "ABC") and "Movie D" not in dlog)
    prog = json.loads(engine.PROGRESS_FILE.read_text())
    check("terminal progress asks for a preview rebuild",
          prog.get("status") == "done" and prog.get("queue_rebuild") is True
          and prog.get("deleted") == 3)

# ── Fallbacks: stale plan, protected marks, thin queue, protection failure ───
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    engine._PLAN_CONFIG_RAW["GRACE_PERIOD_DAYS"] = 7     # stale vs the stamp
    check("stale plan falls back to the full scan",
          engine._redline_fast_path(2 * MB) is False and all(p.exists() for p in paths))

with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    engine.fetch_protected_paths = lambda: ([str(paths[0])], None, None, None)
    handled = engine._redline_fast_path(3 * MB)          # A protected → B, C go
    check("freshly-protected mark is skipped, order continues",
          handled is True and paths[0].exists()
          and not paths[1].exists() and not paths[2].exists() and paths[3].exists())
    engine.fetch_protected_paths = lambda: ([], None, None, None)

with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    check("queue too thin for the target falls back",
          engine._redline_fast_path(50 * MB) is False and all(p.exists() for p in paths))

with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    def _boom(): raise RuntimeError("api down")
    engine.fetch_protected_paths = _boom
    check("unverifiable protection falls back (fail-closed)",
          engine._redline_fast_path(2 * MB) is False and all(p.exists() for p in paths))
    engine.fetch_protected_paths = lambda: ([], None, None, None)

# ── Unlink failures: route around them; if NOTHING deletes, fall back ────────
_orig_unlink = engine.Path.unlink
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    def _deny_a(self, *a, **k):
        if self.name == "Movie A.mkv":
            raise OSError(13, "Permission denied")
        return _orig_unlink(self, *a, **k)
    engine.Path.unlink = _deny_a
    try:
        handled = engine._redline_fast_path(4 * MB)     # needs 2 files; A won't go
    finally:
        engine.Path.unlink = _orig_unlink
    check("undeletable head is routed around with later queue entries",
          handled is True and paths[0].exists()
          and not paths[1].exists() and not paths[2].exists() and paths[3].exists())
    data = json.loads(engine.PENDING_FILE.read_text())
    check("failed file keeps its mark; deleted ones trimmed",
          list(data["entries"]) == [str(paths[0]), str(paths[3])])

with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    def _deny_movies(self, *a, **k):
        if self.name.endswith(".mkv"):
            raise OSError(13, "Permission denied")
        return _orig_unlink(self, *a, **k)
    engine.Path.unlink = _deny_movies
    try:
        handled = engine._redline_fast_path(2 * MB)
    finally:
        engine.Path.unlink = _orig_unlink
    check("nothing deletable falls back to the full scan (never 'handled' empty)",
          handled is False and all(p.exists() for p in paths))

# ── Jellyfin favorites: re-fetched fresh, favorited marks are skipped ────────
_fav_real = engine._jellyfin_favorite_paths
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    engine._jellyfin_favorite_paths = lambda: {str(paths[0])}   # A favorited post-mark
    try:
        handled = engine._redline_fast_path(3 * MB)             # needs 2 → B, C go
    finally:
        engine._jellyfin_favorite_paths = _fav_real
    check("freshly-favorited mark is skipped, order continues",
          handled is True and paths[0].exists()
          and not paths[1].exists() and not paths[2].exists() and paths[3].exists())

# The helper itself is a no-op unless the favorites protection is in play —
# it must never hit the API (which would fail here) when gated off.
_jf_saved = (engine.USE_JELLYFIN, engine.PROTECT_JELLYFIN_FAVORITES)
def _no_api(*a, **k): raise RuntimeError("API must not be called")
_req_saved = engine._jellyfin_request
engine._jellyfin_request = _no_api
try:
    engine.USE_JELLYFIN, engine.PROTECT_JELLYFIN_FAVORITES = False, True
    check("favorites fetch skipped without Jellyfin", engine._jellyfin_favorite_paths() == set())
    engine.USE_JELLYFIN, engine.PROTECT_JELLYFIN_FAVORITES = True, False
    check("favorites fetch skipped when protection is off", engine._jellyfin_favorite_paths() == set())
finally:
    engine.USE_JELLYFIN, engine.PROTECT_JELLYFIN_FAVORITES = _jf_saved
    engine._jellyfin_request = _req_saved

# ── Stop (SIGTERM → SystemExit) during the protection fetch ends the run ─────
# It must propagate — never be treated as "protection unverifiable", which
# would fall back to a FULL SCAN that keeps deleting after the user hit Stop.
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    def _stop(): raise SystemExit(143)
    _fp_saved = engine.fetch_protected_paths
    engine.fetch_protected_paths = _stop
    try:
        raised = False
        try:
            engine._redline_fast_path(2 * MB)
        except SystemExit as e:
            raised = (e.code == 143)
        check("SystemExit propagates out of the protection fetch",
              raised and all(p.exists() for p in paths))
    finally:
        engine.fetch_protected_paths = _fp_saved

# ── deleted.log write failure must not kill the run mid-deletion ─────────────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    _dl_saved = engine.DELETED_LOG
    engine.DELETED_LOG = engine.OUTPUT_DIR    # open(dir, "a") raises IsADirectoryError
    try:
        handled = engine._redline_fast_path(3 * MB)
    finally:
        engine.DELETED_LOG = _dl_saved
    check("unwritable deleted.log doesn't abort the emergency",
          handled is True and not paths[0].exists() and not paths[1].exists())
    data = json.loads(engine.PENDING_FILE.read_text())
    check("marks still trimmed when the audit write fails",
          list(data["entries"]) == [str(paths[2]), str(paths[3])])

# ── Delay clocks and the eligible queue ──────────────────────────────────────
# Redline-only plans never carry delay clocks (nothing is scheduled), so
# leaving the mode can never smuggle already-served delay time into a normal-
# mode plan: an eligible (unclocked) entry entering the marked prefix starts
# its clock fresh, and a clocked entry keeps its running age.
_hr_saved = (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE)
try:
    with tempfile.TemporaryDirectory() as td:
        paths = setup(td)
        engine.HEADROOM_GB, engine.REDLINE_GB = 0, 200          # redline-only
        engine.REDLINE_ONLY_MODE = True
        cand = {"path": paths[0], "title": "Movie A", "retention_score": 1.0}
        engine.write_plan_to_queue([(cand, 2 * MB)], "test")    # scheduled_count 0
        e = json.loads(engine.PENDING_FILE.read_text())["entries"][str(paths[0])]
        check("redline-only plans carry no delay clocks", e["marked_at"] is None)
        # Mode exits and a normal-mode plan schedules the same entry: its clock
        # starts NOW — the eligible time never counted as served delay.
        engine.HEADROOM_GB, engine.REDLINE_GB = 500, 200
        engine.REDLINE_ONLY_MODE = False
        engine.write_plan_to_queue([(cand, 2 * MB)], "test", scheduled_count=1)
        e = json.loads(engine.PENDING_FILE.read_text())["entries"][str(paths[0])]
        check("entering the marked prefix starts a fresh clock", e["marked_at"] > 1700000000)
        _first_clock = e["marked_at"]
        engine.write_plan_to_queue([(cand, 2 * MB)], "test", scheduled_count=1)
        e = json.loads(engine.PENDING_FILE.read_text())["entries"][str(paths[0])]
        check("re-simulate keeps a running clock", e["marked_at"] == _first_clock)
        engine.write_plan_to_queue([(cand, 2 * MB)], "test")    # left the prefix
        e = json.loads(engine.PENDING_FILE.read_text())["entries"][str(paths[0])]
        check("leaving the marked prefix stops the clock", e["marked_at"] is None)
finally:
    engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE = _hr_saved

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
