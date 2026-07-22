"""Incremental marked-queue re-selection.

_reverify_marked_queue re-SIZES the marked set to the current deficit (to_free_bytes)
from the cached queue on FRESH watch data, no full scan: it marks the file-size-
optimized covering set for the deficit, a movie a recent watch lifted out of it is
un-marked and the next in line — re-checked fresh before it's trusted — takes its
place, and the set grows/shrinks with the deficit. One-directional by construction:
a failed/partial fetch can only ever SPARE a movie (the max-with-last-scan belt),
never newly doom a safe one. Nothing is deleted here — only marked_at flags move.
The fresh-data fetch is injected, so this is fully hermetic."""
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
# Scoring at 100% watch history so play count dominates the score deterministically.
E.SCORE_BALANCE = 0
E.HISTORY_WEIGHT, E.QUALITY_WEIGHT = E.score_balance_weights(0)
E.MAX_STALENESS_MONTHS = 36
NOW = 1_700_000_000

def snap(path, sid, plays, *, added=1_500_000_000, users=0, last=0, jf=None):
    return {"path": path, "source_id": sid, "jf_source_id": jf,
            "plays": plays, "users": users, "last_played": last,
            "added_at": added, "rating": 6.0, "votes": 1000}

def store_entry(score, marked):
    return {"title": "m", "score": score, "size_bytes": 1_000_000_000,
            "marked_at": (NOW - 86400) if marked else None}

# Score helper so the test's expected order matches the engine's real scorer.
def score_for(plays, last=0, added=1_500_000_000, users=0):
    s, _ = E.compute_retention_score({
        "total_play_count": plays, "last_played_at": last, "added_at": added,
        "distinct_users_watched": users, "imdb_rating": 6.0, "imdb_num_votes": 1000}, now=NOW)
    return round(s, 3)

# ── Scenario: re-size to a 2-movie deficit; a since-watched mark drops out ────
# 4 movies, 1 GB each, all unwatched → equal score, order by path. The covering
# set for a 2 GB deficit is A,B. Then A gets watched (3 plays): its score rises,
# it drops out of the covering set, and C (next in line, re-checked fresh and
# still unwatched) takes its place — the set stays sized to the 2 GB deficit.
paths = ["A", "B", "C", "D"]
snapshots = {p: snap(p, p, 0) for p in paths}
base_score = score_for(0)
store = {p: store_entry(base_score, marked=(p in ("A", "B"))) for p in paths}
GB = 1_000_000_000

# Fresh data: A was watched 3× (last just now); everyone else unchanged (0 plays).
FRESH = {"A": {"play_count": 3, "last_played": NOW, "favorite": False}}
def fake_fetch(ids):
    return {i: FRESH[i] for i in ids if i in FRESH}

_store, res = E._reverify_marked_queue(dict(store), snapshots, 2 * GB, now=NOW, fetch=fake_fetch)

check("the watched marked movie (A) was un-marked", "A" in res["unmarked"])
check("A's marked clock was dropped", _store["A"]["marked_at"] is None)
check("the next eligible in line (C) was marked in its place",
      "C" in res["newly_marked"] and _store["C"]["marked_at"] == NOW)
check("the un-watched marked movie (B) stayed marked", _store["B"]["marked_at"] is not None and "B" not in res["unmarked"])
check("the covering count matches the 2 GB deficit (still 2 marked)",
      sum(1 for e in _store.values() if e["marked_at"] is not None) == 2)
check("D (beyond the deficit) was not pulled in", _store["D"]["marked_at"] is None)
# The refreshed score reorders the queue to the current deletion order (worst first),
# so the freshly-watched movie (A, now highest-scored) sinks to the END instead of
# keeping its stale front slot — the fix for a re-scored mark showing in the wrong spot.
check("the re-scored (watched) movie sinks to the end of the queue order",
      list(_store.keys()) == ["B", "C", "D", "A"])

