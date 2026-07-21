"""Radarr cleanup fires on the section match, not a last-copy census: the
moment the copy in Radarr's own section is deleted, the movie is removed from
Radarr — duplicates elsewhere (a second Version in the same folder, a copy in
another section) don't keep it monitored, where Radarr would see a missing
file and re-grab it. A copy KNOWN to be in a different section never touches
Radarr (a bare folder-name match can't tell same-named duplicate folders
apart); only a row with UNKNOWN section identity falls back to asking whether
Radarr's path owns the deleted folder."""
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

E.log = lambda *a, **k: None
E.RADARR_OVERSEERR_SECTION_ID = "1"
E.RADARR_URL = "http://radarr.test"
E.RADARR_API_KEY = "key"

deletes = []
E.radarr_delete = lambda tmdb_id, title, movie=None: deletes.append((tmdb_id, title))

tmp = Path(tempfile.mkdtemp())
folder = tmp / "The Movie (2020)"
folder.mkdir()
v1080 = folder / "The Movie (2020) - 1080p.mkv"
v4k = folder / "The Movie (2020) - 2160p.mkv"
v1080.write_bytes(b"x")
v4k.write_bytes(b"y")

def candidate(path, section="1", tmdb="555"):
    return {"tmdb_id": tmdb, "title": "The Movie", "section_id": section, "path": path}

# Section copy deleted while a 4K sibling Version still exists in the same
# folder: Radarr's copy is gone, so the movie leaves Radarr anyway.
v1080.unlink()
deletes.clear()
E.cleanup_radarr(candidate(v1080))
check("section match removes from Radarr despite a sibling copy",
      deletes == [("555", "The Movie")])

# No TMDB ID: nothing to look up, cleanup skipped.
deletes.clear()
E.cleanup_radarr({"tmdb_id": None, "title": "The Movie", "section_id": "1", "path": v4k})
check("no TMDB ID skips cleanup", deletes == [])

# Radarr cleanup disabled (no section configured): never called.
E.RADARR_OVERSEERR_SECTION_ID = None
deletes.clear()
E.cleanup_radarr(candidate(v4k))
check("no configured section skips cleanup", deletes == [])
E.RADARR_OVERSEERR_SECTION_ID = "1"

# A copy KNOWN to be in another section: Radarr keeps the movie even when the
# duplicate's folder shares Radarr's folder NAME — the classic "Movies LQ"
# duplicate whose basename matches Radarr's own folder must not evict the
# movie while Radarr's real file survives.
E.radarr_lookup_movie = lambda tmdb_id, title: {"id": 9, "path": str(folder)}
deletes.clear()
E.cleanup_radarr(candidate(v4k, section="2"))
check("known different section leaves Radarr alone even on a folder-name match",
      deletes == [])

# Unknown section (e.g. a Jellyfin-sourced row) where Radarr's path does NOT
# own the deleted folder: Radarr keeps the movie.
E.radarr_lookup_movie = lambda tmdb_id, title: {"id": 9, "path": "/movies/Somewhere Else (1999)"}
deletes.clear()
E.cleanup_radarr(candidate(v4k, section=None))
check("unknown section without a folder match leaves Radarr alone", deletes == [])

# Unknown section where Radarr's folder IS the deleted folder: Radarr owned
# that file, so cleanup proceeds.
E.radarr_lookup_movie = lambda tmdb_id, title: {"id": 9, "path": str(folder)}
deletes.clear()
E.cleanup_radarr(candidate(v4k, section=None))
check("unknown section cleans up when Radarr owns the deleted folder",
      deletes == [("555", "The Movie")])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
