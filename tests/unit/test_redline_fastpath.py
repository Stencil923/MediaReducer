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
    engine.CHECK_PATH = lib   # hermetic: disk_usage() reads the temp library, not a real /library mount
    engine.MONITOR_DIRS = [str(movies)]
    engine._RESOLVED_MONITORED_ROOTS = None
    engine.OUTPUT_DIR = out
    engine.LOGFILE = out / "lastrun.log"
    engine.DELETED_LOG = out / "deleted.log"
    engine.PROGRESS_FILE = out / "progress.json"
    engine.DB_FILE = out / "mediareducer.db"   # the queue lives in the DB's queue table
    engine._PLAN_CONFIG_RAW = {k: None for k in engine._PLAN_CONFIG_KEYS}
    engine._PLAN_CONFIG_RAW.update({"HEADROOM_GB": 0, "REDLINE_GB": 200,
                                    "REDLINE_ONLY_MODE": True})
    entries = {str(p): {"title": p.stem, "score": i + 1.0, "size_bytes": 2 * MB,
                        "marked_at": 1000000000 + i}
               for i, p in enumerate(paths)}
    engine.save_pending(entries, stamp_thresholds=True)   # -> the queue, stamped
    return paths


def _pending():
    """The pending doc ({entries, plan_config, monitor_dirs}) from the store."""
    return engine.load_cache().get("pending", {})

engine.fetch_protected_paths = lambda: ([], None, None, None)
engine._jellyfin_protected_items = lambda: (set(), set(), set(), set())
engine.RUN_MODE = "headroom"
engine.REDLINE_ONLY_MODE = True   # the fixture plan is a redline-only preview

# The fast path now re-checks fresh watch data before an emergency delete: a
# movie is deletable only if CONFIRMED unwatched since marking. Give the test a
# snapshot join (each movie known 0 plays) and a fresh fetch; by default every
# movie reads back unwatched, so the plan deletes as before. FRESH_PLAYS lets a
# case mark a specific movie as watched-since so it must be spared.
FRESH_PLAYS = {}   # source_id -> play_count reported by the fresh fetch
engine._snapshot_by_store_key = lambda store: {
    k: {"source_id": Path(k).stem, "jf_source_id": None, "plays": 0, "last_played": 0}
    for k in store}
engine._fresh_watch_data = lambda ids: {
    str(i): {"play_count": FRESH_PLAYS.get(str(i), 0), "last_played": 0, "favorite": False}
    for i in ids}

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
    data = _pending()
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

# ── Fast path: a marked movie whose file is already gone is dropped silently ──
# The user deleted the movie manually since the queue was built. The dead mark
# must be dropped from the queue (not lingered), and the next in line must be
# consumed so the target is still covered — no fallback to the full scan.
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    paths[0].unlink()                                    # Movie A gone from disk
    handled = engine._redline_fast_path(4 * MB)          # needs 2 → A dead, B/C go
    check("a dead mark is skipped and the next in line covers the target",
          handled is True and not paths[0].exists()
          and not paths[1].exists() and not paths[2].exists() and paths[3].exists())
    data = _pending()
    check("the dead mark is dropped from the queue; deleted ones trimmed",
          list(data["entries"]) == [str(paths[3])])

# ── Fast path: a dead mark is dropped from the cache even when coverage falls
#    short and the run falls back to the full scan ─────────────────────────────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    paths[1].unlink()                                    # Movie B gone from disk
    handled = engine._redline_fast_path(50 * MB)         # queue can't cover → fall back
    check("thin coverage with a dead mark still falls back to the full scan",
          handled is False)
    data = _pending()
    check("the dead mark is dropped from the cache on the fallback path too",
          str(paths[1]) not in data["entries"]
          and [str(p) for p in (paths[0], paths[2], paths[3])]
              == [k for k in data["entries"] if k != str(paths[1])])

# ── Fast path: a movie watched since it was marked is spared, next in line used
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    FRESH_PLAYS.clear(); FRESH_PLAYS["Movie A"] = 3      # A watched after marking
    try:
        handled = engine._redline_fast_path(5 * MB)      # needs 3 → A spared, B/C/D go
    finally:
        FRESH_PLAYS.clear()
    check("a movie watched since marking is spared; the next in line covers the target",
          handled is True and paths[0].exists()
          and not paths[1].exists() and not paths[2].exists() and not paths[3].exists())
    data = _pending()
    check("the spared (watched) movie keeps its mark; deleted ones trimmed",
          list(data["entries"]) == [str(paths[0])])

