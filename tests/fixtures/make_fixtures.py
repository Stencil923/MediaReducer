"""Build a hermetic e2e environment under the directory given as argv[1]:

  <dir>/library/movies/Test Movie N/...   files matching tests/mocks/mock_tautulli.py
  <dir>/library/other/...                 movies OUTSIDE the monitored paths
  <dir>/config/config.json                fixture config (monitors 'movies' + 'other')
  <dir>/config/... (OUTPUT_DIR)           app/engine state lands here
  <dir>/ratings/title.ratings.tsv         IMDb dataset covering the mock's tt ids

The mock hands out paths like /movies/Test Movie 1/…; the engine's path
translation maps them under MEDIAREDUCER_LIBRARY, so the harness exports
that env var pointing at <dir>/library.

A second positional arg selects the server profile (default "plex"):
  plex      Plex/Tautulli only (the mock on :8765)
  jellyfin  Jellyfin only (mock_jellyfin on :8767)
  both      Plex + Jellyfin, so the merge/identity path runs end to end
"""
import json
import sys
from pathlib import Path

base = Path(sys.argv[1]).resolve()
profile = sys.argv[2] if len(sys.argv) > 2 else "plex"
lib = base / "library"
cfg_dir = base / "config"
ratings_dir = base / "ratings"

for i in range(1, 401):
    folder = "movies" if i <= 300 else "other"
    d = lib / folder / f"Test Movie {i}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"Test Movie {i}.mkv"
    if not f.exists():
        f.write_bytes(b"\0" * 1024)

cfg_dir.mkdir(parents=True, exist_ok=True)
ratings_dir.mkdir(parents=True, exist_ok=True)

tsv = ratings_dir / "title.ratings.tsv"
with open(tsv, "w", encoding="utf-8") as fh:
    fh.write("tconst\taverageRating\tnumVotes\n")
    for i in range(1, 401):
        fh.write(f"tt{7000000 + i}\t{5 + (i % 50) / 10:.1f}\t{1000 + i * 37}\n")

use_plex = profile in ("plex", "both")
use_jellyfin = profile in ("jellyfin", "both")
if profile not in ("plex", "jellyfin", "both"):
    sys.exit(f"unknown profile {profile!r} (want plex|jellyfin|both)")

config = {
    "RUN_MODE": "paused",
    "USE_PLEX": use_plex,
    "USE_JELLYFIN": use_jellyfin,
    "TAUTULLI_URL": "http://127.0.0.1:8765",
    "TAUTULLI_API_KEY": "test-key",
    # Jellyfin (mock_jellyfin.py on :8767) serves the same fixture library.
    "JELLYFIN_URL": "http://127.0.0.1:8767",
    "JELLYFIN_API_KEY": "test-jf-key",
    "MONITOR_DIRS": ["movies", "other"],
    "HEADROOM_GB": 100,
    # Explicit null: a missing key is filled with the DEFAULT redline (200),
    # which exceeds the 100 GB headroom above and trips the cross-field
    # validator's invalid-config lockout.
    "REDLINE_GB": None,
    "SCORE_BALANCE": 0,
    "MAX_IMDB_RATING": None,
    "OUTPUT_DIR": str(cfg_dir),
    # Point downloads at a dead port: any accidental network fetch fails loudly.
    "IMDB_RATINGS_URL": "http://127.0.0.1:9/never",
}
(cfg_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
print(f"fixtures ready under {base} (profile={profile})")
