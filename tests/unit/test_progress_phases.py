"""Per-stage progress bar: each step fills 0→100 once. The Plex+Jellyfin
path-resolution loop must report under the indeterminate "library" step, not
as denominatored "scanning" progress — otherwise the Scanning bar fills twice
(once for resolution, once for the real candidate scan)."""
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

calls = []
E.emit_progress = lambda **f: calls.append(f)
E._QUIET_PROGRESS = False

# Force the merge path (both servers), with Plex rows missing 'file' so the
# path-resolution loop does real work and emits progress.
E.USE_PLEX = True
E.USE_JELLYFIN = True
E.get_all_movies_from_tautulli = lambda: [
    {"rating_key": str(1000 + i), "title": f"Movie {i}"} for i in range(250)
]
E.get_all_movies_from_jellyfin = lambda: [
    {"rating_key": f"jf:{i}", "title": f"JF {i}", "file": f"/library/movies/J{i}/J{i}.mkv"} for i in range(5)
]
E._tag_jellyfin_metadata = lambda r: r
E.extract_file_path = lambda row, quiet=False: Path(f"/library/movies/{row.get('title', 'X').replace(' ', '')}/f.mkv")
E._match_keys = lambda p: {str(p)}

merged = E.get_all_movies()
resolve = [c for c in calls if str(c.get("message", "")).startswith("Resolving")]
scanning_denominatored = [c for c in calls if c.get("phase") == "scanning" and "total" in c]

check("merge produced rows", len(merged) > 0)
check("path resolution emitted progress", len(resolve) > 0)
check("resolution reports under the library step, not scanning",
      all(c.get("phase") == "library" for c in resolve))
check("resolution is indeterminate (no scanned/total denominator)",
      all("total" not in c and "scanned" not in c for c in resolve))
check("no denominatored scanning fill during the merge — the scan loop is the only one",
      len(scanning_denominatored) == 0)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