# ── Fast path: if the fresh fetch is unavailable, fall back to the full scan ──
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    _fwd = engine._fresh_watch_data
    engine._fresh_watch_data = lambda ids: (_ for _ in ()).throw(RuntimeError("api down"))
    try:
        check("an unavailable fresh re-check falls back, deletes nothing",
              engine._redline_fast_path(2 * MB) is False and all(p.exists() for p in paths))
    finally:
        engine._fresh_watch_data = _fwd

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
    data = _pending()
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
    data = _pending()
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
        e = _pending()["entries"][str(paths[0])]
        check("redline-only plans carry no delay clocks", e["marked_at"] is None)
        # Mode exits and a normal-mode plan schedules the same entry: its clock
        # starts NOW — the eligible time never counted as served delay.
        engine.HEADROOM_GB, engine.REDLINE_GB = 500, 200
        engine.REDLINE_ONLY_MODE = False
        engine.write_plan_to_queue([(cand, 2 * MB)], "test", scheduled_count=1)
        e = _pending()["entries"][str(paths[0])]
        check("entering the marked prefix starts a fresh clock", e["marked_at"] > 1700000000)
        _first_clock = e["marked_at"]
        engine.write_plan_to_queue([(cand, 2 * MB)], "test", scheduled_count=1)
        e = _pending()["entries"][str(paths[0])]
        check("re-simulate keeps a running clock", e["marked_at"] == _first_clock)
        engine.write_plan_to_queue([(cand, 2 * MB)], "test")    # left the prefix
        e = _pending()["entries"][str(paths[0])]
        check("leaving the marked prefix stops the clock", e["marked_at"] is None)
finally:
    engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE = _hr_saved

# ── do_radarr: a manual-style queue delete forgets each movie in Radarr ───────
# The manual Cleanup runs THIS same fast path (trigger set, do_radarr=True), so
# it must clean up Radarr from the TMDB id + section the queue now stores — while
# a Redline emergency (the default) still skips Radarr.
_radarr_calls = []
_cr_saved = engine.cleanup_radarr
engine.cleanup_radarr = lambda c: _radarr_calls.append((c.get("title"), c.get("tmdb_id"), c.get("section_id")))
try:
    with tempfile.TemporaryDirectory() as td:
        paths = setup(td)
        d = _pending()
        for i, k in enumerate(d["entries"]):             # stamp Radarr identity
            d["entries"][k]["tmdb_id"] = 1000 + i
            d["entries"][k]["section_id"] = 1
        engine.save_pending(d["entries"], stamp_thresholds=True)
        handled = engine._redline_fast_path(3 * MB, trigger="HEADROOM", do_radarr=True)  # A, B go
        check("a manual-style queue delete forgets each deleted movie in Radarr (stored tmdb/section)",
              handled is True and len(_radarr_calls) == 2
              and _radarr_calls[0][1] == 1000 and _radarr_calls[0][2] == 1
              and _radarr_calls[1][1] == 1001)
    with tempfile.TemporaryDirectory() as td:
        paths = setup(td)
        _radarr_calls.clear()
        engine._redline_fast_path(3 * MB)                # default: Redline emergency
        check("a Redline emergency still skips Radarr cleanup (do_radarr defaults off)",
              len(_radarr_calls) == 0)
finally:
    engine.cleanup_radarr = _cr_saved

# ── write_plan_to_queue stores the Radarr identity for the incremental delete ──
_hr2 = (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE)
try:
    with tempfile.TemporaryDirectory() as td:
        paths = setup(td)
        engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE = 0, 200, True
        cand = {"path": paths[0], "title": "Movie A", "retention_score": 1.0,
                "tmdb_id": 4242, "section_id": 1}
        engine.write_plan_to_queue([(cand, 2 * MB)], "test")
        e = _pending()["entries"][str(paths[0])]
        check("the queue entry carries tmdb_id + section_id for Radarr cleanup",
              e.get("tmdb_id") == 4242 and e.get("section_id") == 1)
finally:
    engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE = _hr2

