"""Save pages guard navigation while a save is in flight or the form is dirty,
and show an ANIMATED "Saving" label so the in-flight state (and why leaving is
blocked) is unmistakable.

  • Configuration (/config): beforeunload guards the in-flight save (_savePending)
    AND unsaved edits (_dirty); the Save button animates via btn-pending-ellipsis.
  • Filtering & Scoring (/explorer): beforeunload guards the in-flight save
    (cfgSaving) AND unsaved edits; the Save label is "Saving" (no static ellipsis)
    with the animated .pending-ellipsis dots.

Marker-level render checks — the same style as test_app_coverage — so a regression
that drops the guard or reverts to a static/absent label is caught."""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

client = A.app.test_client()

def _html(path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)

# ── Configuration page ────────────────────────────────────────────────────────
cfg = _html("/config")
check("config: registers a beforeunload guard", "beforeunload" in cfg)
# The guard must let a clean, saved page leave (return early) yet fire for either
# an in-flight save OR a dirty form.
m = re.search(r"beforeunload[\s\S]{0,400}?\}\);", cfg)
guard = m.group(0) if m else ""
check("config: guard early-returns only when neither pending nor dirty",
      "!_savePending && !_dirty" in guard)
check("config: guard blocks the unload (preventDefault + returnValue)",
      "preventDefault" in guard and "returnValue" in guard)
check("config: Save animates via btn-pending-ellipsis", "btn-pending-ellipsis" in cfg)

# ── Filtering & Scoring (deletion score explorer) ─────────────────────────────
exp = _html("/explorer")
check("explorer: registers a beforeunload guard", "beforeunload" in exp)
m = re.search(r"beforeunload[\s\S]{0,400}?\}\);", exp)
eguard = m.group(0) if m else ""
check("explorer: guard covers the in-flight save (cfgSaving)", "cfgSaving" in eguard)
check("explorer: guard covers unsaved edits (dirty vs savedCfg)",
      "savedCfg" in eguard and "cfgEq" in eguard)
check("explorer: guard early-returns when neither saving nor dirty",
      "!cfgSaving && !dirty" in eguard)
check("explorer: guard blocks the unload (preventDefault + returnValue)",
      "preventDefault" in eguard and "returnValue" in eguard)
# Animated label: the class is toggled with cfgSaving and the label text carries
# NO static ellipsis (the dots come from the CSS animation).
check("explorer: Save toggles the animated .pending-ellipsis on cfgSaving",
      re.search(r"pending-ellipsis['\"]\s*,\s*cfgSaving", exp) is not None)
check("explorer: Save label is 'Saving' with no baked-in ellipsis char",
      "cfgSaving?'Saving':'Save'" in exp and "'Saving…'" not in exp)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
