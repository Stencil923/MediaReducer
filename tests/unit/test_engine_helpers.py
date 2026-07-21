"""Engine internals that no scenario test exercises directly: the Tautulli
intra-source dedup, the config coercion helpers that turn hand-edited JSON into
safe values, compute_config_hash's cache-invalidation surface, and the IMDb
ratings pipeline (bounded decompression + TSV parsing).

These are small, load-bearing, and hostile-input facing — a wrong coercion
persists a bad config, a wrong dedup double-counts plays, an unbounded gunzip is
a decompression bomb, and a lax TSV parser skews every deletion score. All run
hermetically (monkeypatched API, in-memory gzip, temp files)."""
import gzip
import io
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
E.emit_progress = lambda *a, **k: None

# ── Tautulli intra-source dedup (same file across sections) ───────────────────
# Two movie sections; the same physical file appears in both. The merged row
# keeps the HIGHEST play_count and MOST-RECENT last_played, and the Radarr
# section id wins if EITHER copy was in it (so cleanup still finds the movie).
E.RADARR_OVERSEERR_SECTION_ID = "2"
_pages = {
    "1": [{"file": "/m/Dup (2020)/dup.mkv", "play_count": 5, "last_played": 100, "rating_key": "a"},
          {"file": "/m/Solo (2019)/solo.mkv", "play_count": 1, "last_played": 10, "rating_key": "b"}],
    "2": [{"file": "/m/Dup (2020)/dup.mkv", "play_count": 2, "last_played": 999, "rating_key": "c"}],
}
E.get_movie_section_ids = lambda: ["1", "2"]
def _fake_tautulli(cmd, **params):
    if cmd == "get_library_media_info":
        sid = str(params.get("section_id"))
        start = int(params.get("start", 0))
        return {"data": _pages.get(sid, [])[start:start + int(params.get("length", 1000))]}
    return {}
E.tautulli_api = _fake_tautulli

merged = {r.get("file"): r for r in E.get_all_movies_from_tautulli()}
check("a file in two sections collapses to one row", len(merged) == 2)
dup = merged["/m/Dup (2020)/dup.mkv"]
check("dedup keeps the HIGHEST play_count", E.parse_int(dup["play_count"], 0) == 5)
check("dedup keeps the MOST-RECENT last_played", E.parse_int(dup["last_played"], 0) == 999)
check("dedup preserves the Radarr section when either copy is in it",
      str(dup["_section_id"]) == "2")
check("a single-section movie passes through untouched",
      E.parse_int(merged["/m/Solo (2019)/solo.mkv"]["play_count"], 0) == 1)

# ── Config coercion helpers ──────────────────────────────────────────────────
E.CONFIG_ERRORS = []
check("_coerce_config_number parses an int-valued float to int",
      E._coerce_config_number("5.0", "X") == 5)
check("_coerce_config_number keeps a real fractional value",
      E._coerce_config_number("2.5", "X") == 2.5)
