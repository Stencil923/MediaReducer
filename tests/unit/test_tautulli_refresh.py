"""Regression: the Tautulli library scan must force a media-info refresh.

Tautulli's `get_library_media_info` serves a CACHED table that Tautulli only
rebuilds on demand (`refresh=true`) or on its own schedule. A recently-added
movie is absent from that stale cache even though Tautulli already tracks its
play history — so the engine's scan would miss it, and with Jellyfin also
enabled the movie reappears as a 0-play Jellyfin-only row that scores low and
lands in the deletion queue despite being watched (observed live: a movie
watched 3× was marked DRY RUN DELETE #1522).

The fix: `get_all_movies_from_tautulli` passes `refresh=true` on the first page
of each section so the table is rebuilt before it's read. This models Tautulli's
real, STATEFUL behavior — a refresh makes the movie visible for the rest of the
scan — and fails if the engine ever stops sending it."""
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
E.RADARR_OVERSEERR_SECTION_ID = None

# 1000 movies already in Tautulli's cached table (enough to force a 2nd page,
# so the "only the first page refreshes" behavior is exercised too).
BASE = [{"rating_key": str(1000 + i), "title": f"Movie {i}",
         "file": f"/movies/Movie {i}/Movie {i}.mkv", "play_count": 0}
        for i in range(1000)]
# The recently-added, already-watched title — present ONLY after a refresh.
RECENT = {"rating_key": "tt-recent", "title": "Jeff Arcuri: Nice to Meet You",
          "file": "/movies/Jeff Arcuri (2026)/jeff.mkv", "play_count": 3}

state = {"refreshed": False}
calls = []   # the refresh value seen on each get_library_media_info page

def fake_tautulli(cmd, **params):
    if cmd == "get_libraries":
        return [{"section_id": "1", "section_name": "Movies",
                 "section_type": "movie", "is_active": 1}]
    if cmd == "get_library_media_info":
        refresh = params.get("refresh")
        calls.append(refresh)
        if str(refresh) == "true":
            state["refreshed"] = True          # Tautulli rebuilds the table once…
        movies = BASE + ([RECENT] if state["refreshed"] else [])  # …then it's fresh
        start = int(params.get("start", 0)); length = int(params.get("length", 1000))
        return {"data": movies[start:start + length], "recordsFiltered": len(movies)}
    return {}

E.tautulli_api = fake_tautulli

rows = E.get_all_movies_from_tautulli()
titles = {r.get("title") for r in rows}

check("the recently-added, already-watched movie is included (scan forced a refresh)",
      "Jeff Arcuri: Nice to Meet You" in titles)
check("its real play history survives (not a 0-play row)",
      any(r.get("title") == "Jeff Arcuri: Nice to Meet You"
          and E.parse_int(r.get("play_count"), 0) == 3 for r in rows))
check("the first page requests a Tautulli media-info refresh", calls and calls[0] == "true")
check("later pages read the freshly-rebuilt table without re-refreshing",
      len(calls) > 1 and all(c == "false" for c in calls[1:]))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
