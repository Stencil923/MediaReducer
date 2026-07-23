"""CLI smoke test — drive the running MediaReducer service through cli.py end to
end over real HTTP. Run against MR_BASE_URL (a booted app with the mocks connected;
a Simulate may or may not have run — the read commands handle either).

Exercises the request/response plumbing, the config round-trip (get -> set ->
get), the score-config path, the list/log renderers, and the unreachable-service
error path. It does NOT launch a Cleanup (no deletions in a smoke test)."""
import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cli

BASE = os.environ.get("MR_BASE_URL", "http://127.0.0.1:7474")
ok = True


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + extra[:160]) if not cond else ""))
    ok = ok and cond


B = ["--url", BASE]

rc, o = run(B + ["status"])
check("status returns 0 and renders", rc == 0 and "Run state:" in o, o)

rc, o = run(B + ["--json", "status"])
try:
    parsed = json.loads(o)
except Exception:
    parsed = None
check("status --json is valid JSON", rc == 0 and isinstance(parsed, dict))

rc, o = run(B + ["config", "get", "RUN_MODE"])
check("config get RUN_MODE", rc == 0 and o.strip() in ("paused", "headroom"), o)

# Config round-trip through /api/config (round-trips the WHOLE config).
rc, o = run(B + ["config", "set", "GRACE_PERIOD_DAYS=45"])
check("config set (score key -> /api/score-config)", rc == 0 and "Saved" in o, o)
rc, o = run(B + ["config", "get", "GRACE_PERIOD_DAYS"])
check("config set persisted", rc == 0 and o.strip() == "45", o)

# A plain config-page key round-trips via /api/config without losing other keys.
rc, o = run(B + ["config", "set", "DAILY_RUN_TIME=03:30"])
check("config set (config-page key)", rc == 0 and "Saved" in o, o)
rc, o = run(B + ["config", "get", "DAILY_RUN_TIME"])
check("config-page key persisted, others intact", rc == 0 and o.strip() == "03:30", o)
rc, o = run(B + ["config", "get", "GRACE_PERIOD_DAYS"])
check("the earlier score key survived the config-page save", rc == 0 and o.strip() == "45", o)

for cmd, needle in (
    (["queue", "--limit", "3"], None),
    (["history"], "Deleted total"),
    (["library", "--per-page", "3"], None),
    (["logs", "--lines", "20"], None),
    (["connections", "check"], "Overall:"),
    (["cache", "status"], None),
    (["imdb", "status"], None),
    (["stop"], None),
):
    rc, o = run(B + cmd)
    cond = rc == 0 and (needle is None or needle in o)
    check("cmd: " + " ".join(cmd), cond, o)

# Unreachable service -> clean error, exit 2 (not a traceback).
rc, o = run(["--url", "http://127.0.0.1:9099", "status"])
check("unreachable service exits 2", rc == 2)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
