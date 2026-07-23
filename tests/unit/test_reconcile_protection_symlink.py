"""_refresh_snapshot_protection (the collections/favorites-change reconcile) must
keep a genuinely-protected movie protected even when the snapshot's stored path
(symlink-RESOLVED) differs from the freshly-fetched protection path (as-built) —
otherwise a symlinked library clears the flag and re-admits the movie to the
eligible queue. Regression for the exact-path compare; it now uses the same robust
_make_protection_check the deletion paths use."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None

with tempfile.TemporaryDirectory() as td:
    lib = Path(td, "lib")
    film_dir = lib / "Movies" / "Film (2020)"; film_dir.mkdir(parents=True)
    (film_dir / "film.mkv").write_bytes(b"\0" * 1024)
    other_dir = lib / "Movies" / "Other (2019)"; other_dir.mkdir(parents=True)
    (other_dir / "other.mkv").write_bytes(b"\0" * 1024)
    (lib / "PlexMovies").symlink_to(lib / "Movies", target_is_directory=True)
    E.LIBRARY_ROOT = lib
    E.MONITOR_DIRS = [str(lib / "Movies")]
    E.DB_FILE = Path(td, "mediareducer.db")

    resolved_row_path = str((lib / "Movies" / "Film (2020)" / "film.mkv"))    # snapshot form (resolved)
    protection_via_symlink = str(lib / "PlexMovies" / "Film (2020)" / "film.mkv")  # fetched form (as-built)
    other_path = str(lib / "Movies" / "Other (2019)" / "other.mkv")

    # Protection fetch reports the file through the symlinked share; favorites off.
    E.USE_PLEX = True
    E.USE_JELLYFIN = False
    E.PROTECTED_COLLECTIONS = ["Keep"]
    E.fetch_protected_paths = lambda: ({protection_via_symlink}, set(), set(), set())
    E._jellyfin_protected_items = lambda: (set(), set(), set(), set())
    E._jellyfin_favorite_paths = lambda: set()

    # Sanity: the OLD exact compare would have cleared it.
    check("baseline: resolved row path != as-built protection path",
          resolved_row_path not in {protection_via_symlink})

    rows = [
        {"path": resolved_row_path, "protected": 0, "favorite": 0, "title": "Film"},
        {"path": other_path, "protected": 0, "favorite": 0, "title": "Other"},
    ]
    E._refresh_snapshot_protection(rows)

    check("symlinked-protected movie stays protected after the reconcile refresh",
          rows[0]["protected"] is True)
    check("an unprotected movie stays unprotected", rows[1]["protected"] is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
