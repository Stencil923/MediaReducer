"""Container entrypoint: optional PUID/PGID privilege drop, then run the app.

Without PUID/PGID the container runs as root (Docker's default). Setting them
runs MediaReducer as that user instead — deletions in /library then happen
with exactly that user's permissions, so it must be allowed to write your
media files (on Unraid, PUID=99 PGID=100 = nobody:users). /config is chowned
to the mapped user so state and logs stay writable. Pure Python on purpose:
no gosu/su-exec dependency.
"""
import os
import sys


def _chown_tree(path: str, uid: int, gid: int) -> None:
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.lchown(os.path.join(root, name), uid, gid)
            except OSError:
                pass
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


puid = os.environ.get("PUID", "").strip()
pgid = os.environ.get("PGID", "").strip()
if os.getuid() == 0 and (puid or pgid):
    try:
        uid = int(puid or "0")
        gid = int(pgid or "0")
    except ValueError:
        print(f"ERROR: PUID/PGID must be numeric (got PUID={puid!r} PGID={pgid!r}).", flush=True)
        sys.exit(1)
    config_dir = os.path.dirname(os.environ.get("MEDIAREDUCER_CONFIG", "/config/config.json")) or "/config"
    if os.path.isdir(config_dir):
        # Always re-walk: a run without PUID, a root docker-exec, or a backup
        # restore can leave root-owned files inside a tree whose top level
        # still matches — a stale skip would break config saves and log
        # rotation. The walk is cheap relative to app startup.
        _chown_tree(config_dir, uid, gid)
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = "/tmp"
    print(f"Running as uid={uid} gid={gid} (PUID/PGID mapping).", flush=True)

os.execvp(sys.executable, [sys.executable, "app.py"])
