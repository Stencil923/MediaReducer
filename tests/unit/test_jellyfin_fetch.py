"""Jellyfin library fetch + play aggregation — get_all_movies_from_jellyfin().

This is the normalization layer that turns Jellyfin's per-user, bits/sec,
ISO-8601 world into the Tautulli-shaped rows the rest of the engine scores and
merges. Jellyfin has no cross-user play total, so plays must be SUMMED across
every user and last-played taken as the MOST RECENT — getting this wrong would
either hide watched movies or invent watch history. Protection comes from named
BoxSets and must apply by movie ID, by IMDb/TMDb provider id, or by resolved
path (Jellyfin's collection endpoints vary by version, so all three are tried).

The Jellyfin HTTP layer (_jellyfin_request) is monkeypatched with a canned
router so the aggregation logic is tested hermetically, without a socket. The
real URL/auth/parse layer is covered by test_media_server_integration."""
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

# ── The Jellyfin server, as canned payloads ──────────────────────────────────
# Four movies: Alpha is watched by two users and favorited; Bravo/Charlie are
# unwatched; Delta has no provider ids. DateCreated / Bitrate / MediaStreams are
# in Jellyfin's native shapes so the normalization is exercised, not bypassed.
BASE_ITEMS = [
    {"Id": "A", "Name": "Alpha", "ProductionYear": 2020,
     "Path": "/data/Movies/Alpha (2020)/alpha.mkv",
     "DateCreated": "2020-01-02T03:04:05.000000Z",
     "ProviderIds": {"Tmdb": "111", "Imdb": "tt111"},
     "MediaSources": [{"Size": 3_000_000_000, "Bitrate": 8_000_000,
                       "MediaStreams": [{"Type": "Audio"},
                                        {"Type": "Video", "Height": 1080}]}]},
    {"Id": "B", "Name": "Bravo", "ProductionYear": 2019,
     "Path": "/data/Movies/Bravo (2019)/bravo.mkv",
     "DateCreated": "2019-05-05T00:00:00Z",
     "ProviderIds": {"Imdb": "tt222"},
     "MediaSources": [{"Size": 1_000_000_000}]},
    {"Id": "C", "Name": "Charlie", "ProductionYear": 2018,
     "Path": "/data/Movies/Charlie (2018)/charlie.mkv",
     "ProviderIds": {"Tmdb": "333"},
     "MediaSources": []},
    {"Id": "D", "Name": "Delta", "ProductionYear": 2017,
     "Path": "/data/Movies/Delta (2017)/delta.mkv",
     "MediaSources": []},
]

# Per-user watch state. u1 watched Alpha twice and favorited it; u2 watched
# Alpha three more times and marked Bravo Played with a zero PlayCount (a
# distinct watcher that adds no plays). last_played must land on u2's later date.
USERDATA = {
    "u1": [
        {"Id": "A", "UserData": {"PlayCount": 2, "IsFavorite": True,
                                 "LastPlayedDate": "2021-06-01T00:00:00Z"}},
    ],
    "u2": [
        {"Id": "A", "UserData": {"PlayCount": 3,
                                 "LastPlayedDate": "2021-07-15T12:00:00Z"}},
        {"Id": "B", "UserData": {"PlayCount": 0, "Played": True}},
    ],
}

# A protected BoxSet "Keepers" holding Alpha (matched by movie ID), Bravo
# (matched by IMDb provider id, deliberately under a DIFFERENT member Id so the
# id branch can't be what protects it) and Charlie (matched by TMDb id).
BOXSET_CHILDREN = {
    "box-0": [
        {"Id": "A"},
        {"Id": "x-bravo", "ProviderIds": {"Imdb": "tt222"}},
        {"Id": "x-charlie", "ProviderIds": {"Tmdb": "333"}},
    ],
}

