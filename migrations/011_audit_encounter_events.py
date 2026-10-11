"""
Migration 011 -- allow encounter lifecycle events in the audit chain.

migrations/007_audit_events.sql allowed exactly one event type,
PRESCRIPTION_CONFIRMED, and made prescription_id / preview_id NOT NULL.
Encounter open/close (migration 010) must be audited too, so this migration
rebuilds audit_events with:

  - event_type IN ('PRESCRIPTION_CONFIRMED', 'ENCOUNTER_OPENED', 'ENCOUNTER_CLOSED')
  - prescription_id / preview_id nullable, but a CHECK keeps them exactly as
    strict as before for PRESCRIPTION_CONFIRMED, and requires both to be NULL
    for the encounter events (whose encounter_pk lives in the hashed
    event_data instead -- see backend/audit_store.py).

SQLite cannot relax a CHECK or a NOT NULL in place, so this is the standard
create-copy-drop-rename rebuild. Every existing row is copied VERBATIM --
event_id, event_hash, previous_event_hash, event_data_json and every other
column -- so every existing hash still verifies with the unchanged
algorithm (backend/audit_store._canonical_envelope is not changed). The
migration refuses to finish (raising, so the runner rolls it back) unless
the copy has the same row count and the same ordered sequence of
(event_id, event_hash, previous_event_hash) as the original.

The three existing indexes are recreated under their original names. No
other table references audit_events, so the rename breaks no foreign key.
"""
import hashlib

_COLUMNS = ("event_id, clinic_id, actor_user_id, event_type, prescription_id, preview_id, "
            "event_data_json, created_at, previous_event_hash, event_hash")

_CREATE = """
CREATE TABLE audit_events_new (
    event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    clinic_id            TEXT    NOT NULL,
    actor_user_id        INTEGER NOT NULL REFERENCES users(user_id),
    event_type           TEXT    NOT NULL
        CHECK (event_type IN ('PRESCRIPTION_CONFIRMED', 'ENCOUNTER_OPENED', 'ENCOUNTER_CLOSED')),
    prescription_id      INTEGER REFERENCES prescriptions(id),
    preview_id           TEXT    REFERENCES prescription_previews(preview_id),
    event_data_json      TEXT    NOT NULL,
    created_at           TEXT    NOT NULL,
    previous_event_hash  TEXT,
    event_hash           TEXT    NOT NULL,
    CHECK (
        (event_type = 'PRESCRIPTION_CONFIRMED' AND prescription_id IS NOT NULL AND preview_id IS NOT NULL)
        OR (event_type IN ('ENCOUNTER_OPENED', 'ENCOUNTER_CLOSED') AND prescription_id IS NULL AND preview_id IS NULL)
    )
)
"""

_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_events_prescription_id ON audit_events(prescription_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_events_clinic_id_event_id ON audit_events(clinic_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_events_actor_user_id ON audit_events(actor_user_id)",
]


def _fingerprint(conn, table: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f"SELECT event_id, event_hash, previous_event_hash FROM {table} ORDER BY event_id"):
        count += 1
        digest.update(repr(tuple(row)).encode("utf-8"))
    return count, digest.hexdigest()


def upgrade(conn):
    before = _fingerprint(conn, "audit_events")
    conn.execute(_CREATE)
    conn.execute(f"INSERT INTO audit_events_new ({_COLUMNS}) SELECT {_COLUMNS} FROM audit_events")
    copied = _fingerprint(conn, "audit_events_new")
    if copied != before:
        raise RuntimeError(f"audit_events copy mismatch: {before} != {copied}; migration aborted")
    conn.execute("DROP TABLE audit_events")
    conn.execute("ALTER TABLE audit_events_new RENAME TO audit_events")
    for statement in _INDEXES:
        conn.execute(statement)
    after = _fingerprint(conn, "audit_events")
    if after != before:
        raise RuntimeError(f"audit_events changed during rebuild: {before} != {after}; migration aborted")