# ── Re-size: a LARGER deficit marks MORE movies from the eligible queue ───────
_store3, res3 = E._reverify_marked_queue(dict(store), snapshots, 3 * GB, now=NOW, fetch=fake_fetch)
check("a 3 GB deficit marks 3 movies (the set grows with the deficit)",
      sum(1 for e in _store3.values() if e["marked_at"] is not None) == 3)

# ── Safety belt: an UNVERIFIABLE fetch never un-marks a movie ─────────────────
# The fetch returns nothing (every id unreadable). The covering set stays on its
# last-scan verdict — a failed re-check can only spare, never doom, and with no
# watches to spare anyone, the 2 GB covering set is unchanged (A,B).
store2 = {p: store_entry(base_score, marked=(p in ("A", "B"))) for p in paths}
_s2, res2 = E._reverify_marked_queue(dict(store2), snapshots, 2 * GB, now=NOW, fetch=lambda ids: {})
check("an all-unverifiable re-check un-marks nobody", res2["unmarked"] == [])
check("an all-unverifiable re-check keeps the same marked set",
      {p for p, e in _s2.items() if e["marked_at"] is not None} == {"A", "B"})

# ── Belt at the source: a lower fetched count can never lower the score ──────
# _fresh_retention_score bolts max(last-scan, fetched) so a wrong/partial read
# only ever makes a movie look MORE watched (safer), never less.
known5 = snap("X", "X", 5)                       # last scan knew 5 plays
score_known, _v, _p, _l, _f = E._fresh_retention_score(known5, {}, NOW)     # no fetch → uses the 5
score_lowfetch, _v, ep_low, _l, _f = E._fresh_retention_score(
    known5, {"X": {"play_count": 0, "last_played": 0}}, NOW)
check("belt: a fetch reading fewer plays than last scan never lowers the score",
      score_lowfetch == score_known and round(score_known, 3) == score_for(5))
check("belt: the effective plays returned never drop below the last scan (5)", ep_low == 5)
score_watched, ver, ep_hi, el_hi, _f = E._fresh_retention_score(
    known5, {"X": {"play_count": 9, "last_played": NOW}}, NOW)
check("a genuine new watch does raise the score (spares the movie)",
      score_watched > score_known and ver is True)
check("belt: the effective plays/last-played reflect the fresh higher watch",
      ep_hi == 9 and el_hi == NOW)

# ── Re-verify persistence payload: refreshed scores + belt watch values ──────
# The re-verify reports watch_updates (snapshot-path → fresh plays/last/favorite)
# for every movie an id was read for, and rewrites the queue entry's stored score
# to the fresh value, so the caller can keep both the queue and the library
# snapshot honest between daily scans.
snaps_w = {"A": snap("A", "A", 0), "B": snap("B", "B", 0),
           "C": snap("C", "C", 0), "D": snap("D", "D", 0)}
store_w = {p: store_entry(base_score, marked=(p in ("A", "B"))) for p in ("A", "B", "C", "D")}
FRESH_W = {"A": {"play_count": 4, "last_played": NOW, "favorite": True},
           "B": {"play_count": 0, "last_played": 0, "favorite": False}}
_sw, res_w = E._reverify_marked_queue(dict(store_w), snaps_w, 2 * GB, now=NOW,
                                      fetch=lambda ids: {i: FRESH_W[i] for i in ids if i in FRESH_W})
check("watch_updates carries the freshly-watched movie's belt values",
      res_w["watch_updates"].get("A") == {"plays": 4, "last_played": NOW, "favorite": True})
check("a movie read fresh with no new plays still reports its (unchanged) belt values",
      res_w["watch_updates"].get("B") == {"plays": 0, "last_played": 0, "favorite": False})
check("an id that couldn't be read fresh is omitted (unverifiable, never persisted)",
      "D" not in res_w["watch_updates"])