def fake_request(path, params=None, timeout=30):
    params = params or {}
    if path == "Users":
        return [{"Id": "u1", "Name": "one"}, {"Id": "u2", "Name": "two"}]
    if path == "Items" and "ParentId" not in params:
        return {"Items": BASE_ITEMS}          # the base library scan
    if path.startswith("Users/") and path.endswith("/Items"):
        uid = path.split("/")[1]
        if params.get("IncludeItemTypes") == "BoxSet":
            return {"Items": [{"Id": "box-0", "Name": "Keepers"}]}
        if params.get("EnableUserData") == "true":
            return {"Items": USERDATA.get(uid, [])}
        return {"Items": []}
    if path.startswith("Collections/") and path.endswith("/Items"):
        box_id = path.split("/")[1]
        return {"Items": BOXSET_CHILDREN.get(box_id, [])}
    return {}

E._jellyfin_request = fake_request
E.USE_JELLYFIN = True
E.JELLYFIN_PROTECTED_COLLECTIONS = {"Keepers"}
E.RUN_MODE = "headroom"

rows = {r["title"]: r for r in E.get_all_movies_from_jellyfin()}

# ── Row normalization ────────────────────────────────────────────────────────
a = rows["Alpha"]
check("every Jellyfin movie is returned", set(rows) == {"Alpha", "Bravo", "Charlie", "Delta"})
check("rating_key is namespaced jf:<id>", a["rating_key"] == "jf:A")
check("path is carried from the item", a["file"] == "/data/Movies/Alpha (2020)/alpha.mkv")
check("year and title normalized", a["year"] == 2020 and a["title"] == "Alpha")
check("file_size read from MediaSources", E.parse_int(a["file_size"], 0) == 3_000_000_000)
check("bitrate converted bits/sec -> kbps", E.parse_int(a["bitrate"], 0) == 8000)
check("video resolution taken from the Video stream height", a["video_resolution"] == "1080")
check("tmdb/imdb pulled from ProviderIds", a["tmdb_id"] == "111" and a["imdb_id"] == "tt111")
check("DateCreated parsed to added_at epoch",
      a["added_at"] == E._jellyfin_date_to_epoch("2020-01-02T03:04:05Z"))
check("source is tagged jellyfin", a["_source"] == "jellyfin")

# ── Play aggregation across users ────────────────────────────────────────────
check("play counts SUM across every user", E.parse_int(a["play_count"], 0) == 5)
check("distinct Jellyfin watchers counted (both users watched Alpha)", a["_jf_users"] == 2)
check("a favorite on any user flips _jf_favorite", a["_jf_favorite"] is True)
check("last_played is the MOST RECENT across users",
      a["last_played"] == E._jellyfin_date_to_epoch("2021-07-15T12:00:00Z"))
# Bravo: one user marked it Played with PlayCount 0 — a distinct watcher, no plays.
b = rows["Bravo"]
check("Played-with-zero-plays still counts a distinct watcher",
      b["_jf_users"] == 1 and E.parse_int(b["play_count"], 0) == 0)
check("an unwatched movie has no watchers", rows["Delta"]["_jf_users"] == 0)
check("a movie with no provider ids keeps them None",
      rows["Delta"]["tmdb_id"] is None and rows["Delta"]["imdb_id"] is None)

# ── BoxSet protection: by id, by imdb, by tmdb ───────────────────────────────
check("protected by movie ID (Alpha is a BoxSet member)", a["protected"] is True)
check("protected by IMDb provider id (member id differed)", b["protected"] is True)
check("protected by TMDb provider id", rows["Charlie"]["protected"] is True)
check("a non-member stays unprotected", rows["Delta"]["protected"] is False)

# ── _jellyfin_date_to_epoch, directly ────────────────────────────────────────
check("blank/None date -> 0", E._jellyfin_date_to_epoch("") == 0 and E._jellyfin_date_to_epoch(None) == 0)
check("garbage date -> 0 (never raises)", E._jellyfin_date_to_epoch("not-a-date") == 0)
check("fractional seconds and Z are ignored",
      E._jellyfin_date_to_epoch("2020-01-02T03:04:05.999Z")
      == E._jellyfin_date_to_epoch("2020-01-02T03:04:05Z"))

# ── Fail-closed: a configured protected collection that isn't found aborts ────
E.JELLYFIN_PROTECTED_COLLECTIONS = {"Nonexistent"}
try:
    E.get_all_movies_from_jellyfin()
    check("a missing protected BoxSet aborts a deleting run", False)
except SystemExit:
    check("a missing protected BoxSet aborts a deleting run", True)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
