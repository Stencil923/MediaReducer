"""Re-Simulating under a new scoring balance must keep each existing mark's AGE
(marked_at) but refresh its displayed score/title/size to the current plan.
Regression for write_plan_to_queue previously freezing the score at first-mark
time, so the deleted-history modal showed scores from a stale balance."""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

tmp = Path(tempfile.mkdtemp())
E.OUTPUT_DIR = tmp
E.DB_FILE = tmp / "mediareducer.db"
E.DELETE_DELAY_DAYS = 3
E.log = lambda *a, **k: None   # silence

def cand(path, title, score, size):
    return {"path": Path(path), "title": title, "retention_score": score,
            "file_size": size}

# First Simulate (balance=0): everything scores 0.0.
old_now = time.time() - 5 * 86400   # marked 5 days ago
E.save_pending({
    str(Path("/library/m/A.mkv")): {"title": "A", "score": 0.0,
                                     "marked_at": old_now, "trigger": "sim", "size_bytes": 100},
    str(Path("/library/m/B.mkv")): {"title": "B", "score": 0.0,
                                    "marked_at": old_now, "trigger": "sim", "size_bytes": 200},
})

# Re-Simulate (balance=100): same movies, new scores/sizes, plus a brand-new one.
planned = [
    (cand("/library/m/A.mkv", "A", 11.0, 150), 150),
    (cand("/library/m/B.mkv", "B", 42.5, 250), 250),
    (cand("/library/m/C.mkv", "C", 7.0, 300), 300),
]
kept, new_marks, dropped = E.write_plan_to_queue(planned, "sim", scheduled_count=3)

check("all three kept", set(Path(k).name for k in kept) == {"A.mkv", "B.mkv", "C.mkv"})
check("one new mark (C)", new_marks == 1)

a = kept[str(Path("/library/m/A.mkv"))]
b = kept[str(Path("/library/m/B.mkv"))]
c = kept[str(Path("/library/m/C.mkv"))]

# Age preserved for the pre-existing marks…
check("A keeps its original mark age", abs(a["marked_at"] - old_now) < 1)
check("B keeps its original mark age", abs(b["marked_at"] - old_now) < 1)
# …but the score/size refresh to the new plan (the whole point of the fix).
check("A score refreshed to new plan", a["score"] == 11.0)
check("B score refreshed to new plan", b["score"] == 42.5)
check("A size refreshed", a["size_bytes"] == 150)
check("B size refreshed", b["size_bytes"] == 250)
# New mark starts its clock now and carries its own score.
check("C is freshly marked now", (time.time() - c["marked_at"]) < 5)
check("C carries its score", c["score"] == 7.0)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
