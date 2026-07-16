"""Generate the engine-side scoring expectations for the parity check.

Writes scoring.json (the shared SCORING constants) and py_scores.json
(engine scores over a grid of balance x age x distinct-users) into the
directory given as argv[1]. parity_check.cjs replays the same grid through
the Score Explorer's JS mirror and compares.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import engine
from scoring_constants import SCORING

out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out_dir.mkdir(parents=True, exist_ok=True)

now = time.time()
engine.MAX_STALENESS_MONTHS = 36
py = {}
for bal in (0, 50):
    engine.SCORE_BALANCE = bal
    engine.HISTORY_WEIGHT, engine.QUALITY_WEIGHT = engine.score_balance_weights(bal)
    grid = {}
    for age in (900, 1500, 2000):
        for users in (0, 1, 2, 4, 6):
            played = users > 0
            rec = {
                "total_play_count": 1 if played else 0,
                "last_played_at": int(now - age * 86400) if played else 0,
                "added_at": int(now - age * 86400),
                "distinct_users_watched": users,
                "imdb_rating": 6.5,
                "imdb_num_votes": 50000,
            }
            score, b = engine.compute_retention_score(rec, now=now)
            grid[f"{age}d/{users}u"] = {
                "score": round(score, 4),
                "recency": round(b.get("recency", 0.0), 4),
                "shelf": round(b.get("shelf", 0.0), 4),
            }
    py[str(bal)] = grid

(out_dir / "scoring.json").write_text(json.dumps(SCORING), encoding="utf-8")
(out_dir / "py_scores.json").write_text(json.dumps(py), encoding="utf-8")
print(f"wrote {out_dir}/scoring.json and {out_dir}/py_scores.json")
