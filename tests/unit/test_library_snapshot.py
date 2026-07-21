"""The library snapshot (cache.json "library_snapshot") that backs the
Filtering & Scoring table: written atomically only at scan completion, it
survives engine cache clears (version bumps / metadata-config changes) and an
interrupted run leaves the previous snapshot untouched. The app reads it back
verbatim."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-test-out.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import engine as E
import app as A

E.OUTPUT_DIR = Path(_OUT)
E.CACHE_FILE = Path(_OUT) / "cache.json"
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

# First write lands alongside existing cache content without disturbing it.
E.save_cache({"movies": {"k1": {"title": "cached"}}, "last_cleanup_date": "2026-07-01"})
E._write_library_snapshot([row])
cache = json.loads(E.CACHE_FILE.read_text(encoding="utf-8"))
check("snapshot merged into cache without disturbing other keys",
      cache["library_snapshot"]["movies"][0]["title"] == "Movie A"
      and cache["last_cleanup_date"] == "2026-07-01"
      and cache["movies"]["k1"]["title"] == "cached")
check("snapshot stamps built_at", cache["library_snapshot"]["built_at"] > 0)

# An engine cache clear (version bump / metadata-config change) rewrites the
# code-derived keys but must preserve the snapshot — it is run output, not
# derived cache.
E.save_cache({"movies": {}, "last_cleanup_date": "2026-07-02"})
cache = json.loads(E.CACHE_FILE.read_text(encoding="utf-8"))
check("save_cache preserves the snapshot from disk",
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

# Missing cache -> clean 'missing' answer (the page shows the run-a-Simulate note).
E.CACHE_FILE.unlink()
snap, err = A._read_library_snapshot()
check("missing cache reads as missing", snap is None and err == "missing")

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
