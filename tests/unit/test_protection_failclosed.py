"""A configured protected collection that matches NOTHING on the server must
abort deleting runs (fail closed), warn-and-continue in the quiet summary
(debug_info mark upkeep), and proceed normally when it matches."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import engine

engine.PROTECTED_COLLECTIONS = ["Keep Forever"]
engine.PLEX_URL = "http://x"
engine.PLEX_TOKEN = "t"
engine._plex_movie_section_ids_direct = lambda: ["1"]
# Plex answers fine but lists ZERO matching collections (renamed/deleted).
engine.plex_request = lambda p: (200, {"MediaContainer": {"Directory": [
    {"title": "Some Other Collection", "ratingKey": "9"}]}})

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

for mode in ("headroom", "debug_sim"):
    engine.RUN_MODE = mode
    try:
        engine.fetch_protected_paths()
        check(f"{mode}: aborted on missing protection", False)
    except SystemExit:
        check(f"{mode}: aborted on missing protection", True)

engine.RUN_MODE = "debug_info"
try:
    r = engine.fetch_protected_paths()
    check("debug_info: warns and continues", r == (set(), set(), set(), set()))
except SystemExit:
    check("debug_info: warns and continues", False)

engine.RUN_MODE = "headroom"
def _req(p):
    if "collections" in p and "children" not in p:
        return (200, {"MediaContainer": {"Directory": [{"title": "Keep Forever", "ratingKey": "5"}]}})
    return (200, {"MediaContainer": {"Metadata": []}})
engine.plex_request = _req
try:
    engine.fetch_protected_paths()
    check("matched collection proceeds", True)
except SystemExit:
    check("matched collection proceeds", False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
