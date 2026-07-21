"""deleted.log parsing: lines with and without the optional best-effort fields
must parse, size_bytes must survive the rationale fields, and the history line
carries the why."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

cases = [
    ("2026-07-14 10:00:00 | Movie A | /library/movies/A/A.mkv", None),
    ("2026-07-14 10:00:00 | Movie A | /library/movies/A/A.mkv | size_bytes=5000000000", "5000000000"),
    ("2026-07-14 10:00:00 | Movie A | /library/movies/A/A.mkv | size_bytes=5000000000 "
     "| score=7.5 | plays=3 | last_played=2024-03-01 20:15:33", "5000000000"),
    ("2026-07-14 10:00:00 | Movie A | /library/movies/A/A.mkv | size_bytes=123 "
     "| score=0.0 | plays=0 | last_played=never", "123"),
]
for line, want_size in cases:
    m = A._DELETED_LOG_RE.match(line)
    got = m.group("size_bytes") if m else "NOMATCH"
    check(f"parses [{line[:60]}…]", got == want_size and m.group("path") == "/library/movies/A/A.mkv")

sample = [
    "2026-07-14 10:00:00 | Plain Movie | /library/movies/O/O.mkv | size_bytes=4000000000",
    "2026-07-14 10:05:00 | New Movie | /library/movies/N/N.mkv | size_bytes=5000000000 "
    "| score=7.5 | plays=3 | last_played=2024-03-01 20:15:33",
    "2026-07-14 10:06:00 | Unwatched | /library/movies/U/U.mkv | size_bytes=123 "
    "| score=0.0 | plays=0 | last_played=never",
]
A._deleted_log_lines = lambda: sample
A._deleted_stats_memo = None
entries = A.deleted_entries()
check("line without rationale has no why", "score" not in entries[0]["line"])
check("line with rationale shows why", "score 7.5" in entries[1]["line"] and "3 plays" in entries[1]["line"]
      and "last watched 2024-03-01" in entries[1]["line"])
check("never-played reads as never watched", "never watched" in entries[2]["line"])
check("sizes intact", entries[1]["size_bytes"] == 5000000000)
stats = A.deleted_stats()
check("reclaimed bytes sum every line shape", stats["reclaimed_bytes"] == 4000000000 + 5000000000 + 123)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