# ── File size optimization in the fast path (the user's scenario) ─────────────
# A group of near-tied-score movies sits at the deletion boundary: three 1 MB
# files and one 5.5 MB file, all within NEAR_TIE_PTS. Target 5 MB. Strict order
# would delete all three small ones and still fall short; File size optimization
# deletes the single 5.5 MB file that covers the target on its own and SPARES the
# three small near-ties.
with tempfile.TemporaryDirectory() as td:
    lib = Path(td, "library"); movies = lib / "movies"
    out = Path(td, "out"); out.mkdir(parents=True, exist_ok=True)
    engine.LIBRARY_ROOT = lib
    engine.CHECK_PATH = lib   # hermetic: disk_usage() reads the temp library, not a real /library mount
    engine.MONITOR_DIRS = [str(movies)]
    engine._RESOLVED_MONITORED_ROOTS = None
    engine.OUTPUT_DIR = out
    engine.LOGFILE = out / "lastrun.log"
    engine.DELETED_LOG = out / "deleted.log"
    engine.PROGRESS_FILE = out / "progress.json"
    engine.DB_FILE = out / "mediareducer.db"
    engine.NEAR_TIE_PTS = 2.0
    engine._PLAN_CONFIG_RAW = {k: None for k in engine._PLAN_CONFIG_KEYS}
    engine._PLAN_CONFIG_RAW.update({"HEADROOM_GB": 0, "REDLINE_GB": 200,
                                    "REDLINE_ONLY_MODE": True, "NEAR_TIE_PTS": 2.0})
    # Plan order = score ascending: three 1 MB near-ties, then a 5.5 MB near-tie.
    specs = [("S1", 1 * MB, 1.0), ("S2", 1 * MB, 1.5),
             ("S3", 1 * MB, 1.8), ("BIG", 5 * MB + MB // 2, 2.0)]
    fpaths = {}
    entries = {}
    for name, sz, score in specs:
        d = movies / f"Movie {name}"; d.mkdir(parents=True)
        f = d / f"Movie {name}.mkv"; f.write_bytes(b"\0" * sz)
        fpaths[name] = f
        entries[str(f)] = {"title": name, "score": score, "size_bytes": sz, "marked_at": None}
    engine.save_pending(entries, stamp_thresholds=True)
    handled = engine._redline_fast_path(5 * MB)
    check("file size optimization deletes the one 5.5 MB near-tie that covers the target",
          handled is True and not fpaths["BIG"].exists())
    check("the three small near-ties are spared (one big movie saved the others)",
          all(fpaths[n].exists() for n in ("S1", "S2", "S3")))
    data = _pending()
    check("only the big movie is trimmed; the spared small near-ties keep their marks",
          set(Path(k).stem for k in data["entries"]) == {"Movie S1", "Movie S2", "Movie S3"})
    # With the optimization OFF, the same queue deletes in strict score order:
    # the three small ones first (3 MB, still short of 5 MB) and then BIG too — so
    # ALL FOUR go (4 deletions) instead of the single one. Proves the setting gates it.
    engine.NEAR_TIE_PTS = None
    for name, sz, score in specs:
        d = movies / f"Movie {name}"; d.mkdir(parents=True, exist_ok=True)
        (d / f"Movie {name}.mkv").write_bytes(b"\0" * sz)   # BIG's folder was removed on delete
    engine.save_pending(entries, stamp_thresholds=True)
    engine._redline_fast_path(5 * MB)
    check("optimization off: strict order deletes all four (small near-ties first, then BIG)",
          not any(fpaths[n].exists() for n in ("S1", "S2", "S3", "BIG")))
    engine.NEAR_TIE_PTS = 2.0

# ── Stored queue in NON-score order still deletes worst-first ────────────────
# The queue is written in file-size-optimized order by Simulate (NOT pure score
# order). _pop_next_deletion assumes score-ascending input, so the fast path must
# re-sort worst-first before selecting — otherwise the near-tie window forms from
# a high-scored head and would delete a GOOD movie over the WORST ones. (Regression
# for the sim-vs-debug-live log mismatch.)
with tempfile.TemporaryDirectory() as td:
    lib = Path(td, "library"); movies = lib / "movies"
    out = Path(td, "out"); out.mkdir(parents=True, exist_ok=True)
    engine.LIBRARY_ROOT = lib
    engine.CHECK_PATH = lib   # hermetic: disk_usage() reads the temp library, not a real /library mount
    engine.MONITOR_DIRS = [str(movies)]
    engine._RESOLVED_MONITORED_ROOTS = None
    engine.OUTPUT_DIR = out
    engine.LOGFILE = out / "lastrun.log"
    engine.DELETED_LOG = out / "deleted.log"
    engine.PROGRESS_FILE = out / "progress.json"
    engine.DB_FILE = out / "mediareducer.db"
    engine.NEAR_TIE_PTS = 2.0
    engine._PLAN_CONFIG_RAW = {k: None for k in engine._PLAN_CONFIG_KEYS}
    engine._PLAN_CONFIG_RAW.update({"HEADROOM_GB": 0, "REDLINE_GB": 200,
                                    "REDLINE_ONLY_MODE": True, "NEAR_TIE_PTS": 2.0})
    # Three worst near-ties (1 MB each) + one GOOD large movie (2.5 MB). The queue
    # is STORED with GOOD first (as a file-size-opt Simulate would), not by score.
    specs = [("GOOD", 5 * MB // 2, 9.0), ("W1", 1 * MB, 1.0),
             ("W2", 1 * MB, 1.5), ("W3", 1 * MB, 1.8)]
    fp = {}
    entries = {}
    for name, sz, score in specs:
        d = movies / f"Movie {name}"; d.mkdir(parents=True)
        f = d / f"Movie {name}.mkv"; f.write_bytes(b"\0" * sz)
        fp[name] = f
        entries[str(f)] = {"title": name, "score": score, "size_bytes": sz, "marked_at": None}
    engine.save_pending(entries, stamp_thresholds=True)
    engine._redline_fast_path(2 * MB)             # needs 2 MB
    check("worst-first: the two lowest-scored movies go, GOOD is spared",
          not fp["W1"].exists() and not fp["W2"].exists()
          and fp["GOOD"].exists() and fp["W3"].exists())
    engine.NEAR_TIE_PTS = 2.0

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
