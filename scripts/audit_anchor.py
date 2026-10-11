"""
AyurCore audit anchor -- checkpoints of each clinic's audit hash chain held
OUTSIDE the database (security review: "no external anchor" and "deleting
the most recent event would go undetected").

The hash chain in audit_events (backend/audit_store.py) detects an edited
event, but a chain has no memory of how long it used to be: delete the last
N events -- or recompute the whole chain after an edit -- and what remains
still verifies. Migration 014 blocks UPDATE/DELETE with triggers, but anyone
with write access to the database FILE can drop them. The remedy is a
record kept somewhere else.

    # append one signed checkpoint per clinic to an anchor file
    AYURCORE_AUDIT_ANCHOR_KEY=... python scripts/audit_anchor.py checkpoint --anchor-file /secure/place/ayurcore_audit_anchors.jsonl

    # verify the live database against every checkpoint ever written
    AYURCORE_AUDIT_ANCHOR_KEY=... python scripts/audit_anchor.py verify --anchor-file /secure/place/ayurcore_audit_anchors.jsonl

Each checkpoint line records, per clinic: event_count, last_event_id,
last_event_hash, anchored_at, signed with HMAC-SHA256 under
AYURCORE_AUDIT_ANCHOR_KEY (at least 32 characters; keep it out of the
database machine's reach where possible). verify reports a violation when:

  - a checkpoint line's signature does not verify (anchor file edited, or
    the wrong key);
  - an anchored event no longer exists (tail truncated / event deleted);
  - an anchored event's hash changed (chain rewritten or recomputed);
  - the number of events up to an anchored event_id changed (events
    deleted or inserted before the anchor);
  - the clinic's chain itself does not verify (verify_audit_integrity).

LIMIT, stated plainly: events appended AFTER the latest checkpoint are only
as safe as the in-database triggers and hash chain -- deleting them is
detectable only once a later checkpoint has recorded them. Checkpoint as
often as that window matters (e.g. hourly and after each clinic session),
and keep the anchor file where the database's operator cannot silently
rewrite it (another machine, append-only/WORM storage). Only clinic ids,
counts, event ids and hashes are written -- no patient data.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEY_ENV_VAR = "AYURCORE_AUDIT_ANCHOR_KEY"
MIN_KEY_LENGTH = 32


class AnchorError(Exception):
    """The anchor operation could not be carried out (missing/short key,
    unreadable anchor file). Never a verification verdict -- see AnchorReport."""


@dataclass
class AnchorReport:
    valid: bool
    checked_anchors: int
    violations: list = field(default_factory=list)


def _key(key: Optional[str]) -> bytes:
    key = key if key is not None else os.environ.get(KEY_ENV_VAR)
    if not key or len(key) < MIN_KEY_LENGTH:
        raise AnchorError(f"{KEY_ENV_VAR} must be set to a secret of at least {MIN_KEY_LENGTH} characters.")
    return key.encode("utf-8")


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sign(record: dict, key: bytes) -> str:
    return hmac.new(key, _canonical(record), hashlib.sha256).hexdigest()


def _connect(db_path: Optional[str]) -> sqlite3.Connection:
    if db_path is None:
        from db.database import DB_PATH
        db_path = DB_PATH
    # Read-only: an anchor operation never writes to the database.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def checkpoint(anchor_file: str, key: Optional[str] = None, db_path: Optional[str] = None) -> list[dict]:
    """Appends one signed checkpoint line per clinic that has audit events,
    and returns the records written. The anchor file is opened in append
    mode and fsync'd; nothing is ever rewritten."""
    secret = _key(key)
    anchored_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT a.clinic_id, COUNT(*) AS event_count, MAX(a.event_id) AS last_event_id
            FROM audit_events a GROUP BY a.clinic_id ORDER BY a.clinic_id
            """
        ).fetchall()
        records = []
        for row in rows:
            last_hash = conn.execute(
                "SELECT event_hash FROM audit_events WHERE event_id = ?", (row["last_event_id"],)
            ).fetchone()["event_hash"]
            records.append({
                "clinic_id": row["clinic_id"],
                "event_count": row["event_count"],
                "last_event_id": row["last_event_id"],
                "last_event_hash": last_hash,
                "anchored_at": anchored_at,
            })
    finally:
        conn.close()
    with open(anchor_file, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps({"record": record, "hmac": _sign(record, secret)}, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return records


def verify(anchor_file: str, key: Optional[str] = None, db_path: Optional[str] = None) -> AnchorReport:
    """Checks the database against EVERY checkpoint in the anchor file (not
    only the latest), so a truncation back past any earlier checkpoint is
    also caught."""
    secret = _key(key)
    if not os.path.exists(anchor_file):
        raise AnchorError(f"anchor file does not exist: {anchor_file}")
    from backend.audit_store import verify_audit_integrity

    violations: list[str] = []
    checked = 0
    conn = _connect(db_path)
    try:
        clinics_seen = set()
        with open(anchor_file, encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    record, signature = entry["record"], entry["hmac"]
                except (ValueError, KeyError, TypeError):
                    violations.append(f"line {line_no}: not a valid anchor record")
                    continue
                if not hmac.compare_digest(signature, _sign(record, secret)):
                    violations.append(f"line {line_no}: signature does not verify (anchor edited or wrong key)")
                    continue
                checked += 1
                clinic = record["clinic_id"]
                clinics_seen.add(clinic)
                row = conn.execute(
                    "SELECT event_hash, clinic_id FROM audit_events WHERE event_id = ?", (record["last_event_id"],)
                ).fetchone()
                if row is None or row["clinic_id"] != clinic:
                    violations.append(
                        f"line {line_no}: clinic {clinic!r} anchored event_id={record['last_event_id']} is missing "
                        "(events deleted or chain truncated)")
                    continue
                if row["event_hash"] != record["last_event_hash"]:
                    violations.append(
                        f"line {line_no}: clinic {clinic!r} event_id={record['last_event_id']} hash changed since it "
                        "was anchored (chain rewritten)")
                count = conn.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE clinic_id = ? AND event_id <= ?",
                    (clinic, record["last_event_id"]),
                ).fetchone()[0]
                if count != record["event_count"]:
                    violations.append(
                        f"line {line_no}: clinic {clinic!r} has {count} events up to event_id={record['last_event_id']}, "
                        f"anchored {record['event_count']} (events deleted or inserted before the anchor)")
        for clinic in sorted(clinics_seen):
            report = verify_audit_integrity(clinic, conn=conn)
            if not report.valid:
                violations.append(f"clinic {clinic!r}: hash chain does not verify ({len(report.violations)} violation(s))")
    finally:
        conn.close()
    return AnchorReport(valid=not violations, checked_anchors=checked, violations=violations)


def main() -> int:
    parser = argparse.ArgumentParser(description="Signed, off-database checkpoints of the AyurCore audit chain")
    parser.add_argument("command", choices=("checkpoint", "verify"))
    parser.add_argument("--anchor-file", required=True, help="append-only JSONL file kept outside the database")
    parser.add_argument("--db", default=None, help="database path (default: the application's database)")
    args = parser.parse_args()
    try:
        if args.command == "checkpoint":
            records = checkpoint(args.anchor_file, db_path=args.db)
            print(f"anchored {len(records)} clinic chain(s) to {args.anchor_file}")
            return 0
        report = verify(args.anchor_file, db_path=args.db)
    except AnchorError as e:
        print(f"ERROR: {e}")
        return 2
    print(f"checked {report.checked_anchors} anchor record(s): {'VALID' if report.valid else 'VIOLATIONS FOUND'}")
    for v in report.violations:
        print(f"  - {v}")
    return 0 if report.valid else 1


if __name__ == "__main__":
    sys.exit(main())
