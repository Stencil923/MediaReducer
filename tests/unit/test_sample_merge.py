"""Focused test: dual-source quick sample must fold Plex+Jellyfin data
correctly regardless of shuffle order (the JF-twin-first bug)."""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pathlib import Path
import engine

# ── force dual-source mode ────────────────────────────────────────────────
engine.USE_PLEX = True
engine.USE_JELLYFIN = True
engine.MONITOR_DIRS = [Path("/lib")]

# Movie X: on BOTH servers. Watched 5x on PLEX, never on Jellyfin.
#   Jellyfin protects it (BoxSet) and carries imdb id.
# A Plex light (Tautulli) row carries NO inline file path — only rating_key;
# the real path is resolved lazily (stub_path maps rating_key -> path below).
plex_X = {
    "title": "Movie X", "year": 2000, "rating_key": "111",
    "play_count": 5, "last_played": 1_600_000_000,
    "added_at": 1_500_000_000, "file_size": 5_000_000_000,
}
jelly_X = {
    "title": "Movie X", "year": 2000, "rating_key": "jf:aaa",
    "file": "/lib/X.mkv", "play_count": 0, "last_played": 0,
    "added_at": 1_400_000_000, "file_size": 5_000_000_000,
    "_source": "jellyfin", "_jf_users": 0, "_jf_favorite": False,
    "protected": True, "imdb_id": "tt9999999", "tmdb_id": "42",
}

def stub_tautulli(): return [dict(plex_X)]
def stub_jellyfin(): return [dict(jelly_X)]

captured = {}
def stub_write(movies): captured["movies"] = movies
def stub_path(row, allow_api_lookup=True):
    # Both servers mount the library at the same path, so both resolve to the
    # SAME absolute file (the case where resolved-path dedup fires).
    if row.get("file"):
        return Path(str(row.get("file"))), False
    return Path("/lib/X.mkv"), True   # Plex light row resolved via (stubbed) API
def stub_under(p): return True
def stub_meta(row):
    # (imdb_id, protected) as the real one would resolve them from row fields
    protected = bool(row.get("protected")) or bool(row.get("_jf_protected"))
    imdb = row.get("imdb_id") or row.get("_jf_imdb_id")
    return (imdb or None), protected
def stub_ratings(ids): return {}
def stub_cache(): return {"movies": {}}

engine.get_all_movies_from_tautulli = stub_tautulli
engine.get_all_movies_from_jellyfin = stub_jellyfin
engine._write_sample_pool_file = stub_write
engine._quick_sample_file_path = stub_path
engine.is_under_monitored_dir = stub_under
engine._quick_sample_row_meta = stub_meta
engine._load_imdb_ratings_subset = stub_ratings
engine.load_cache = stub_cache
engine.validate_connections = lambda: True
engine.imdb_dataset_needed = lambda: False   # 100% history: no imdb download

results = {}
for order_name, shuffle_fn, target in [
    ("plex_first", lambda rows: rows.sort(key=lambda r: 0 if not str(r.get("rating_key","")).startswith("jf:") else 1), 10),
    ("jelly_first", lambda rows: rows.sort(key=lambda r: 0 if str(r.get("rating_key","")).startswith("jf:") else 1), 10),
    # target=1: the JF twin fills the only slot and the loop BREAKS before the
    # Plex twin is ever scanned — the reverse title/year lookup must still
    # recover the Plex play data at pick time.
    ("jelly_first_t1", lambda rows: rows.sort(key=lambda r: 0 if str(r.get("rating_key","")).startswith("jf:") else 1), 1),
]:
    captured.clear()
    engine.random.shuffle = shuffle_fn
    engine.build_quick_sample_pool(target=target)
    movies = captured.get("movies", [])
    assert len(movies) == 1, f"{order_name}: expected 1 merged movie, got {len(movies)}"
    m = movies[0]
    results[order_name] = m
    print(f"{order_name}: plays={m['plays']} users={m['users']} protected={m['protected']} title={m['title']}")

# All orders must produce IDENTICAL merged results.
ok = True
base = results["plex_first"]
for name in ("jelly_first", "jelly_first_t1"):
    for field in ("plays", "users", "protected", "last_played", "added_at"):
        if base[field] != results[name][field]:
            print(f"  MISMATCH {field}: plex_first={base[field]} {name}={results[name][field]}")
            ok = False

# Correctness assertions: Plex's 5 plays and 1 watcher must survive; JF protection must survive.
for name, m in results.items():
    if m["plays"] != 5:
        print(f"  BUG {name}: plays={m['plays']} (expected 5 — Plex watch data lost)"); ok = False
    if m["users"] != 1:
        print(f"  BUG {name}: users={m['users']} (expected 1)"); ok = False
    if not m["protected"]:
        print(f"  BUG {name}: protected={m['protected']} (expected True — JF BoxSet protection lost)"); ok = False

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
