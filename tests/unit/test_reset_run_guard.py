"""Reset / clear-cache must not race a run. Two contracts:

  1. /api/config/reset-invalid refuses (409) while a run is active — the guard
     every other config-mutating endpoint has (it was missing).
  2. /api/cache/clear and /api/config/reset perform the store wipe (db.reset_store)
     while holding _run_lock, so a run / summary / reconcile can't start between the
     'is a run active?' check and the unlink and then write into the just-deleted
     store (its engine subprocess would hit a table-less DB)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_CFG_DIR = tempfile.mkdtemp(prefix="mr-reset-guard.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_CFG_DIR) / "config.json")
import app as A
import db

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

client = A.app.test_client()

# ── 1. reset-invalid 409 while a run is active ────────────────────────────────
A._run_active = True
r = client.post("/api/config/reset-invalid", headers={"X-MediaReducer": "1"})
A._run_active = False
check("reset-invalid returns 409 while a run is active", r.status_code == 409)

# ── 2. the wipe runs UNDER _run_lock ──────────────────────────────────────────
# Make the store exist so the endpoints reach the wipe, and spy on reset_store to
# record whether _run_lock was held at the moment it ran.
store_path = A.db_path()
store_path.parent.mkdir(parents=True, exist_ok=True)
with db.transaction(store_path) as conn:          # create a real store file
    db.set_meta(conn, "pending_schema", 1)

_seen = {}
_orig_reset = db.reset_store
def _spy(p):
    _seen["locked"] = A._run_lock.locked()
    # Recreate an empty file so downstream code that expects the path is happy,
    # without actually tearing down shared state.
    _orig_reset(p)
db.reset_store = _spy
# Neutralize the post-wipe work so the test stays fast and hermetic.
A.run_summary = lambda *a, **k: (False, "skip")
A.burn_daily_window_on_startup = lambda *a, **k: None
A._restart_schedule_clock = lambda *a, **k: None

try:
    _seen.clear()
    r = client.post("/api/cache/clear", headers={"X-MediaReducer": "1"})
    check("cache/clear performed the wipe (200)", r.status_code == 200)
    check("cache/clear held _run_lock during the store wipe", _seen.get("locked") is True)

    # Re-create the store (the spy's reset removed it) for the second endpoint.
    with db.transaction(store_path) as conn:
        db.set_meta(conn, "pending_schema", 1)
    _seen.clear()
    r = client.post("/api/config/reset", headers={"X-MediaReducer": "1"})
    check("config/reset performed the wipe (200)", r.status_code == 200)
    check("config/reset held _run_lock during the store wipe", _seen.get("locked") is True)
finally:
    db.reset_store = _orig_reset

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
