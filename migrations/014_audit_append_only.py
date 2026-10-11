"""
Migration 014 -- make audit_events append-only at the database level.

Until now nothing in the database stopped an UPDATE or DELETE on
audit_events; the hash chain only DETECTED an in-place edit afterwards, and
deleting the most recent event(s) was not detectable at all. These triggers
reject every UPDATE and DELETE on audit_events, from any connection,
regardless of PRAGMA foreign_keys.

Scope, stated plainly: an attacker with write access to the database FILE
can drop these triggers first. They stop accidental and application-level
modification and raise the bar for deliberate tampering; detecting a
deliberate tail truncation needs an anchor held outside the database --
see scripts/audit_anchor.py. No legitimate code path updates or deletes
audit events (backend/audit_store.py exposes one append operation).
DROP TABLE (used by migration 011's rebuild) does not fire DELETE triggers.
"""

STATEMENTS = [
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events_append_only');
    END
    """,
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
