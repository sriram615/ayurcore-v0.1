import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'ayurcore.db')

DEFAULT_CLINIC_ID = "default_clinic"


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    """Brings this database's schema fully up to date.

    Phase A (migration-infrastructure milestone): this function used to
    contain the CREATE TABLE statements directly, plus a hand-rolled
    `_ensure_column()` additive-backfill helper -- a reasonable pattern for
    a one-developer prototype, but not a real migration system (no
    versioning, no record of what had been applied where, no way to add a
    second schema change safely). That schema now lives in
    migrations/001_baseline_schema.sql and
    migrations/002_add_clinic_id_to_panchakarma_resources.sql; this
    function is kept as a stable entry point -- anything that already
    calls `init_db()`, including `python db/database.py` below, keeps
    working unchanged -- but now delegates to the real migration runner.
    See db/migrations.py's module docstring for what that runner actually
    guarantees: idempotent, transactional per migration, safe on both an
    empty database and this project's real existing one, and it detects
    (rather than silently allowing) an already-applied migration file
    being edited after the fact.
    """
    from db.migrations import run_migrations
    run_migrations()


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
