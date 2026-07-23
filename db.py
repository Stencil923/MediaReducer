"""SQLite persistence for MediaReducer — shared by app.py (the Flask web server)
and engine.py (the deletion subprocess). Four datasets across four tables:

  meta            Small key/value pairs, values JSON-encoded: code_checksum,
                  config_hash, last_cleanup_date, dashboard_stats,
                  snapshot_built_at, snapshot_monitor_dirs, pending_schema,
                  pending_plan_config, pending_monitor_dirs, schema_version.
  metadata_cache  rating_key -> {protected, tmdb_id, imdb_id, v}: the slow
                  get_metadata results, so a rescan skips those API calls.
  movies          The library snapshot: one row per scored movie, ordered by
                  `ord` (scan/score order). `ord` is the primary key, not path,
                  so the list round-trips exactly — duplicate or NULL paths are
                  allowed.
  queue           The marked & eligible deletion queue, keyed by path, ordered
                  by `ord` (deletion order).

CONCURRENCY. The engine subprocess and Flask request threads both open the same
.db file. WAL mode lets readers run without blocking the single writer, and
busy_timeout retries the rare two-writer overlap instead of raising
SQLITE_BUSY — so writes serialize and readers always see a consistent snapshot.
The combined queue+snapshot write is one multi-table transaction (see
save_pending in engine.py).

Every operation opens a short-lived connection (connect()/transaction()) rather
than sharing one across threads — sqlite3 connections are not thread-safe and a
local-file open is cheap. WAL is a persistent property of the DB file (set
once, sticks); busy_timeout is per connection, so both are (re)applied on each
connect. WAL leaves `-wal` / `-shm` sidecar files next to the .db; they live on
the same /config volume and are cleared alongside the .db on a reset.
"""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

# Human-readable marker of the table layout, stored in meta for diagnostics. The
# actual "did the schema change?" decision is automatic — see _schema_fingerprint
# / _guard_schema_fingerprint below — so this never needs to be bumped by hand.
SCHEMA_VERSION = 1

