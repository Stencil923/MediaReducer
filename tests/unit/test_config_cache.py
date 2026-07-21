"""load_config() is memoized by config.json's (mtime, size) so a single status
poll doesn't re-parse and re-validate the file hundreds of times. The memo must
be invisible: every call returns an ISOLATED copy (callers mutate the result),
it invalidates the instant the file changes, and the missing-file onboarding
state is itself a stable, cacheable state."""
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td, "config.json")
    cfg_path.write_text(json.dumps({
        "RUN_MODE": "paused", "HEADROOM_GB": 500, "MONITOR_DIRS": ["/library/movies"],
        "PROTECTED_COLLECTIONS": ["Keep"], "USE_PLEX": False, "USE_JELLYFIN": False,
    }))
    _orig = A.CONFIG_PATH
    A.CONFIG_PATH = cfg_path
    try:
        # A caller mutating the result — scalars AND nested lists — must not leak
        # into the next call (the classic shared-mutable-cache bug).
        a = A.load_config()
        a["HEADROOM_GB"] = 99999
        a["MONITOR_DIRS"].append("/hacked")
        a["PROTECTED_COLLECTIONS"].clear()
        b = A.load_config()
        check("mutation of a returned cfg never leaks into the next call",
              b["HEADROOM_GB"] == 500 and "/hacked" not in b["MONITOR_DIRS"]
              and b["PROTECTED_COLLECTIONS"] == ["Keep"])

        # A file change invalidates the memo (mtime/size key), even back to back.
        # Use a different-length value (4321 vs 500) so the file SIZE changes too —
        # otherwise invalidation rides on st_mtime_ns alone, which can tie on a
        # coarse-mtime filesystem and flake this check.
        base = A.load_config()["HEADROOM_GB"]
        d = json.loads(cfg_path.read_text()); d["HEADROOM_GB"] = 4321
        cfg_path.write_text(json.dumps(d))
        check("a write to config.json invalidates the memo",
              base == 500 and A.load_config()["HEADROOM_GB"] == 4321)

        # _CONFIG_FILE_ISSUES is a side effect load_config sets on every call —
        # the memo must keep setting it (a hand-edited bad value stays flagged).
        d["HEADROOM_GB"] = -5   # below the 0 floor → a file-validator issue
        cfg_path.write_text(json.dumps(d))
        A.load_config()
        flagged = any(i.get("key") == "HEADROOM_GB" for i in A._CONFIG_FILE_ISSUES)
        A.load_config()   # a cached second call must still report the issue
        still = any(i.get("key") == "HEADROOM_GB" for i in A._CONFIG_FILE_ISSUES)
        check("cached calls still expose _CONFIG_FILE_ISSUES", flagged and still)
    finally:
        A.CONFIG_PATH = _orig

# Missing file (fresh install, pre-onboarding): builds from defaults, no crash,
# and stays consistent across repeated calls.
with tempfile.TemporaryDirectory() as td:
    A.CONFIG_PATH = Path(td, "does-not-exist.json")
    try:
        c1 = A.load_config()
        c2 = A.load_config()
        check("missing config builds defaults and is stable",
              isinstance(c1, dict) and "HEADROOM_GB" in c1
              and c1["HEADROOM_GB"] == c2["HEADROOM_GB"])
    finally:
        A.CONFIG_PATH = _orig

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
