"""Sample build vs the IMDb dataset at every gate combination:
  A. balance 0 + dataset ON DISK  -> annotate from it, NO download
  B. balance 0 + no dataset       -> unrated, NO download
  C. IMDb needed                  -> download path (ensure called), annotated
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pathlib import Path
import engine

import tempfile
SCRATCH = Path(tempfile.mkdtemp(prefix="mr-annotate-"))
SCRATCH.mkdir(exist_ok=True)
TSV = SCRATCH / "title.ratings.tsv"

engine.USE_PLEX = True
engine.USE_JELLYFIN = False
engine.MONITOR_DIRS = [Path("/lib")]
engine.IMDB_RATINGS_PATH = TSV

plex_row = {"title": "Movie A", "year": 2001, "rating_key": "201",
            "play_count": 0, "last_played": 0, "added_at": 1_400_000_000,
            "file_size": 4_000_000_000}

captured = {}
calls = {"ensure": 0}
engine.validate_connections = lambda: True
engine._iter_tautulli_random_rows = lambda page_len=25: iter([dict(plex_row)])
engine._write_sample_pool_file = lambda m: captured.__setitem__("movies", m)
engine._quick_sample_file_path = lambda row, allow_api_lookup=True: (Path("/lib/A.mkv"), False)
engine.is_under_monitored_dir = lambda p: True
engine._quick_sample_row_meta = lambda row: ("tt0000001", False)
engine.load_cache = lambda: {"movies": {}}

def fake_ensure():
    calls["ensure"] += 1
    TSV.write_text("tconst\taverageRating\tnumVotes\ntt0000001\t7.3\t123456\n", encoding="utf-8")
engine._ensure_imdb_dataset_for_sample = fake_ensure

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

def build():
    captured.clear()
    engine.build_quick_sample_pool(target=10)
    return captured["movies"][0]

# A: balance 0, dataset on disk
engine.imdb_dataset_needed = lambda: False
TSV.write_text("tconst\taverageRating\tnumVotes\ntt0000001\t7.3\t123456\n", encoding="utf-8")
calls["ensure"] = 0
m = build()
check("A: annotated from on-disk dataset (rating 7.3)", m["rating"] == 7.3 and m["votes"] == 123456)
check("A: no download attempted", calls["ensure"] == 0)

# B: balance 0, no dataset anywhere
TSV.unlink(missing_ok=True)
gz = TSV.with_name(TSV.name + ".gz")
gz.unlink(missing_ok=True)
calls["ensure"] = 0
m = build()
check("B: unrated without dataset", m["rating"] is None)
check("B: no download attempted", calls["ensure"] == 0)

# C: IMDb needed -> ensure (download) path runs and ratings load
engine.imdb_dataset_needed = lambda: True
calls["ensure"] = 0
m = build()
check("C: download path invoked when needed", calls["ensure"] == 1)
check("C: annotated after download", m["rating"] == 7.3)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