# Retry a contended write lock for up to this long before surfacing SQLITE_BUSY.
# The only real contention is the engine mid-run overlapping an app request that
# mutates state (daily-window burn/reopen, queue clear) — brief, so a short
# retry window absorbs it and the two writers serialize instead of one failing.
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS metadata_cache (
    rating_key TEXT PRIMARY KEY,
    protected  INTEGER,
    -- tmdb_id / imdb_id carry whatever the media APIs returned (int OR str), so
    -- they are declared with NO type = BLOB affinity: SQLite stores each value in
    -- its own storage class and never coerces, so an id round-trips as the exact
    -- int-or-str the media API returned (a TEXT column would turn an int id into a str).
    tmdb_id,
    imdb_id,
    v          INTEGER
);
CREATE TABLE IF NOT EXISTS movies (
    ord          INTEGER PRIMARY KEY,
    path         TEXT,
    title        TEXT,
    year         INTEGER,
    rating       REAL,
    votes        INTEGER,
    plays        INTEGER,
    users        INTEGER,
    last_played  INTEGER,
    size_gb      REAL,
    -- size_bytes is the exact file size; size_gb (2 dp) is kept for display. The
    -- config-save reconcile sizes its deletion plan from the exact bytes.
    size_bytes   INTEGER,
    added_at     INTEGER,
    protected    INTEGER,
    favorite     INTEGER,
    -- Set when the scan excluded the movie for a reason the reconcile can't
    -- recompute from stored facts (a Plex/Jellyfin identity mismatch): the
    -- reconcile treats such a row as ineligible so it can't be re-admitted.
    excluded     INTEGER,
    source_id    TEXT,
    jf_source_id TEXT,
    -- tmdb_id / section_id are the Radarr identity an incremental forget needs, so
    -- a queue the reconcile rebuilds from the snapshot keeps it. Untyped = BLOB
    -- affinity to round-trip the exact int-or-str the media API returned.
    tmdb_id,
    section_id
);
CREATE INDEX IF NOT EXISTS idx_movies_path ON movies(path);
CREATE TABLE IF NOT EXISTS queue (
    path       TEXT PRIMARY KEY,
    ord        INTEGER,
    title      TEXT,
    score      REAL,
    size_bytes INTEGER,
    marked_at  REAL,
    -- tmdb_id / section_id are the Radarr identity the incremental delete forgets
    -- the movie by; the media APIs hand them back as int OR str, so (like the
    -- metadata cache) they get NO type = BLOB affinity to round-trip the exact
    -- int-or-str the API returned.
    tmdb_id,
    section_id
);
CREATE INDEX IF NOT EXISTS idx_queue_ord ON queue(ord);
"""

# Sentinel so meta reads can tell "key absent" from "key stored as None/JSON
# null" — the composed dict must omit a section that was never written, exactly
# like a JSON file that never had the key.
_MISSING = object()

# Paths whose schema/WAL have already been initialised this process, so the hot
# read path doesn't re-run CREATE/PRAGMA on every connect. Keyed by db path
# string; tests that repoint the path get a fresh init. Guarded by _init_lock so
# two request threads first-connecting at once can't both run the schema rebuild.
_initialized: set = set()
_init_lock = threading.Lock()

_SCHEMA_FINGERPRINT = None


def _schema_fingerprint() -> str:
    """SHA-256 of THIS file's source — db.py owns the schema AND the row<->dict
    mapping, so any change here can shift the persisted shape. It's the DB analog
    of the engine's code_checksum (which tracks engine.py), and the two together
    cover the only files that define what's stored. Checked at connect and, on a
    change, the code-derived tables are rebuilt (DROP+recreate) — a column change
    needs more than the row-wipe the engine guard does. Computed once per process."""
    global _SCHEMA_FINGERPRINT
    if _SCHEMA_FINGERPRINT is None:
        try:
            _SCHEMA_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        except Exception:
            # Unreadable source: fall back to a constant so the store still works
            # (like the engine's code_checksum) rather than rebuilding every boot.
            _SCHEMA_FINGERPRINT = "unknown"
    return _SCHEMA_FINGERPRINT


def _apply_pragmas_and_schema(conn, path_key: str) -> None:
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    if path_key in _initialized:
        return
    # Double-checked: the fast path above never takes the lock, so already-init
    # paths (the common case) pay nothing. Only a first connect enters here, and
    # the lock serialises concurrent first connects so exactly one runs the schema
    # setup / fingerprint rebuild while the others wait and then see it done.
    with _init_lock:
        if path_key in _initialized:
            return
        # journal_mode=WAL can't run inside a transaction; a fresh connection has
        # none open. synchronous=NORMAL is the WAL-safe durability level (a crash
        # can lose the last commit but never corrupts — acceptable for a cache the
        # next scan/Simulate repopulates).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(SCHEMA_VERSION),),
        )
        _guard_schema_fingerprint(conn)
        conn.commit()
        _initialized.add(path_key)


def _guard_schema_fingerprint(conn) -> None:
    """If db.py (the schema + row mapping) changed since this store was written,
    its tables may not match the current column layout — CREATE TABLE IF NOT
    EXISTS won't alter an existing table, so rebuild the code-derived tables from
    scratch. Runs once per process on the first connect, BEFORE any read, so both
    the app and the engine are protected. last_cleanup_date (the daily schedule)
    survives; the rebuilt cache is repopulated by the next scan/Simulate."""
    current = _schema_fingerprint()
    stored = _get_meta_opt(conn, "schema_fingerprint")
    if stored == current:
        return
    # A brand-new empty store just gets stamped; one that already holds data
    # written by a different db.py layout is rebuilt. (A store that predates this
    # guard has no fingerprint but may carry data — code_checksum tells them apart.)
    has_data = _get_meta_opt(conn, "code_checksum") is not _MISSING
    if stored is not _MISSING or has_data:
        kept_date = get_meta(conn, "last_cleanup_date")
        for table in ("metadata_cache", "movies", "queue"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for key in _CODE_DERIVED_META + ("code_checksum",):
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
        conn.executescript(_SCHEMA)   # recreate the dropped tables at the current layout
        if kept_date is not None:
            set_meta(conn, "last_cleanup_date", kept_date)
        # This rebuild empties the data tables but runs outside transaction() (which
        # is what normally bumps _gen), so advance the generation here too — else an
        # app that already memoized the pre-rebuild snapshot keeps serving it (the
        # WAL main-file size may not change to invalidate the file-identity memo).
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('_gen', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
    set_meta(conn, "schema_fingerprint", current)


@contextmanager
def connect(db_path):
    """A short-lived connection with busy_timeout + WAL + schema ensured. Use for
    reads; for a multi-statement write use transaction() so it commits/rolls back
    as a unit. The caller must commit its own writes made directly on a connect()
    connection."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    key = str(p)
    conn = sqlite3.connect(key, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas_and_schema(conn, key)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(db_path):
    """connect() wrapped in an immediate write transaction (rollback on error).
    BEGIN IMMEDIATE takes the write lock up front so two writers serialize via
    busy_timeout instead of one failing mid-way.

    On any transaction that actually changed a row, bumps the `_gen` generation
    counter before commit, so the app's read memo has a reliable change signal
    (see data_generation / data_fingerprint) — file mtime is too coarse for rapid
    writes and PRAGMA data_version resets on every fresh connection."""
    with connect(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            before = conn.total_changes
            yield conn
            if conn.total_changes != before:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('_gen', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def data_generation(db_path) -> int:
    """The store's monotonic write generation (`_gen` meta), bumped by every
    mutating transaction. 0 on an empty/absent store. Cross-process: an engine
    write and an app write both advance it, so either side's next read sees the
    change."""
    if not Path(db_path).exists():
        return 0        # absent store: a read (e.g. a status poll) mustn't create it
    try:
        with connect(db_path) as conn:
            return get_meta(conn, "_gen", 0)
    except Exception:
        return 0


def data_fingerprint(db_path):
    """A value that changes whenever the store's contents change, for the app's
    read memo. Combines the file identity (device, inode, size — so a
    delete-and-recreate reads as new) with the in-DB generation counter (so an
    in-place rewrite reads as new even when the file identity is unchanged and
    mtime hasn't ticked)."""
    p = Path(db_path)
    try:
        s = p.stat()
        ident = (s.st_dev, s.st_ino, s.st_size)
    except OSError:
        ident = None
    return (ident, data_generation(db_path))


# ── meta key/value helpers (values are JSON-encoded) ──────────────────────────

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def _get_meta_opt(conn, key):
    """Like get_meta but returns the _MISSING sentinel when the key is absent, so
    a section written as an explicit null still round-trips."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return _MISSING
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return _MISSING


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def del_meta(conn, key):
    conn.execute("DELETE FROM meta WHERE key=?", (key,))


# ── code-change guard (mirrors the old load_cache / _cache_base_for_merge) ─────

# Everything derived from engine code — flushed on a code_checksum mismatch so a
# code change rebuilds it from a fresh scan rather than trusting a differently-
# shaped cache. last_cleanup_date is deliberately NOT here: the daily schedule
# must survive a code change.
_CODE_DERIVED_META = (
    "config_hash", "dashboard_stats",
    "snapshot_built_at", "snapshot_monitor_dirs",
    "pending_schema", "pending_plan_config", "pending_monitor_dirs",
)


def ensure_code_current(conn, current_checksum: str) -> None:
    """Engine-side guard: if the stored code_checksum doesn't match the running
    engine, drop every code-derived section (metadata cache, snapshot, queue,
    their meta) and re-stamp — keeping only last_cleanup_date. Called at the
    start of every engine write so no stale section survives a code change. The
    app never calls this (it has no checksum; it reads whatever the engine last
    wrote)."""
    if get_meta(conn, "code_checksum") == current_checksum:
        return
    conn.execute("DELETE FROM metadata_cache")
    conn.execute("DELETE FROM movies")
    conn.execute("DELETE FROM queue")
    for k in _CODE_DERIVED_META:
        conn.execute("DELETE FROM meta WHERE key=?", (k,))
    set_meta(conn, "code_checksum", current_checksum)


# ── row <-> dict mapping (the documented dict shapes) ─────────────────────────

def _movie_row_to_dict(r) -> dict:
    return {
        "path": r["path"],
        "title": r["title"] or "",
        "year": r["year"],
        "rating": r["rating"],
        "votes": r["votes"],
        "plays": r["plays"],
        "users": r["users"],
        "last_played": r["last_played"],
        "added_at": r["added_at"],
        "source_id": r["source_id"],
        "jf_source_id": r["jf_source_id"],
        "size_gb": r["size_gb"],
        "size_bytes": r["size_bytes"],
        "protected": bool(r["protected"]),
        "favorite": bool(r["favorite"]),
        "excluded": bool(r["excluded"]),
        "tmdb_id": r["tmdb_id"],
        "section_id": r["section_id"],
    }


def _queue_row_to_entry(r) -> dict:
    return {
        "title": r["title"],
        "score": r["score"],
        "size_bytes": r["size_bytes"],
        "marked_at": r["marked_at"],
        "tmdb_id": r["tmdb_id"],
        "section_id": r["section_id"],
    }


# ── section writers ───────────────────────────────────────────────────────────

def replace_metadata_cache(conn, movies: dict) -> None:
    """Replace the whole metadata cache from a {rating_key: {...}} dict. Written
    whole once per scan, so a full replace (not upsert) mirrors the old
    clear-and-rewrite and drops entries the caller removed."""
    conn.execute("DELETE FROM metadata_cache")
    conn.executemany(
        "INSERT INTO metadata_cache(rating_key, protected, tmdb_id, imdb_id, v) "
        "VALUES(?, ?, ?, ?, ?)",
        [(str(rk),
          1 if e.get("protected") else 0,
          e.get("tmdb_id"), e.get("imdb_id"), e.get("v"))
         for rk, e in (movies or {}).items() if isinstance(e, dict)],
    )


def replace_movies(conn, movies_list) -> None:
    """Replace the library snapshot from an ordered list of _snapshot_entry dicts.
    ord preserves list order; path is indexed but not unique."""
    conn.execute("DELETE FROM movies")
    conn.executemany(
        "INSERT INTO movies(ord, path, title, year, rating, votes, plays, users, "
        "last_played, size_gb, size_bytes, added_at, protected, favorite, excluded, "
        "source_id, jf_source_id, tmdb_id, section_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(i, m.get("path"), m.get("title") or "", m.get("year"), m.get("rating"),
          m.get("votes"), m.get("plays"), m.get("users"), m.get("last_played"),
          m.get("size_gb"), m.get("size_bytes"), m.get("added_at"),
          1 if m.get("protected") else 0, 1 if m.get("favorite") else 0,
          1 if m.get("excluded") else 0,
          m.get("source_id"), m.get("jf_source_id"), m.get("tmdb_id"), m.get("section_id"))
         for i, m in enumerate(movies_list or []) if isinstance(m, dict)],
    )


def delete_movies(conn, paths) -> None:
    """Remove library-snapshot rows whose path is in `paths`. Used when a run
    confirms a file is physically gone (vanished outside MediaReducer): the light
    upkeep and fast-delete paths don't rewrite the whole snapshot, so without this
    a deleted title would linger as a phantom row until the next full scan."""
    rows = [(str(p),) for p in (paths or ()) if p]
    if not rows:
        return
    conn.executemany("DELETE FROM movies WHERE path=?", rows)


def update_movie_watch(conn, updates: dict) -> None:
    """Apply incremental fresh watch values (plays / last_played / favorite) onto
    matching snapshot rows by path, leaving built_at/monitor_dirs untouched (a
    watch refresh, not a rescan). Mirrors _merge_snapshot_watch_updates so the
    queue's refreshed scores and the snapshot's refreshed plays can commit in the
    same save_pending transaction."""
    for path, u in (updates or {}).items():
        if not isinstance(u, dict):
            continue
        sets, vals = [], []
        for field in ("plays", "last_played", "favorite"):
            if field in u:
                sets.append(f"{field}=?")
                vals.append((1 if u[field] else 0) if field == "favorite" else u[field])
        if not sets:
            continue
        vals.append(path)
        conn.execute(f"UPDATE movies SET {', '.join(sets)} WHERE path=?", vals)


def replace_queue(conn, entries: dict) -> None:
    """Replace the whole marked & eligible queue from a {path: entry} dict,
    preserving the dict's iteration order (the deletion order) via `ord`."""
    conn.execute("DELETE FROM queue")
    conn.executemany(
        # OR REPLACE: if two source entries stringify to the same path, keep the
        # last rather than aborting the whole write on the PRIMARY KEY conflict.
        "INSERT OR REPLACE INTO queue(path, ord, title, score, size_bytes, marked_at, "
        "tmdb_id, section_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        [(str(path), i, e.get("title"), e.get("score"), e.get("size_bytes"),
          e.get("marked_at"), e.get("tmdb_id"), e.get("section_id"))
         for i, (path, e) in enumerate((entries or {}).items()) if isinstance(e, dict)],
    )


# ── section readers (compose the dict shapes) ─────────────────────────────────

def read_metadata_cache(conn) -> dict:
    rows = conn.execute(
        "SELECT rating_key, protected, tmdb_id, imdb_id, v FROM metadata_cache"
    ).fetchall()
    return {
        r["rating_key"]: {
            "protected": bool(r["protected"]),
            "tmdb_id": r["tmdb_id"],
            "imdb_id": r["imdb_id"],
            "v": r["v"],
        }
        for r in rows
    }


def read_snapshot(conn):
    """The library_snapshot envelope {built_at, monitor_dirs, movies:[...]}, or
    None when no scan has written one."""
    built_at = _get_meta_opt(conn, "snapshot_built_at")
    if built_at is _MISSING:
        return None
    rows = conn.execute("SELECT * FROM movies ORDER BY ord").fetchall()
    return {
        "built_at": built_at,
        "monitor_dirs": get_meta(conn, "snapshot_monitor_dirs", []),
        "movies": [_movie_row_to_dict(r) for r in rows],
    }


def read_pending_doc(db_path) -> dict:
    """The marked & eligible queue document {schema, entries, plan_config,
    monitor_dirs}, or {} when no plan has been written. Targeted read (queue
    table + pending meta only) so the frequent pending consults don't drag in the
    whole movie snapshot."""
    if not Path(db_path).exists():
        return {}
    with connect(db_path) as conn:
        schema = _get_meta_opt(conn, "pending_schema")
        if schema is _MISSING:
            return {}
        rows = conn.execute("SELECT * FROM queue ORDER BY ord").fetchall()
        doc = {"schema": schema,
               "entries": {r["path"]: _queue_row_to_entry(r) for r in rows}}
        plan_config = _get_meta_opt(conn, "pending_plan_config")
        if plan_config is not _MISSING:
            doc["plan_config"] = plan_config
        monitor_dirs = _get_meta_opt(conn, "pending_monitor_dirs")
        if monitor_dirs is not _MISSING:
            doc["monitor_dirs"] = monitor_dirs
        return doc


def _compose(conn) -> dict:
    """Compose the whole store as one dict from all tables. Sections never
    written are omitted (the composed dict carries only the keys actually set),
    so every dict-shaped caller (load_cache, the app's memoized read) is simple."""
    out = {}
    for key in ("code_checksum", "config_hash", "last_cleanup_date", "dashboard_stats"):
        v = _get_meta_opt(conn, key)
        if v is not _MISSING:
            out[key] = v
    metadata = read_metadata_cache(conn)
    if metadata:
        out["movies"] = metadata
    snap = read_snapshot(conn)
    if snap is not None:
        out["library_snapshot"] = snap
    schema = _get_meta_opt(conn, "pending_schema")
    if schema is not _MISSING:
        rows = conn.execute("SELECT * FROM queue ORDER BY ord").fetchall()
        doc = {"schema": schema,
               "entries": {r["path"]: _queue_row_to_entry(r) for r in rows}}
        plan_config = _get_meta_opt(conn, "pending_plan_config")
        if plan_config is not _MISSING:
            doc["plan_config"] = plan_config
        monitor_dirs = _get_meta_opt(conn, "pending_monitor_dirs")
        if monitor_dirs is not _MISSING:
            doc["monitor_dirs"] = monitor_dirs
        out["pending"] = doc
    return out


def read_cache_dict(db_path) -> dict:
    """The whole store composed as a single dict. Used by the app's memoized read;
    the engine uses load_cache() (same shape, plus the code guard)."""
    if not Path(db_path).exists():
        return {}        # absent store composes to the empty dict — don't create it
    with connect(db_path) as conn:
        return _compose(conn)


def db_files(db_path):
    """The .db and its WAL/SHM sidecars — everything to unlink for a full reset."""
    p = Path(db_path)
    return [p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")]


def reset_store(db_path) -> None:
    """Delete the store (and its WAL/SHM sidecars) and drop its init memo, so the
    next connect rebuilds a fresh schema — used by Clear Cache.

    Held under _init_lock, forgetting BEFORE unlinking, so a concurrent request
    thread can't see a table-less DB: one that already opened its connection keeps
    reading the old (unlinked but still-open) file, and one that connects after is
    no longer in the init memo, so it recreates the schema instead of assuming it
    exists. Missing files are ignored (a concurrent reset already removed them)."""
    with _init_lock:
        _initialized.discard(str(Path(db_path)))
        for fp in db_files(db_path):
            try:
                fp.unlink()
            except FileNotFoundError:
                pass


def forget_initialized(db_path=None) -> None:
    """Drop the per-path init memo so the next connect re-creates schema + WAL.
    Used by tests that repoint the path (no argument clears every path)."""
    with _init_lock:
        if db_path is None:
            _initialized.clear()
        else:
            _initialized.discard(str(Path(db_path)))
