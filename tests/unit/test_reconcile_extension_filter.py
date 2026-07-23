"""reconcile_from_snapshot must honor the CURRENT MOVIE_EXTENSIONS. The snapshot
may hold rows scanned under a different extension set (e.g. .mkv removed from the
config by a hand-edit); since the reconcile re-stamps the plan as current WITHOUT a
rescan, it must drop a now-ineligible extension — otherwise a Cleanup would delete
files the new config excludes (a full Simulate skips them as bad_extension).
Regression for MOVIE_EXTENSIONS being a plan key the reconcile stamped but never
applied."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate
import db
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
E.MAX_IMDB_RATING = None
E.GRACE_PERIOD_DAYS = 0
E.SKIP_UNPLAYED_MOVIES = False
E.PROTECT_JELLYFIN_FAVORITES = False
E.REDLINE_ONLY_MODE = False
E.REDLINE_GB = None
E.MAX_LIBRARY_GB = None
E.NEAR_TIE_PTS = 0.0
E.USE_PLEX = True
E.USE_JELLYFIN = False
E.MONITOR_DIRS = ["/lib"]
E.HEADROOM_GB = 506
GB = 1_000_000_000
NOW = 1_700_000_000
DISK = {"total": 1000 * GB, "used": 500 * GB, "free": 500 * GB}
E.get_usage_info = lambda: {
    "total": DISK["total"], "used": DISK["used"], "free": DISK["free"],
    "used_gb": DISK["used"] / GB, "max_gb": DISK["total"] / GB - (E.HEADROOM_GB or 0)}


def movie(path, plays, size=2 * GB):
    return {"path": path, "title": Path(path).stem, "year": 2020, "rating": 6.0,
            "votes": 1000, "plays": plays, "users": 1, "last_played": 0,
            "added_at": 1_400_000_000, "size_gb": round(size / 1e9, 2), "size_bytes": size,
            "protected": False, "favorite": False, "excluded": False,
            "source_id": path, "jf_source_id": None, "tmdb_id": None, "section_id": "1"}


def reconcile_queue(td, exts):
    E.OUTPUT_DIR = Path(td)
    E.DB_FILE = Path(td) / "mediareducer.db"
    E.MOVIE_EXTENSIONS = set(exts)
    movies = [movie("/lib/A.mkv", 0), movie("/lib/B.mp4", 1), movie("/lib/C.mkv", 2)]
    _dbstate.seed(E.DB_FILE, {
        "code_checksum": E.code_checksum(),
        "library_snapshot": {"built_at": NOW, "monitor_dirs": E.MONITOR_DIRS, "movies": movies}})
    E.reconcile_from_snapshot(trigger="test")
    return set(db.read_pending_doc(E.DB_FILE).get("entries", {}))


with tempfile.TemporaryDirectory() as td:
    q = reconcile_queue(td, {".mkv", ".mp4"})
    check("all extensions eligible: every movie is queued",
          q == {"/lib/A.mkv", "/lib/B.mp4", "/lib/C.mkv"})

with tempfile.TemporaryDirectory() as td:
    q = reconcile_queue(td, {".mp4"})   # .mkv removed from the config since the scan
    check("removing .mkv drops the .mkv rows from the reconciled queue", q == {"/lib/B.mp4"})
    check("neither .mkv movie is left deletable by the reconcile",
          "/lib/A.mkv" not in q and "/lib/C.mkv" not in q)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
