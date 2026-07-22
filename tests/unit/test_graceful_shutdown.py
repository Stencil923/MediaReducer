"""A container/app stop (SIGTERM to PID 1) must stop an in-flight run cleanly —
forward the stop to the engine child and wait for it to exit before the app
does — instead of letting the child be SIGKILLed with the container."""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

# Capture the terminal-progress write instead of touching a real progress.json.
marks = []
A._mark_progress_terminal = lambda status, msg, **k: marks.append((status, msg))

# Stand-in for the engine run: a real child process that just sleeps, so we can
# prove the app's shutdown handler actually reaches and stops the child.
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
A._run_process = proc
A._run_active = True
A._run_cleanup = True
A._run_stop_requested = threading.Event()
A._shutting_down = False

t0 = time.time()
exited = None
try:
    A._graceful_shutdown(signal.SIGTERM, None)
except SystemExit as e:
    exited = e.code
elapsed = time.time() - t0

check("handler exits the app with SystemExit(0)", exited == 0)
check("engine child was actually stopped", proc.poll() is not None)
check("stopped promptly, well inside the 8s wait budget", elapsed < 7)
check("run was flagged for stop", A._run_stop_requested.is_set())
check("progress was marked stopped", any(s == "stopped" for s, _ in marks))

# Second signal while already shutting down: exit immediately, don't re-stop.
marks.clear()
try:
    A._graceful_shutdown(signal.SIGTERM, None)
    second_exit = "no-exit"
except SystemExit as e:
    second_exit = e.code
check("re-entrant signal exits immediately", second_exit == 0)
check("re-entrant signal does not re-stop", marks == [])

# Idle shutdown (no active run) must still exit cleanly and not touch progress.
A._shutting_down = False
A._run_active = False
A._run_process = None
marks.clear()
try:
    A._graceful_shutdown(signal.SIGTERM, None)
    idle_exit = "no-exit"
except SystemExit as e:
    idle_exit = e.code
check("idle shutdown exits cleanly", idle_exit == 0)
check("idle shutdown writes no stopped-progress", marks == [])

if proc.poll() is None:
    proc.kill()

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
