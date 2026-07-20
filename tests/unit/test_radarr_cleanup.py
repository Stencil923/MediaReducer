"""Radarr cleanup fires on the section match, not a last-copy census: the
moment the copy in Radarr's own section is deleted, the movie is removed from
Radarr — duplicates elsewhere (a second Version in the same folder, a copy in
another section) don't keep it monitored, where Radarr would see a missing
file and re-grab it. Off-section deletions still never touch Radarr unless
Radarr's own path owns the deleted folder."""
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

# Off-section deletion where Radarr's path does NOT own the deleted folder:
# Radarr keeps the movie (its copy is elsewhere and untouched).
E.radarr_lookup_movie = lambda tmdb_id, title: {"id": 9, "path": "/movies/Somewhere Else (1999)"}
deletes.clear()
E.cleanup_radarr(candidate(v4k, section="2"))
check("off-section copy leaves Radarr alone", deletes == [])

# Off-section deletion where Radarr's folder IS the deleted folder (section
# metadata missing/mismatched): Radarr owned that file, so cleanup proceeds.
E.radarr_lookup_movie = lambda tmdb_id, title: {"id": 9, "path": str(folder)}
deletes.clear()
E.cleanup_radarr(candidate(v4k, section="2"))
check("Radarr-owned folder cleans up even without a section match",
      deletes == [("555", "The Movie")])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