check("_coerce_config_number rejects non-numbers to the default",
      E._coerce_config_number("abc", "X", default=7) == 7 and "X must be a number." in E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("_coerce_config_number enforces min",
      E._coerce_config_number("-1", "X", min_value=0, default=0) == 0 and E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("_coerce_config_number allow_none maps blank to None",
      E._coerce_config_number("", "X", allow_none=True) is None)
# The scripted-POST non-finite vector: a hand-edited Infinity/NaN must be
# rejected OUTRIGHT — even a bound-less field would otherwise let it through
# (every comparison with nan is False, and inf passes any lone lower bound).
E.CONFIG_ERRORS = []
check("_coerce_config_number rejects inf outright (no bound needed)",
      E._coerce_config_number("1e999", "X", default=0) == 0 and E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("_coerce_config_number rejects nan outright",
      E._coerce_config_number("NaN", "X", default=0) == 0 and E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("_coerce_config_positive_or_none rejects a non-finite value",
      E._coerce_config_positive_or_none("Infinity", "Y", default=None) is None and E.CONFIG_ERRORS)

check("_coerce_config_positive_or_none maps blank/none to None",
      E._coerce_config_positive_or_none("none", "Y") is None)
E.CONFIG_ERRORS = []
check("_coerce_config_positive_or_none rejects <= 0",
      E._coerce_config_positive_or_none("0", "Y", default=None) is None and E.CONFIG_ERRORS)

check("_coerce_string_list splits comma/newline strings, trims, dedups",
      E._coerce_string_list("a, b\nb , c", "L") == ["a", "b", "c"])
check("_coerce_string_list passes a list through, trimming blanks",
      E._coerce_string_list(["x", " ", "y"], "L") == ["x", "y"])
E.CONFIG_ERRORS = []
check("_coerce_string_list rejects a non-list/non-string",
      E._coerce_string_list(42, "L") == [] and E.CONFIG_ERRORS)

check("_coerce_movie_extensions normalizes to lowercased dotted set",
      E._coerce_movie_extensions("MKV, .mp4") == {".mkv", ".mp4"})

check("_coerce_config_bool reads truthy strings", E._coerce_config_bool("Yes") is True)
check("_coerce_config_bool reads falsey/garbage as False",
      E._coerce_config_bool("nope") is False and E._coerce_config_bool(0) is False)

# _normalize_library_path maps every entry shape under the library root.
root = str(E.LIBRARY_ROOT)
name = root.rsplit("/", 1)[-1]
check("_normalize_library_path accepts a bare folder name",
      E._normalize_library_path("Movies") == f"{root}/Movies")
check("_normalize_library_path treats '/' as the whole root",
      E._normalize_library_path("/") == root)
check("_normalize_library_path keeps only the leaf of a foreign absolute path",
      E._normalize_library_path("/mnt/user/Movies") == f"{root}/Movies")
check("_normalize_library_path returns None for blank", E._normalize_library_path("  ") is None)

# ── compute_config_hash: metadata-source sensitivity ─────────────────────────
E.TAUTULLI_URL = "http://tautulli"; E.TAUTULLI_API_KEY = "k"
E.PLEX_URL = "http://plex"; E.PLEX_TOKEN = "t"
E.PROTECTED_COLLECTIONS = ["Keep"]
E.USE_PLEX = True; E.USE_JELLYFIN = False
E.JELLYFIN_URL = ""; E.JELLYFIN_API_KEY = ""; E.JELLYFIN_PROTECTED_COLLECTIONS = set()
E.MONITOR_DIRS = ["/library/Movies"]
E.MAX_LIBRARY_SIZE_GB = 100
base_hash = E.compute_config_hash()
# A threshold/scoring/path change must NOT bust the metadata cache.
E.MAX_LIBRARY_SIZE_GB = 200
E.MONITOR_DIRS = ["/library/Other"]
check("compute_config_hash ignores thresholds and monitored paths",
      E.compute_config_hash() == base_hash)
# A metadata-source change (Tautulli key) MUST bust it.
E.TAUTULLI_API_KEY = "different"
check("compute_config_hash changes when a metadata source changes",
      E.compute_config_hash() != base_hash)

# ── imdb_dataset_needed ──────────────────────────────────────────────────────
E.QUALITY_WEIGHT = 0.0; E.MAX_IMDB_RATING = None
check("IMDb dataset not needed at zero quality weight and no cutoff",
      E.imdb_dataset_needed() is False)
E.MAX_IMDB_RATING = 6.0
check("a Max IMDb cutoff alone requires the dataset", E.imdb_dataset_needed() is True)
E.MAX_IMDB_RATING = None; E.QUALITY_WEIGHT = 0.5
check("a positive quality weight alone requires the dataset", E.imdb_dataset_needed() is True)

# ── _bounded_gunzip: decompression-bomb caps ─────────────────────────────────
small = gzip.compress(b"tconst\taverageRating\tnumVotes\ntt1\t7.0\t100\n")
check("_bounded_gunzip round-trips a small archive",
      b"tconst" in E._bounded_gunzip(small))
_saved_gz = E._IMDB_GZ_MAX_BYTES
E._IMDB_GZ_MAX_BYTES = 4  # smaller than the compressed blob
try:
    E._bounded_gunzip(small)
    check("_bounded_gunzip rejects an oversized compressed archive", False)
except ValueError:
    check("_bounded_gunzip rejects an oversized compressed archive", True)
E._IMDB_GZ_MAX_BYTES = _saved_gz
_saved_tsv = E._IMDB_TSV_MAX_BYTES
E._IMDB_TSV_MAX_BYTES = 4  # decompressed output exceeds this
try:
    E._bounded_gunzip(small)
    check("_bounded_gunzip rejects a decompression bomb (output cap)", False)
except ValueError:
    check("_bounded_gunzip rejects a decompression bomb (output cap)", True)
E._IMDB_TSV_MAX_BYTES = _saved_tsv

# ── _load_imdb_ratings_from_disk: header + row validation ─────────────────────
tsv_dir = Path(tempfile.mkdtemp(prefix="mr-imdb."))
good = tsv_dir / "good.tsv"
good.write_text("tconst\taverageRating\tnumVotes\ntt1\t7.5\t1000\ntt2\tBAD\tROW\ntt3\t6.0\t50\n",
                encoding="utf-8")
E.IMDB_RATINGS_PATH = good
ratings = E._load_imdb_ratings_from_disk()
check("valid rows parse to (rating, votes)", ratings.get("tt1") == (7.5, 1000))
check("a malformed row is skipped, not fatal", "tt2" not in ratings and ratings.get("tt3") == (6.0, 50))
bad_header = tsv_dir / "bad.tsv"
bad_header.write_text("id\trating\tvotes\ntt1\t7.5\t1000\n", encoding="utf-8")
E.IMDB_RATINGS_PATH = bad_header
try:
    E._load_imdb_ratings_from_disk()
    check("a wrong TSV header is rejected", False)
except ValueError:
    check("a wrong TSV header is rejected", True)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
