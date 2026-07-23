"""The no-rescan delete paths (redline/manual fast path, debug-cleanup preview,
15-minute upkeep) must recognise a protected/favorited movie even when its
queue-key path differs from the freshly-fetched protection path by a symlink,
mount prefix, or case — the normal dual-server / Unraid user-share case.

Regression for the exact-string protection match (`str(key) in {paths}`) that
missed those and could DELETE a protected movie. `_make_protection_check` now
mirrors the full scan: the _match_keys SET (as-built + symlink-resolved +
lowercased) plus Plex rating_key / Jellyfin id / TMDB id."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

with tempfile.TemporaryDirectory() as td:
    lib = Path(td, "lib")
    film_dir = lib / "Movies" / "Film (2020)"
    film_dir.mkdir(parents=True)
    (film_dir / "film.mkv").write_bytes(b"\0" * 1024)
    other_dir = lib / "Movies" / "Other (2019)"
    other_dir.mkdir(parents=True)
    (other_dir / "other.mkv").write_bytes(b"\0" * 1024)
    # PlexMovies -> Movies: the SAME file reachable through a symlinked share, the
    # way Plex and Jellyfin often mount one library at two in-container paths.
    (lib / "PlexMovies").symlink_to(lib / "Movies", target_is_directory=True)
    E.LIBRARY_ROOT = lib

    queue_key = str(lib / "Movies" / "Film (2020)" / "film.mkv")            # scan/queue form
    fav_via_symlink = str(lib / "PlexMovies" / "Film (2020)" / "film.mkv")  # protection form
    other_key = str(lib / "Movies" / "Other (2019)" / "other.mkv")

    # The OLD code matched exactly like this — and missed the symlinked path:
    check("baseline: exact-string match MISSES the symlinked protection path",
          queue_key not in {fav_via_symlink})

    # 1. A favorite reached via the symlinked path still protects the queue key.
    m = E._make_protection_check(set(), set(), set(), set(), set(), set(), {fav_via_symlink})
    check("symlinked favorite path protects the queue key", m(queue_key) is True)
    check("an unrelated movie is NOT protected", m(other_key) is False)

    # 2. Plex rating_key (snapshot source_id) protects with no path overlap at all.
    m2 = E._make_protection_check(set(), {"555"}, set(), set(), set(), set(), set())
    check("Plex rating_key match protects", m2(queue_key, None, {"source_id": "555"}) is True)
    check("non-matching rating_key does not protect", m2(queue_key, None, {"source_id": "999"}) is False)

    # 3. Jellyfin item id (snapshot jf_source_id).
    m3 = E._make_protection_check(set(), set(), set(), {"jf-1"}, set(), set(), set())
    check("Jellyfin id match protects", m3(queue_key, None, {"jf_source_id": "jf-1"}) is True)

    # 4. TMDB id from either the queue entry or the snapshot row.
    m4 = E._make_protection_check(set(), set(), {"12345"}, set(), set(), {"12345"}, set())
    check("tmdb match via queue entry protects", m4(queue_key, {"tmdb_id": "12345"}, None) is True)
    check("tmdb match via snapshot protects", m4(queue_key, None, {"tmdb_id": "12345"}) is True)
    check("non-matching tmdb does not protect", m4(queue_key, {"tmdb_id": "1"}, None) is False)

    # 5. A plain exact (non-symlinked) protection path still matches — no regression.
    m5 = E._make_protection_check({queue_key}, set(), set(), set(), set(), set(), set())
    check("exact protection path still protects", m5(queue_key) is True)

    # 6. None inputs (a stubbed 'nothing protected' fetch) don't crash and match nothing.
    m6 = E._make_protection_check([], None, None, None, None, None, None)
    check("None/empty fetches are safe and protect nothing", m6(queue_key) is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
