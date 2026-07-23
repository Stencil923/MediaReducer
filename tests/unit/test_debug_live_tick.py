"""Debug Cleanup mirrors a real cleanup tick from the standing marked queue in cache
— NO full scan and it DELETES NOTHING, but it DOES apply the same cache-honesty
upkeep a Cleanup does (drop gone/newly-protected marks, re-score) and then only
PREVIEWS the deletions. This locks: `_debug_cleanup_delete_preview` is fully read-only
(walks the queue, reports what a cleanup tick WOULD delete via the same protection +
fresh-watch re-verify as the redline fast path, never unlinks or writes
deleted.log), while `_debug_cleanup_from_queue` runs the real (persisting) upkeep and
deletes no files — 'Cleanup minus deletion'."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate
import engine

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

MB = 1_000_000

engine.log = lambda *a, **k: None
engine.log_raw = lambda *a, **k: None
engine.log_stage = lambda *a, **k: None
engine.log_blank = lambda *a, **k: None
engine._QUIET_PROGRESS = False
engine.fetch_protected_paths = lambda: ([], None, None, None)
engine._jellyfin_protected_items = lambda: (set(), set(), set(), set())
engine._jellyfin_favorite_paths = lambda: set()
engine.RUN_MODE = "debug_cleanup"
engine.REDLINE_ONLY_MODE = True

FRESH_PLAYS = {}   # source_id -> play_count the fresh fetch reports (default 0 = unwatched)
engine._snapshot_by_store_key = lambda store: {
    k: {"source_id": Path(k).stem, "jf_source_id": None, "plays": 0, "last_played": 0}
    for k in store}
engine._fresh_watch_data = lambda ids: {
    str(i): {"play_count": FRESH_PLAYS.get(str(i), 0), "last_played": 0, "favorite": False}
    for i in ids}


def setup(td):
    """Temp library with 4 real 2 MB movies + a matching current plan (redline-only)."""
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
    engine.DB_FILE = out / "mediareducer.db"   # the queue lives in the DB's queue table
    engine._PLAN_CONFIG_RAW = {k: None for k in engine._PLAN_CONFIG_KEYS}
    engine._PLAN_CONFIG_RAW.update({"HEADROOM_GB": 0, "REDLINE_GB": 200, "REDLINE_ONLY_MODE": True})
    entries = {str(p): {"title": p.stem, "score": i + 1.0, "size_bytes": 2 * MB,
                        "marked_at": 1000000000 + i}
               for i, p in enumerate(paths)}
    engine.save_pending(entries, stamp_thresholds=True)   # -> the queue, stamped
    return paths

# ── Preview reports the covering prefix and touches nothing ───────────────────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    before = _dbstate.read(engine.DB_FILE)
    count, freed, covers = engine._debug_cleanup_delete_preview(5 * MB, "REDLINE")
    check("preview reports the covering prefix (3 of the 2 MB files, target 5 MB)",
          count == 3 and freed == 6 * MB and covers is True)
    check("preview deletes nothing", all(p.exists() for p in paths))
    check("preview leaves the pending queue unchanged",
          _dbstate.read(engine.DB_FILE) == before)
    check("preview writes no deleted.log", not engine.DELETED_LOG.exists())

# ── A movie watched since marking is spared, next in line covers ──────────────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    FRESH_PLAYS.clear(); FRESH_PLAYS["Movie A"] = 3
    try:
        count, freed, covers = engine._debug_cleanup_delete_preview(5 * MB, "REDLINE")
    finally:
        FRESH_PLAYS.clear()
    check("a since-watched movie is spared; the next in line covers the target",
          count == 3 and covers is True and all(p.exists() for p in paths))

# ── A queue too thin reports it can't cover (a real run would fall back) ───────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    count, freed, covers = engine._debug_cleanup_delete_preview(50 * MB, "REDLINE")
    check("a queue too thin for the target reports covers=False, still deletes nothing",
          covers is False and all(p.exists() for p in paths))

# ── A stale plan yields no preview (a real run would full-scan) ───────────────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    engine._PLAN_CONFIG_RAW["GRACE_PERIOD_DAYS"] = 7    # differs from the stamped plan
    count, freed, covers = engine._debug_cleanup_delete_preview(2 * MB, "REDLINE")
    check("a stale plan previews nothing (would fall back to a full scan)",
          (count, freed, covers) == (0, 0, False) and all(p.exists() for p in paths))

# ── End to end: the debug_cleanup body deletes nothing (removes no queue entries) ─
# The upkeep may adjust marked_at flags (redline-only carries no delay clocks), but
# it must never REMOVE an entry (that would mean a deletion) or write deleted.log,
# while the would-delete tally is still reported.
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    before_keys = set(engine.load_cache().get("pending", {}).get("entries", {}))
    engine._debug_cleanup_from_queue(to_free_bytes=5 * MB, trigger="REDLINE", breached=True,
                                  used_gb=900.0, max_gb=800.0, library_gb=500.0)
    after_keys = set(engine.load_cache().get("pending", {}).get("entries", {}))
    check("debug_cleanup deletes nothing", all(p.exists() for p in paths))
    check("debug_cleanup removes no queue entries (it deletes nothing)", after_keys == before_keys)
    check("debug_cleanup writes no deleted.log", not engine.DELETED_LOG.exists())
    prog = json.loads(engine.PROGRESS_FILE.read_text())
    check("debug_cleanup reports the would-delete tally and ends done",
          prog.get("status") == "done" and prog.get("deleted") == 3 and prog.get("bytes_freed") == 6 * MB)

# ── Within limits: the tick previews nothing to delete ────────────────────────
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    engine._debug_cleanup_from_queue(to_free_bytes=0, trigger="scheduled daily", breached=False,
                                  used_gb=100.0, max_gb=800.0, library_gb=500.0)
    prog = json.loads(engine.PROGRESS_FILE.read_text())
    check("within limits the tick deletes nothing and reports 0",
          all(p.exists() for p in paths)
          and prog.get("status") == "done" and (prog.get("deleted") or 0) == 0)

# ── Debug Cleanup's upkeep PERSISTS (it's 'Cleanup minus deletion', not a dry run) ──
# A marked file vanished. Running the debug_cleanup body must drop that dead mark from
# the queue on disk (same as a real Cleanup's upkeep) — while still deleting no
# real file. This is the behavior that changed: the upkeep is no longer dry.
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)
    paths[0].unlink()                       # a marked file vanished → upkeep drops it
    engine._debug_cleanup_from_queue(to_free_bytes=2 * MB, trigger="REDLINE", breached=True,
                                  used_gb=900.0, max_gb=800.0, library_gb=500.0)
    after = engine.load_cache().get("pending", {})
    check("debug_cleanup persists the drop of a gone mark (upkeep is not dry)",
          str(paths[0]) not in after.get("entries", {}))
    check("debug_cleanup still deleted no real file", all(p.exists() for p in paths[1:]))
    check("debug_cleanup wrote no deleted.log", not engine.DELETED_LOG.exists())

# ── The preview logs ONLY the covering prefix — no full-queue "spare" spam ────
# Regression for a run where a tiny redline deficit against a 2,500-entry queue
# logged a "Would spare (unverifiable)" line for every movie past the covering
# prefix and buried the real WOULD DELETE lines under thousands of them.
with tempfile.TemporaryDirectory() as td:
    paths = setup(td)                      # 4 × 2 MB, target 2 MB → first file covers
    _logs = []
    _real_log = engine.log
    engine.log = lambda m="", *a, **k: _logs.append(str(m))
    try:
        count, freed, covers = engine._debug_cleanup_delete_preview(2 * MB, "REDLINE")
    finally:
        engine.log = _real_log
    would = [l for l in _logs if "WOULD DELETE" in l]
    spare = [l for l in _logs if "Would spare" in l]
    check("only the covering prefix is logged inline — no spare-spam for the rest of the queue",
          count == 1 and covers is True and len(would) == 1 and len(spare) == 0)

# ── File size optimization in the preview: says WHICH of a near-tied group it
#    would delete — the one big file that covers, sparing the small ones ─────────
with tempfile.TemporaryDirectory() as td:
    lib = Path(td, "library"); movies = lib / "movies"
    out = Path(td, "out"); out.mkdir(parents=True, exist_ok=True)
    engine.LIBRARY_ROOT = lib
    engine.MONITOR_DIRS = [str(movies)]
    engine._RESOLVED_MONITORED_ROOTS = None
    engine.OUTPUT_DIR = out
    engine.DB_FILE = out / "mediareducer.db"
    engine.PROGRESS_FILE = out / "progress.json"
    engine.DELETED_LOG = out / "deleted.log"
    engine.NEAR_TIE_PTS = 2.0
    engine._PLAN_CONFIG_RAW = {k: None for k in engine._PLAN_CONFIG_KEYS}
    engine._PLAN_CONFIG_RAW.update({"HEADROOM_GB": 0, "REDLINE_GB": 200,
                                    "REDLINE_ONLY_MODE": True, "NEAR_TIE_PTS": 2.0})
    specs = [("S1", 1 * MB, 1.0), ("S2", 1 * MB, 1.5),
             ("S3", 1 * MB, 1.8), ("BIG", 5 * MB + MB // 2, 2.0)]
    entries = {}
    for name, sz, score in specs:
        d = movies / f"Movie {name}"; d.mkdir(parents=True)
        f = d / f"Movie {name}.mkv"; f.write_bytes(b"\0" * sz)
        entries[str(f)] = {"title": name, "score": score, "size_bytes": sz, "marked_at": None}
    engine.save_pending(entries, stamp_thresholds=True)
    _logs = []
    _real_log = engine.log
    engine.log = lambda m="", *a, **k: _logs.append(str(m))
    try:
        count, freed, covers = engine._debug_cleanup_delete_preview(5 * MB, "REDLINE")
    finally:
        engine.log = _real_log
    would = [l for l in _logs if "WOULD DELETE" in l]
    check("preview: file size optimization reports the single big near-tie it would delete",
          count == 1 and covers is True and len(would) == 1 and "BIG" in would[0])
    check("preview: it deletes nothing (all files still present)",
          all((movies / f"Movie {n}" / f"Movie {n}.mkv").exists() for n, _, _ in specs))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
