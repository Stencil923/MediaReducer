"""Config-save reconcile (reconcile_from_snapshot): rebuild the marked & eligible
queue from the stored library snapshot under new filtering/scoring/threshold config
— NO library walk, and NO media-server fetch unless a protection source changed.

Proves the properties the feature promises:
  • scores + eligibility are recomputed from the snapshot's stored facts, both
    directions (turning a filter off RE-ADMITS movies, on removes them);
  • the marked prefix tracks the current space target;
  • a still-marked movie KEEPS its delay clock (marked_at), a newly-marked one gets
    a fresh clock, and a now-ineligible one drops off;
  • the stored `excluded` flag (an identity mismatch) is never re-admitted;
  • the refetch path updates protected/favorite facts from injected server lookups.

Fully hermetic: a temp DB, a stubbed disk read, and injected fetchers — no network,
no filesystem walk."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate
_OUT = tempfile.mkdtemp(prefix="mr-reconcile.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
import db
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
E.OUTPUT_DIR = Path(_OUT)
E.DB_FILE = Path(_OUT) / "mediareducer.db"
GB = 1_000_000_000
NOW = 1_700_000_000
SEED_TS = NOW - 5 * 86400          # an old delay clock, to prove preservation

# Deterministic scoring: 100% watch history, no IMDb, wide staleness window, so a
# movie's score is a clean function of its play count (fewer plays → deleted first).
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
E.DELETE_DELAY_DAYS = 3
E.USE_PLEX = True
E.USE_JELLYFIN = False
E.MONITOR_DIRS = ["/lib"]
# Headroom target so the deficit = HEADROOM_GB - free = 506 - 500 = 6 GB → 3 marks.
E.HEADROOM_GB = 506

# Deterministic disk: 1000 GB total, 500 used / 500 free.
DISK = {"total": 1000 * GB, "used": 500 * GB, "free": 500 * GB}
E.get_usage_info = lambda: {
    "total": DISK["total"], "used": DISK["used"], "free": DISK["free"],
    "used_gb": DISK["used"] / GB, "max_gb": DISK["total"] / GB - (E.HEADROOM_GB or 0)}

CHECKSUM = E.code_checksum()   # seed with the running checksum so ensure_code_current is a no-op

def movie(path, plays, *, size=2 * GB, added=1_400_000_000, last=0, users=1,
          rating=6.0, votes=1000, protected=False, favorite=False, excluded=False,
          tmdb=None, section="1", jf=None):
    return {"path": path, "title": Path(path).stem, "year": 2020, "rating": rating,
            "votes": votes, "plays": plays, "users": users, "last_played": last,
            "added_at": added, "size_gb": round(size / 1e9, 2), "size_bytes": size,
            "protected": protected, "favorite": favorite, "excluded": excluded,
            "source_id": (jf or path), "jf_source_id": jf,
            "tmdb_id": tmdb, "section_id": section}

def seed(movies, queue=None):
    doc = {"code_checksum": CHECKSUM,
           "library_snapshot": {"built_at": NOW, "monitor_dirs": E.MONITOR_DIRS, "movies": movies}}
    if queue is not None:
        doc["pending"] = {"schema": 1, "entries": queue}
    _dbstate.seed(E.DB_FILE, doc)

def qentry(marked_at=None, score=0.0):
    return {"title": "m", "score": score, "size_bytes": 2 * GB,
            "marked_at": marked_at, "tmdb_id": None, "section_id": "1"}

def queue_now():
    return db.read_pending_doc(E.DB_FILE).get("entries", {})

def marked(entries):
    return {k for k, e in entries.items() if e.get("marked_at") is not None}

P = [f"/lib/M{n}.mkv" for n in range(5)]

# ══ 1. Basic reconcile: eligible list + target-sized marks + marked_at preserved ══
# 5 movies, 2 GB each, plays 0..4 → deletion order M0,M1,M2,M3,M4 (fewest plays
# first). 6 GB deficit → mark M0,M1,M2. Seed M1 already marked with an OLD clock.
movies = [movie(P[n], plays=n) for n in range(5)]
seed(movies, queue={P[1]: qentry(marked_at=SEED_TS)})
E.reconcile_from_snapshot(trigger="test")
q = queue_now()
check("every eligible movie is in the queue", set(q) == set(P))
check("the target-covering prefix (3 movies) is marked", len(marked(q)) == 3)
check("the marked set is the 3 lowest-scored (fewest plays)",
      marked(q) == {P[0], P[1], P[2]})
check("a still-marked movie KEEPS its delay clock",
      q[P[1]]["marked_at"] == SEED_TS)
check("a newly-marked movie gets a FRESH clock (not the old seed, not None)",
      q[P[0]]["marked_at"] not in (None, SEED_TS)
      and q[P[2]]["marked_at"] not in (None, SEED_TS))
check("movies beyond the target stay eligible-but-unmarked",
      q[P[3]]["marked_at"] is None and q[P[4]]["marked_at"] is None)
check("scores were recomputed onto the queue",
      all(isinstance(q[p]["score"], (int, float)) for p in P))

# ══ 2. Turn a filter ON removes movies; turning it OFF re-admits them ═════════════
# Skip-unplayed ON → the 0-play movie (M0) becomes ineligible and drops off.
E.SKIP_UNPLAYED_MOVIES = True
E.reconcile_from_snapshot(trigger="test")
q = queue_now()
check("turning skip-unplayed ON drops the 0-play movie from the queue", P[0] not in q)
check("the still-eligible movies remain", set(q) == {P[1], P[2], P[3], P[4]})
# Back OFF → the 0-play movie is re-admitted with NO rescan (it was in the snapshot).
E.SKIP_UNPLAYED_MOVIES = False
E.reconcile_from_snapshot(trigger="test")
check("turning skip-unplayed OFF re-admits the 0-play movie", P[0] in queue_now())

# ══ 3. A stored fact excludes a movie: protected / favorite ══════════════════════
prot = [movie(P[0], 0, protected=True), movie(P[1], 1), movie(P[2], 2)]
seed(prot)
E.reconcile_from_snapshot(trigger="test")
check("a protected movie is excluded from the queue", P[0] not in queue_now())

fav = [movie(P[0], 0, favorite=True), movie(P[1], 1)]
seed(fav)
E.PROTECT_JELLYFIN_FAVORITES = True
E.reconcile_from_snapshot(trigger="test")
check("a favorite is excluded when favorites protection is ON", P[0] not in queue_now())
E.PROTECT_JELLYFIN_FAVORITES = False
E.reconcile_from_snapshot(trigger="test")
check("the favorite is eligible again once favorites protection is OFF", P[0] in queue_now())

# ══ 4. The `excluded` flag (identity mismatch) is never re-admitted ══════════════
exc = [movie(P[0], 0, excluded=True), movie(P[1], 1)]
seed(exc)
E.reconcile_from_snapshot(trigger="test")
check("an `excluded` (identity-mismatch) movie is never eligible", P[0] not in queue_now())

# ══ 5. A threshold change re-sizes the marked set (no eligibility change) ═════════
movies = [movie(P[n], plays=n) for n in range(5)]
seed(movies)
E.HEADROOM_GB = 504          # deficit 4 GB → 2 marks
E.reconcile_from_snapshot(trigger="test")
check("a smaller headroom target marks fewer movies", len(marked(queue_now())) == 2)
E.HEADROOM_GB = 508          # deficit 8 GB → 4 marks
E.reconcile_from_snapshot(trigger="test")
check("a larger headroom target marks more movies", len(marked(queue_now())) == 4)
E.HEADROOM_GB = 506

# ══ 6. The refetch path updates protected/favorite facts from server lookups ═════
# The snapshot says nothing is protected; the injected lookup says M0's path is now
# in a protected collection → after a refetch reconcile it's excluded, and the
# refreshed fact is persisted to the snapshot.
movies = [movie(P[0], 0), movie(P[1], 1), movie(P[2], 2)]
seed(movies)
E.fetch_protected_paths = lambda: ({P[0]}, set(), set(), set())
E._jellyfin_protected_items = lambda: (set(), set(), set(), set())
E._jellyfin_favorite_paths = lambda: set()
E.reconcile_from_snapshot(trigger="collections", refetch_protection=True)
check("a refetch reconcile excludes a newly-protected movie", P[0] not in queue_now())
with db.connect(E.DB_FILE) as conn:
    _snap = {m["path"]: m for m in db.read_snapshot(conn)["movies"]}
check("the refreshed protection fact is persisted to the snapshot",
      _snap[P[0]]["protected"] is True)

# ══ 7. No snapshot → the reconcile is a safe no-op ═══════════════════════════════
_dbstate.reset(E.DB_FILE)
seed([])                      # empty snapshot
E.reconcile_from_snapshot(trigger="test")
check("an empty snapshot reconciles to an empty queue (no crash)", queue_now() == {})

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
