"""The app-side reconcile trigger: deciding when a config save rebuilds the queue
in place, whether it needs a media-server re-fetch (collections / favorites) or is a
pure recompute (scoring / filters / thresholds), and holding + auto-retrying a
re-fetch when the server it needs is unreachable."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-reconcile-trigger.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text('{"OUTPUT_DIR": "%s"}' % _OUT, encoding="utf-8")
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# Capture launches instead of spawning the engine subprocess.
calls = []
A.run_reconcile = lambda *, refetch, trigger: calls.append({"refetch": refetch, "trigger": trigger})
A._has_monitored_dirs = lambda cfg=None: True
HEALTH = {"critical_ok": True}
A._refresh_connection_health_cache = lambda cfg=None, probe=True: dict(HEALTH)
A._reconcile_held = None

def reset():
    calls.clear()
    A._reconcile_held = None

# 1. A pure scoring change → reconcile with NO server refetch.
reset()
r = A._reconcile_after_save({"SCORE_BALANCE": 50}, {"SCORE_BALANCE": 70}, source="filtering & scoring")
check("a scoring change reconciles with no server refetch",
      r == "started" and len(calls) == 1 and calls[0]["refetch"] is False)

# 2. A threshold change → reconcile in place, no refetch.
reset()
r = A._reconcile_after_save({"HEADROOM_GB": 100}, {"HEADROOM_GB": 200}, source="configuration")
check("a threshold change reconciles in place (no refetch)",
      r == "started" and calls[0]["refetch"] is False)

# 3. A collections change with a healthy server → reconcile WITH refetch.
reset()
HEALTH = {"critical_ok": True}
r = A._reconcile_after_save({"PROTECTED_COLLECTIONS": ["A"]},
                            {"PROTECTED_COLLECTIONS": ["A", "B"]}, source="configuration")
check("a collections change re-fetches protection when the server is up",
      r == "started" and calls[0]["refetch"] is True)

# 4. Favorites toggle → refetch (the Jellyfin re-fetch decision).
reset()
r = A._reconcile_after_save({"PROTECT_JELLYFIN_FAVORITES": False},
                            {"PROTECT_JELLYFIN_FAVORITES": True}, source="filtering & scoring")
check("toggling favorites re-fetches from Jellyfin", r == "started" and calls[0]["refetch"] is True)

# 5. A collections change with the server DOWN → HELD, nothing launched.
reset()
HEALTH = {"critical_ok": False}
r = A._reconcile_after_save({"PROTECTED_COLLECTIONS": []},
                            {"PROTECTED_COLLECTIONS": ["A"]}, source="configuration")
check("a refetch change with the server down is HELD, not launched",
      r == "held_connection" and calls == [] and A._reconcile_held is not None)

# 6. The held reconcile fires once the connection recovers.
HEALTH = {"critical_ok": True}
fired = A._retry_held_reconcile({"USE_PLEX": True, "MONITOR_DIRS": ["/x"]})
check("the held reconcile fires when the connection recovers",
      fired is True and calls and calls[-1]["refetch"] is True and A._reconcile_held is None)

# 7. Disabling the server also resolves a held refetch (nothing to fetch → pure).
reset()
HEALTH = {"critical_ok": False}
A._reconcile_after_save({"JELLYFIN_PROTECTED_COLLECTIONS": []},
                        {"JELLYFIN_PROTECTED_COLLECTIONS": ["X"]}, source="configuration")
fired = A._retry_held_reconcile({"USE_PLEX": False, "USE_JELLYFIN": False, "MONITOR_DIRS": ["/x"]})
check("disabling the server resolves a held reconcile as a pure recompute",
      fired is True and calls[-1]["refetch"] is False and A._reconcile_held is None)

# 8. No plan-affecting change → no reconcile.
reset()
r = A._reconcile_after_save({"LOG_RETENTION_DAYS": 30}, {"LOG_RETENTION_DAYS": 60}, source="configuration")
check("an unrelated setting change does not reconcile", r == "none" and calls == [])

# 9. Nothing monitored → nothing to reconcile.
reset()
A._has_monitored_dirs = lambda cfg=None: False
r = A._reconcile_after_save({"SCORE_BALANCE": 50}, {"SCORE_BALANCE": 70}, source="x")
check("nothing monitored -> nothing to reconcile", r == "none" and calls == [])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
