"""The candidate stage (build_candidates) under each server configuration —
Plex-only, Jellyfin-only, and both enabled — the branches that consume merged
rows and that NO full run otherwise exercises (the Plex-only e2e never sets
USE_JELLYFIN). This is where Jellyfin protection, the unmerged-twin skip, and
the cross-server identity check actually gate deletion (engine.py ~3758-3972).

Driven hermetically: a real temp /library with real movie files (the resolver
and .exists()/.stat() checks need them), canned source fetchers, and IMDb
disabled (100% watch history, no cutoff) so no dataset is needed. build_candidates
runs for real end to end and returns (candidates, stats, total)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-cand-out.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
E.log_stage = lambda *a, **k: None
E.log_blank = lambda *a, **k: None
E.emit_progress = lambda *a, **k: None

# ── A real /library with real movie files ────────────────────────────────────
lib = Path(tempfile.mkdtemp(prefix="mr-cand-lib."))
def make(rel):
    p = lib / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 1024)
    return p
f_keep   = make("Movies/Keep (2020)/keep.mkv")       # eligible on either server
f_prot   = make("Movies/Protected (2019)/prot.mkv")   # in a protection set
f_fav    = make("Movies/Fav (2018)/fav.mkv")          # a Jellyfin favorite
f_shared = make("Movies/Shared (2021)/shared.mkv")    # present on BOTH servers
f_twin_p = make("A/Twin (2017)/twin.mkv")             # Plex side of a near-miss twin
f_twin_j = make("B/Twin (2017)/twin.mkv")             # Jellyfin side (paths diverge)

E.LIBRARY_ROOT = lib
E.MONITOR_DIRS = [str(lib)]
E.MOVIE_EXTENSIONS = {".mkv"}
E.QUALITY_WEIGHT = 0.0          # 100% watch history → IMDb dataset never needed
E.MAX_IMDB_RATING = None
E.GRACE_PERIOD_DAYS = 0
E.SKIP_UNPLAYED_MOVIES = False
E.RADARR_OVERSEERR_SECTION_ID = None

# The path resolver is real, but our rows already carry real /library paths.
E.extract_file_path = lambda item, quiet=True: Path(item["file"]) if item.get("file") else None

_PLEX = []      # Tautulli-shaped rows for this case
_JELLY = []     # Jellyfin-shaped rows for this case
_PLEX_META = {} # rating_key -> {protected, tmdb_id, imdb_id}
_PLEX_PROT = (set(), set(), set(), set())  # fetch_protected_paths() return

def _reset(use_plex, use_jellyfin, *, protect_favorites=False):
    """Fresh, isolated engine state for one combo."""
    E.USE_PLEX = use_plex
    E.USE_JELLYFIN = use_jellyfin
    E.PROTECT_JELLYFIN_FAVORITES = protect_favorites
    E.PROTECTED_COLLECTIONS = []
    E.JELLYFIN_PROTECTED_COLLECTIONS = set()
    E._metadata_cache.clear()
    E._JELLYFIN_PROTECTED_MATCH_KEYS = set()
    E._JELLYFIN_PROTECTED_IMDB_IDS = set()
    E._JELLYFIN_PROTECTED_TMDB_IDS = set()
    E._JELLYFIN_IDS_BY_MATCH_KEY = {}
    E.OUTPUT_DIR = Path(_OUT)
    E.CACHE_FILE = Path(_OUT) / "cache.json"
    E.CACHE_FILE.unlink(missing_ok=True)
    E.get_all_movies_from_tautulli = lambda: [dict(r) for r in _PLEX]
    E.get_all_movies_from_jellyfin = lambda: [dict(r) for r in _JELLY]
    E.fetch_protected_paths = lambda: _PLEX_PROT
    E.fetch_movie_metadata = lambda rk, title=None: dict(
        _PLEX_META.get(rk, {"protected": False, "tmdb_id": None, "imdb_id": None}))

def plex_row(rk, f, **kw):
    r = {"rating_key": rk, "title": Path(f).stem, "file": str(f),
         "play_count": 3, "last_played": 1_600_000_000, "added_at": 1_500_000_000,
         "_section_id": "1"}
    r.update(kw); return r

def jf_row(jid, f, **kw):
    r = {"rating_key": f"jf:{jid}", "title": Path(f).stem, "file": str(f),
         "play_count": 2, "last_played": 1_650_000_000, "added_at": 1_400_000_000,
         "protected": False, "_jf_users": 1, "_jf_favorite": False,
         "tmdb_id": None, "imdb_id": None, "video_resolution": "1080", "bitrate": 8000}
    r.update(kw); return r

def sources(c):
    return sorted({x["source"] for x in c})

# ══ Plex-only ════════════════════════════════════════════════════════════════
# Two Plex movies; one sits in a protected collection (path returned by the real
# fetch_protected_paths path). The protected one is excluded; the other is a
# Plex-sourced candidate.
_PLEX = [plex_row("100", f_keep), plex_row("101", f_prot)]
_JELLY = []
_PLEX_META = {"100": {"protected": False, "tmdb_id": "10", "imdb_id": "tt10"},
              "101": {"protected": False, "tmdb_id": "11", "imdb_id": "tt11"}}
_PLEX_PROT = ({str(f_prot)}, set(), set(), set())
_reset(True, False)
cands, stats, total = E.build_candidates()
check("Plex-only: the unprotected movie is a candidate",
      len(cands) == 1 and cands[0]["title"] == "keep")
check("Plex-only: candidates are Plex-sourced", sources(cands) == ["plex"])
check("Plex-only: the protected-collection movie is excluded", stats["protected"] == 1)

# ══ Jellyfin-only ════════════════════════════════════════════════════════════
# Three Jellyfin movies: one eligible, one protected (BoxSet → row protected),
# one favorited. With favorites protection ON, the favorite is a hard skip.
_PLEX = []
_PLEX_META = {}
_PLEX_PROT = (set(), set(), set(), set())
_JELLY = [jf_row("A", f_keep, tmdb_id="20", imdb_id="tt20"),
          jf_row("B", f_prot, protected=True),
          jf_row("C", f_fav, _jf_favorite=True)]
_reset(False, True, protect_favorites=True)
cands, stats, total = E.build_candidates()
check("Jellyfin-only: the eligible movie is a candidate",
      len(cands) == 1 and cands[0]["title"] == "keep")
check("Jellyfin-only: candidates are Jellyfin-sourced", sources(cands) == ["jellyfin"])
check("Jellyfin-only: a protected-BoxSet movie is excluded", stats["protected"] == 1)
check("Jellyfin-only: a favorited movie is excluded when favorites protection is on",
      stats["jellyfin_favorite"] == 1)
# Same movies, favorites protection OFF → the favorite becomes eligible.
_reset(False, True, protect_favorites=False)
cands, stats, total = E.build_candidates()
check("Jellyfin-only: the favorite is eligible once favorites protection is off",
      stats["jellyfin_favorite"] == 0 and {c["title"] for c in cands} == {"keep", "fav"})

# ══ Both enabled ═════════════════════════════════════════════════════════════
# The SAME file on both servers must collapse to ONE candidate with summed
# plays; a matching-identity pair is fine, but a provider-id CONFLICT on a shared
# file must be skipped as an identity mismatch, never deleted on a guess.
_PLEX = [plex_row("200", f_shared)]
_PLEX_META = {"200": {"protected": False, "tmdb_id": "30", "imdb_id": "tt30"}}
_PLEX_PROT = (set(), set(), set(), set())
_JELLY = [jf_row("D", f_shared, tmdb_id="30", imdb_id="tt30", play_count=5)]
_reset(True, True)
cands, stats, total = E.build_candidates()
check("Both: the same file on both servers collapses to ONE candidate", len(cands) == 1)
check("Both: cross-server plays are summed on the merged candidate",
      cands and E.parse_int(cands[0]["play_count"], 0) == 3 + 5)

# Identity conflict on a shared file: Plex says tt30, Jellyfin says tt99 for the
# same path → skip, flag the run completed-with-errors, delete nothing.
_PLEX = [plex_row("300", f_shared)]
_PLEX_META = {"300": {"protected": False, "tmdb_id": "30", "imdb_id": "tt30"}}
_JELLY = [jf_row("E", f_shared, tmdb_id="99", imdb_id="tt99")]
_reset(True, True)
# The Jellyfin fetch normally records this map; with the fetch stubbed, seed it
# the way get_all_movies_from_jellyfin would (real path -> Jellyfin's identity).
E._JELLYFIN_IDS_BY_MATCH_KEY = {k: {"imdb": "tt99", "tmdb": "99", "title": "shared"}
                                for k in E._match_keys(str(f_shared))}
cands, stats, total = E.build_candidates()
check("Both: a provider-id conflict on a shared file is skipped, not deleted",
      len(cands) == 0 and stats["identity_mismatch"] >= 1)

# Near-miss twin: same folder+filename, divergent paths → they don't merge, but
# the Jellyfin twin is flagged and skipped (never double-counted or deleted).
_PLEX = [plex_row("400", f_twin_p)]
_PLEX_META = {"400": {"protected": False, "tmdb_id": "40", "imdb_id": "tt40"}}
_JELLY = [jf_row("F", f_twin_j, tmdb_id="40", imdb_id="tt40")]
_reset(True, True)
cands, stats, total = E.build_candidates()
check("Both: an unmerged Plex/Jellyfin twin is flagged and not double-counted",
      stats["identity_mismatch"] >= 1
      and sum(1 for c in cands if c["title"] == "twin") <= 1)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
