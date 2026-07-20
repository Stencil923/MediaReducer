"""Radarr cleanup must not forget a movie while another physical copy is still
on disk. A single Plex item can hold multiple files ("Versions") that the scan
registers as one path, so the last-copy check also scans the deleted file's own
folder for a surviving playable file before removing the movie from Radarr."""
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

def candidate(path):
    return {"tmdb_id": "555", "title": "The Movie", "section_id": "1", "path": path}

# Scan only registered ONE of the two physical files (the multi-version gap).
section_map = {"555": {v1080.resolve()}}

# Delete the 1080p file, then run cleanup — the 4K sibling still exists in the
# same folder, so Radarr must NOT be told to forget the movie.
v1080.unlink()
deletes.clear()
E.cleanup_radarr(candidate(v1080), section_map)
check("sibling copy in folder blocks Radarr removal", deletes == [])

# Now the 4K copy is also gone — this really was the last copy, so cleanup runs.
v4k.unlink()
deletes.clear()
E.cleanup_radarr(candidate(v4k), {"555": {v4k.resolve()}})
check("true last copy triggers Radarr removal", deletes == [("555", "The Movie")])

# A non-movie sibling (subtitle/nfo) must NOT be treated as a surviving copy.
folder2 = tmp / "Other (2019)"
folder2.mkdir()
mov = folder2 / "Other (2019).mkv"
srt = folder2 / "Other (2019).srt"
mov.write_bytes(b"x")
srt.write_bytes(b"sub")
mov.unlink()
deletes.clear()
E.cleanup_radarr({"tmdb_id": "777", "title": "Other", "section_id": "1", "path": mov},
                 {"777": {mov.resolve()}})
check("non-movie sibling does not block removal", deletes == [("777", "Other")])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
