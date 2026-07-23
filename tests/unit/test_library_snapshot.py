"""The library snapshot (the store's library_snapshot) that backs the
Filtering & Scoring table: written straight to the movies table at scan
completion, it survives a same-version metadata rewrite (a metadata rebuild
after a config change must not clobber it) and an interrupted run leaves the
previous snapshot untouched. The one deliberate exception is a code version
bump: the snapshot's row shape is engine-defined, so a checksum mismatch
flushes it to be rebuilt on the next scan rather than carrying a possibly
wrong-shaped table forward. The app reads it back verbatim."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dbstate
_OUT = tempfile.mkdtemp(prefix="mr-test-out.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import engine as E
import app as A

E.OUTPUT_DIR = Path(_OUT)
E.DB_FILE = Path(_OUT) / "mediareducer.db"
A.load_config = lambda: {"OUTPUT_DIR": _OUT}

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

row = E._snapshot_entry("Movie A", 2001, 7.1, 1200, 3, 2, 1600000000,
                        1500000000, 2_000_000_000, protected=True, favorite=False)
check("entry shape", row["title"] == "Movie A" and row["rating"] == 7.1
      and row["size_gb"] == 2.0 and row["protected"] is True)

# First write lands alongside existing store content without disturbing it.
E.save_cache({"movies": {"k1": {"protected": False, "tmdb_id": "tt1", "imdb_id": "i1", "v": 2}},
              "last_cleanup_date": "2026-07-01"})
E._write_library_snapshot([row])
cache = _dbstate.read(E.DB_FILE)
check("snapshot written without disturbing the metadata cache / schedule date",
      cache["library_snapshot"]["movies"][0]["title"] == "Movie A"
      and cache["last_cleanup_date"] == "2026-07-01"
      and cache["movies"]["k1"]["tmdb_id"] == "tt1")
check("snapshot stamps built_at", cache["library_snapshot"]["built_at"] > 0)
# The snapshot records the paths it was scanned from — the app's arming gate
# uses it as "a Simulate has seen THIS library" proof (_simulate_evidence),
# which only holds for these exact paths.
E.MONITOR_DIRS = ["/library/movies", "/library/other"]
E._write_library_snapshot([row])
cache = _dbstate.read(E.DB_FILE)
check("snapshot stamps the monitored paths it scanned",
      cache["library_snapshot"]["monitor_dirs"] == ["/library/movies", "/library/other"])

# A SAME-VERSION metadata rewrite (e.g. movie metadata rebuilt after a config or
# threshold change) must NOT clobber the snapshot: save_cache writes only the
# metadata cache + kv, leaving the movies table (snapshot) alone.
E.save_cache({"movies": {}, "last_cleanup_date": "2026-07-02"})
cache = _dbstate.read(E.DB_FILE)
check("a same-version metadata rewrite preserves the snapshot",
      cache.get("library_snapshot", {}).get("movies", [{}])[0].get("title") == "Movie A")

# Interrupted run: nothing calls _write_library_snapshot, so whatever partial
# state the run died in leaves the previous snapshot readable and intact.
snap, err = A._read_library_snapshot()
check("app reads the snapshot back", err is None and snap["movies"][0]["title"] == "Movie A")

# A completed scan replaces it wholesale.
row2 = E._snapshot_entry("Movie B", 2002, None, 0, 0, 0, 0, 0, 500_000_000)
E._write_library_snapshot([row2, row])
snap, err = A._read_library_snapshot()
check("next completed scan replaces the snapshot",
      err is None and len(snap["movies"]) == 2 and snap["movies"][0]["title"] == "Movie B")
check("unrated entry keeps rating None", snap["movies"][0]["rating"] is None)

# The deliberate exception: a CODE VERSION BUMP (checksum mismatch) flushes the
# snapshot instead of carrying a possibly wrong-shaped table forward — it is
# rebuilt on the next scan. Simulate it by ageing the stored checksum, then
# confirm BOTH engine read/write paths drop it (the schedule date still rides
# through). This is the real behavior the same-version case above does NOT test.
stale = _dbstate.read(E.DB_FILE)
stale["code_checksum"] = "stale-old-engine-version"
stale["last_cleanup_date"] = "2026-07-03"
_dbstate.seed(E.DB_FILE, stale)
reloaded = E.load_cache()
check("load_cache drops the snapshot across a code version bump",
      "library_snapshot" not in reloaded and reloaded.get("last_cleanup_date") == "2026-07-03")
_dbstate.seed(E.DB_FILE, stale)
E.save_cache({"movies": {}})
check("save_cache does not carry a stale-version snapshot forward",
      "library_snapshot" not in _dbstate.read(E.DB_FILE))

# Missing store -> clean 'missing' answer (the page shows the run-a-Simulate note).
_dbstate.reset(E.DB_FILE)
snap, err = A._read_library_snapshot()
check("missing store reads as missing", snap is None and err == "missing")

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
