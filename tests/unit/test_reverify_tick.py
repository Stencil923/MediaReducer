"""The tick wiring: _revalidate_pending_marks (the 15-minute Summary upkeep) runs
the incremental re-verify for headroom/library-cap while breached, and skips it
in redline-only mode — end to end against a real pending file + cache snapshot,
with the fresh-watch fetch and protections stubbed. Proves the glue: mode gate,
the snapshot↔plan path join, and that the un-mark/backfill is persisted."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
E.SCORE_BALANCE = 0
E.HISTORY_WEIGHT, E.QUALITY_WEIGHT = E.score_balance_weights(0)
E.MAX_STALENESS_MONTHS = 36
# Protections quiet; favorites off.
E.fetch_protected_paths = lambda: ([], None, None, None)
E._jellyfin_protected_items = lambda: (set(), set(), set(), set())
E.USE_JELLYFIN = False
E.PROTECT_JELLYFIN_FAVORITES = False

def _seed(td):
    lib = Path(td, "library"); movies = lib / "movies"
    paths = []
    for n in ("A", "B", "C", "D"):
        d = movies / f"Movie {n}"; d.mkdir(parents=True)
        f = d / f"Movie {n}.mkv"; f.write_bytes(b"\0" * 1024)
        paths.append(f)
    out = Path(td, "out"); out.mkdir()
    E.LIBRARY_ROOT = lib
    E.MONITOR_DIRS = [str(movies)]
    E.OUTPUT_DIR = out
    E.PENDING_FILE = out / "pending_deletions.json"
    E.CACHE_FILE = out / "cache.json"
    # Plan: A, B marked (covering count 2); C, D eligible behind them.
    entries = {}
    for i, p in enumerate(paths):
        entries[str(p)] = {"title": p.stem, "score": 1.0, "size_bytes": 1024,
                           "marked_at": (time.time() - 86400) if i < 2 else None}
    # Snapshot: every movie known unwatched (0 plays), keyed by the same path.
    # The queue now lives under cache.json["pending"], so write it all in one file.
    snap = [E._snapshot_entry(p.stem, 2000, 6.0, 1000, 0, 0, 0, 1_500_000_000, 1024,
                              source_id=p.stem, path=str(p)) for p in paths]
    E.CACHE_FILE.write_text(json.dumps({
        "code_checksum": E.code_checksum(),
        "library_snapshot": {"movies": snap, "built_at": 1},
        "pending": {"schema": 1, "entries": entries},
    }), encoding="utf-8")
    return paths

def _marked(paths):
    data = E.load_cache().get("pending", {}).get("entries", {})
    return {p.stem for p in paths if data.get(str(p), {}).get("marked_at") is not None}

def _snap_plays(path):
    movies = (E.load_cache().get("library_snapshot") or {}).get("movies") or []
    return {m["path"]: m.get("plays") for m in movies}.get(str(path))

def _snap_built_at():
    return (E.load_cache().get("library_snapshot") or {}).get("built_at")

# Fresh fetch: Movie A was watched (3 plays) since marking; the rest unchanged.
def fresh(ids):
    return {str(i): {"play_count": 3 if i == "Movie A" else 0, "last_played": 0, "favorite": False}
            for i in ids}
E._fresh_watch_data = fresh

# The movies are 1024 bytes each, so a 2-movie deficit is 2048 bytes.
DEFICIT_2 = 2 * 1024
DEFICIT_3 = 3 * 1024

# ── Headroom deficit for 2: A (watched) drops out, C takes its place ──────────
with tempfile.TemporaryDirectory() as td:
    paths = _seed(td)
    E.REDLINE_ONLY_MODE = False; E.HEADROOM_GB = 100; E.REDLINE_GB = None
    E._revalidate_pending_marks(DEFICIT_2)
    marked = _marked(paths)
    check("watched marked movie A was un-marked on the tick", "Movie A" not in marked)
    check("next in line C was marked in its place", "Movie C" in marked)
    check("un-watched marked movie B stayed marked", "Movie B" in marked)
    check("the marked set is sized to the 2-movie deficit", len(marked) == 2)
    # The tick also keeps the cache honest: A's fresh 3 plays are written into the
    # library snapshot, and built_at is preserved (a watch refresh, not a rescan).
    check("A's fresh plays were persisted into the library snapshot", _snap_plays(paths[0]) == 3)
    check("an unwatched movie's snapshot plays stay 0", _snap_plays(paths[1]) == 0)
    check("the snapshot's built_at is preserved (not bumped to a fresh scan)", _snap_built_at() == 1)

# ── A bigger deficit marks MORE (the set grows with the filesystem data) ───────
with tempfile.TemporaryDirectory() as td:
    paths = _seed(td)
    E.REDLINE_ONLY_MODE = False; E.HEADROOM_GB = 100; E.REDLINE_GB = None
    E._revalidate_pending_marks(DEFICIT_3)
    check("a 3-movie deficit marks 3 movies", len(_marked(paths)) == 3)

# ── Redline-only: the tick never creates delay clocks (redline deletes now) ────
with tempfile.TemporaryDirectory() as td:
    paths = _seed(td)
    E.REDLINE_ONLY_MODE = True; E.HEADROOM_GB = 0; E.REDLINE_GB = 200
    E._revalidate_pending_marks(DEFICIT_2)          # even given a deficit
    check("redline-only tick schedules no delay-clocked marks", _marked(paths) == set())

# ── Zero deficit (within limits): marks are unscheduled, the queue stays ───────
with tempfile.TemporaryDirectory() as td:
    paths = _seed(td)
    E.REDLINE_ONLY_MODE = False; E.HEADROOM_GB = 100; E.REDLINE_GB = None
    E._revalidate_pending_marks(0)
    check("within limits, the tick unschedules all marks", _marked(paths) == set())

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
