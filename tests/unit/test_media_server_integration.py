"""End-to-end HTTP integration for the three media-server integrations, driven
through the engine's REAL request functions against a live local mock — the
layer the monkeypatched unit tests (test_protection_failclosed, test_radarr_
cleanup) don't reach: URL construction, auth headers, response parsing, and
path resolution.

  • Plex protected collections  → fetch_protected_paths() returns the movie
  • Jellyfin favorites          → _jellyfin_favorite_paths() returns the movie
  • Jellyfin protected BoxSets   → _jellyfin_protected_items() returns the movie
  • Radarr cleanup              → cleanup_radarr() actually DELETEs in Radarr
  • fail-closed                 → an unreachable Plex / a missing BoxSet aborts
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mocks"))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E
from mock_services import start_mock_services

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None

# A disposable /library with three real movie files — the resolver only returns
# paths that exist on disk, so the mock must report files that are really here.
lib = Path(tempfile.mkdtemp(prefix="mr-lib."))
def make(rel):
    p = lib / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p
prot_file = make("Movies/Protected Film (2020)/film.mkv")
fav_file  = make("Movies/Fav Film (2019)/fav.mkv")
box_file  = make("Movies/Boxset Film (2022)/box.mkv")
rad_file  = make("Movies LQ/Radarr Film (2021)/radarr.mkv")
E.LIBRARY_ROOT = lib

# The media servers see the same files under a different container root (/data),
# which the resolver maps back under LIBRARY_ROOT by the longest existing tail.
mock = start_mock_services(
    plex_collections={"Keepers": [
        {"ratingKey": "9001", "file": "/data/Movies/Protected Film (2020)/film.mkv"}]},
    jellyfin_favorites=["/data/Movies/Fav Film (2019)/fav.mkv"],
    jellyfin_boxsets={"Keepers JF": ["/data/Movies/Boxset Film (2022)/box.mkv"]},
    radarr_movies=[{"id": 42, "tmdbId": 555,
                    "path": "/data/Movies LQ/Radarr Film (2021)",
                    "rootFolderPath": "/data/Movies LQ"}],
)

try:
    # ── Plex protected collections (real HTTP: sections → collections → children)
    E.PLEX_URL = mock.base_url
    E.PLEX_TOKEN = "plex-token"
    E.PROTECTED_COLLECTIONS = ["Keepers"]
    E.RUN_MODE = "headroom"
    paths, keys, imdb_ids, tmdb_ids = E.fetch_protected_paths()
    check("Plex protection resolves the collection movie's real path",
          str(prot_file) in paths)
    check("Plex protection keeps the rating key as a secondary match", "9001" in keys)
    check("the engine actually queried Plex sections + collection children",
          ("GET", "/library/sections") in mock.calls
          and any(c[1].endswith("/children") for c in mock.calls))

    # ── Jellyfin favorites (real HTTP: /Users → /Users/{id}/Items IsFavorite)
    E.USE_JELLYFIN = True
    E.PROTECT_JELLYFIN_FAVORITES = True
    E.JELLYFIN_URL = mock.base_url
    E.JELLYFIN_API_KEY = "jf-key"
    fav_paths = E._jellyfin_favorite_paths()
    check("Jellyfin favorite resolves to the real file path", str(fav_file) in fav_paths)

    # ── Jellyfin protected BoxSets (real HTTP: /Users → BoxSet listing →
    #    member enumeration across the version-varying child endpoints)
    E.JELLYFIN_PROTECTED_COLLECTIONS = {"Keepers JF"}
    _ids, prot_paths, _imdb, _tmdb = E._jellyfin_protected_items()
    check("Jellyfin BoxSet protection resolves the member's real path",
          str(box_file) in prot_paths)
    # A configured BoxSet that the server doesn't have must abort a deleting run
    # (fail closed — never run a protection off silently because it went missing).
    E.JELLYFIN_PROTECTED_COLLECTIONS = {"Renamed Away"}
    try:
        E._jellyfin_protected_items()
        check("a missing Jellyfin BoxSet aborts a deleting run", False)
    except SystemExit:
        check("a missing Jellyfin BoxSet aborts a deleting run", True)
    E.JELLYFIN_PROTECTED_COLLECTIONS = set()

    # ── Radarr cleanup (real HTTP: lookup by tmdbId → DELETE the movie)
    E.RADARR_URL = mock.base_url
    E.RADARR_API_KEY = "radarr-key"
    E.RADARR_OVERSEERR_SECTION_ID = "1"
    E.cleanup_radarr({"tmdb_id": 555, "title": "Radarr Film",
                      "section_id": "1", "path": str(rad_file)})
    check("a section-match deletion actually DELETEs the movie in Radarr",
          mock.deletes == ["42"])
    # An off-section copy whose section is KNOWN must not touch Radarr.
    mock.deletes.clear()
    E.cleanup_radarr({"tmdb_id": 555, "title": "Radarr Film",
                      "section_id": "2", "path": str(rad_file)})
    check("a known different-section deletion leaves Radarr alone",
          mock.deletes == [])

    # ── Fail-closed: Plex unreachable while protection is configured aborts a
    #    deleting run (the real HTTP error path, not a monkeypatched return).
    E.PLEX_URL = "http://127.0.0.1:9"   # dead port
    E.RUN_MODE = "headroom"
    try:
        E.fetch_protected_paths()
        check("unreachable Plex aborts a deleting run", False)
    except SystemExit:
        check("unreachable Plex aborts a deleting run", True)
finally:
    mock.stop()

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