_a_fresh_score = round(E._fresh_retention_score(snaps_w["A"], FRESH_W, NOW)[0], 3)
check("the re-scored marked entry's stored score was refreshed to the fresh value",
      _sw["A"]["score"] == _a_fresh_score and _a_fresh_score != base_score)

# ── Re-marking resets the delay clock (marked → eligible → marked) ───────────
# A mark that falls out of the covering set loses its clock entirely; when it later
# re-enters, it must start a FRESH delay clock — never resume the old one. A movie
# that stays continuously marked keeps its original clock.
def _snap3(sid, plays):
    return snap(sid, sid, plays)
_snaps3 = {"A": _snap3("A", 0), "B": _snap3("B", 1), "C": _snap3("C", 2)}
_store3 = {p: store_entry(score_for(pl), marked=False) for p, pl in (("A", 0), ("B", 1), ("C", 2))}
T1, T2, T3 = 1000, 2000, 3000
_no_watch = lambda ids: {}
_a_watched = lambda ids: {i: {"play_count": 5, "last_played": 0, "favorite": False} for i in ids if i == "A"}
# T1: 1 GB deficit → the worst (A, 0 plays) is marked with a T1 clock.
_store3, _ = E._reverify_marked_queue(_store3, _snaps3, 1 * GB, now=T1, fetch=_no_watch)
check("re-mark reset: A is first marked at T1", _store3["A"]["marked_at"] == T1)
# T2: A is watched 5× → its score jumps past the others → A drops out, B takes its place.
_store3, _ = E._reverify_marked_queue(_store3, _snaps3, 1 * GB, now=T2, fetch=_a_watched)
check("re-mark reset: the watched A fell out of the marked set (clock cleared)",
      _store3["A"]["marked_at"] is None and _store3["B"]["marked_at"] == T2)
# T3: the deficit grows to 3 GB → A is pulled back in. Its clock must be the fresh
# T3, not the stale T1 it carried the first time; B (never un-marked) keeps its T2.
_store3, _ = E._reverify_marked_queue(_store3, _snaps3, 3 * GB, now=T3, fetch=_a_watched)
check("re-mark reset: A re-entering the marked set gets a FRESH T3 clock (delay reset)",
      _store3["A"]["marked_at"] == T3)
check("re-mark reset: continuously-marked B keeps its original T2 clock",
      _store3["B"]["marked_at"] == T2)

# ── Redline delete gate: _confirmed_unwatched ────────────────────────────────
# An emergency delete may only take a movie EVERY id of which was read fresh and
# shows no new watch. Watched, partially-verifiable, or id-less → spared.
plex_only = snap("P", "P", 2)                       # one id, 2 known plays
check("confirmed unwatched: verified, no new plays → deletable",
      E._confirmed_unwatched(plex_only, {"P": {"play_count": 2, "last_played": 0}}) is True)
check("watched since marking (more plays) → spared",
      E._confirmed_unwatched(plex_only, {"P": {"play_count": 3, "last_played": 0}}) is False)
check("watched since marking (newer last-played) → spared",
      E._confirmed_unwatched(snap("P", "P", 2, last=100),
                             {"P": {"play_count": 2, "last_played": 200}}) is False)
check("an unreadable id (fetch omitted it) → spared, never deleted on a guess",
      E._confirmed_unwatched(plex_only, {}) is False)
both = snap("M", "M", 5, jf="jf:M2")                # two ids, 5 known merged plays
check("both-servers: both ids verified and quiet → deletable",
      E._confirmed_unwatched(both, {"M": {"play_count": 3, "last_played": 0},
                                    "jf:M2": {"play_count": 2, "last_played": 0}}) is True)
check("both-servers: only one id readable → spared (a watch on the other side can't be ruled out)",
      E._confirmed_unwatched(both, {"M": {"play_count": 3, "last_played": 0}}) is False)
check("a movie with no source id at all → spared",
      E._confirmed_unwatched(snap("Z", None, 0), {}) is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
