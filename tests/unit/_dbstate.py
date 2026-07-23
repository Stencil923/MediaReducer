"""Test helper: seed, read, and reset the SQLite store directly.

Most tests drive state through the engine/app helpers (save_pending,
write_plan_to_queue, load_cache, …) and don't need this. Reach for it only when a
test must plant or inspect raw store contents:

    seed(E.DB_FILE, {...})   # plant a composed store dict
    read(E.DB_FILE)          # read it back as one dict

seed() takes the composed store dict (code_checksum, config_hash,
last_cleanup_date, dashboard_stats, movies, library_snapshot, pending) and
decomposes it into the tables; read() composes it back with db.read_cache_dict.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import db  # noqa: E402


def reset(db_path) -> None:
    """Delete the store so a test starts from an empty slate."""
    for fp in db.db_files(db_path):
        try:
            fp.unlink()
        except FileNotFoundError:
            pass
    db.forget_initialized(db_path)


def seed(db_path, cache: dict) -> None:
    """Replace the whole store from a composed store dict, rewriting in place
    (not delete-and-recreate) so the generation counter keeps climbing and the
    app's read memo never sees two different seeds as the same version."""
    with db.transaction(db_path) as conn:
        conn.execute("DELETE FROM meta WHERE key != '_gen'")  # keep the memo generation climbing
        for table in ("metadata_cache", "movies", "queue"):
            conn.execute(f"DELETE FROM {table}")
        for key in ("code_checksum", "config_hash", "last_cleanup_date"):
            if key in cache:
                db.set_meta(conn, key, cache[key])
        if "dashboard_stats" in cache:
            db.set_meta(conn, "dashboard_stats", cache["dashboard_stats"])
        if isinstance(cache.get("movies"), dict):
            db.replace_metadata_cache(conn, cache["movies"])
        snap = cache.get("library_snapshot")
        if isinstance(snap, dict):
            db.replace_movies(conn, snap.get("movies") or [])
            db.set_meta(conn, "snapshot_built_at", snap.get("built_at", 0))
            if snap.get("monitor_dirs") is not None:
                db.set_meta(conn, "snapshot_monitor_dirs", snap["monitor_dirs"])
        pend = cache.get("pending")
        if isinstance(pend, dict):
            db.set_meta(conn, "pending_schema", pend.get("schema", 1))
            db.replace_queue(conn, pend.get("entries") or {})
            if pend.get("plan_config") is not None:
                db.set_meta(conn, "pending_plan_config", pend["plan_config"])
            if pend.get("monitor_dirs") is not None:
                db.set_meta(conn, "pending_monitor_dirs", pend["monitor_dirs"])


def read(db_path) -> dict:
    """The whole store composed as one dict."""
    return db.read_cache_dict(db_path)


def read_pending(db_path) -> dict:
    """Just the pending document {schema, entries, plan_config, monitor_dirs}."""
    return db.read_pending_doc(db_path)


def exists(db_path) -> bool:
    return Path(db_path).exists()
