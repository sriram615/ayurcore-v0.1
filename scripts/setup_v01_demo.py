#!/usr/bin/env python3
"""
AyurCore v0.1 demo setup: a fresh, disposable demo database inside this
checkout -- never the application's database.

    ./venv/bin/python scripts/setup_v01_demo.py              # demo/ayurcore_v01_demo.db
    ./venv/bin/python scripts/setup_v01_demo.py --db demo/other.db

In that new database it:
  1. applies every migration with the existing runner (db.migrations.run_migrations),
     with the same audit-chain integrity guard the application uses at startup;
  2. creates one DOCTOR, one ADMIN and one AUDITOR with backend.auth_service.create_user
     (Argon2id; a random password each);
  3. registers one synthetic patient with backend.patient_store.create_patient;
  4. inserts a small synthetic stock for the demo clinic (columns as in db/seed.py,
     which is not reused: it deletes ALL inventory rows and only seeds default_clinic).

Nothing here is clinical data: the patient and stock are invented for a demo.

PATH SAFETY (validate_demo_db_path) -- the database path is refused unless:
  - after resolving symlinks it lies strictly inside this checkout;
  - it is not under the checkout's db/ directory or .git, and is not named
    ayurcore.db anywhere (the application's database, db/ayurcore.db, and the
    stray root ayurcore.db);
  - it ends in .db;
  - neither it nor its -wal/-shm/-journal companions exist (a fresh database
    only; nothing is ever overwritten -- delete an old demo database by hand).

The database is reached by pointing db.database.DB_PATH at it for the duration
of the setup (restored afterwards), so the reused functions write there and
nowhere else. If setup fails, the files this run created are removed.

Passwords are not printed: they are written to credentials.v01-demo.json next
to the database (mode 0600; ignored by Git via credentials*.json, the
database via *.db).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db.database as database  # noqa: E402  (needs ROOT on sys.path)

DEFAULT_DB_PATH = os.path.join(ROOT, "demo", "ayurcore_v01_demo.db")
CREDENTIALS_FILENAME = "credentials.v01-demo.json"
DB_COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")
FORBIDDEN_BASENAME = "ayurcore.db"

DEMO_CLINIC_ID = "demo_clinic"
DEMO_USERS = (
    ("v01_doctor", "DOCTOR"),
    ("v01_admin", "ADMIN"),
    ("v01_auditor", "AUDITOR"),
)
DEMO_PATIENT = {"clinic_patient_id": "DEMO-0001", "display_name": "Demo Patient (synthetic)", "phone": None}
# Synthetic stock only. Names are canonical formulary names (backend/herb_resolver.py);
# botanical names as in db/seed.py.
DEMO_STOCK = (
    ("Ashwagandha", "Withania somnifera", 2000, "DEMO-ASH-01", 95),
    ("Brahmi", "Bacopa monnieri", 1500, "DEMO-BRA-01", 92),
    ("Shatavari", "Asparagus racemosus", 1500, "DEMO-SHA-01", 94),
    ("Triphala", "Emblica officinalis, Terminalia chebula, Terminalia bellirica", 2000, "DEMO-TRI-01", 90),
)


class UnsafeDemoPathError(ValueError):
    """The requested demo database path is not allowed."""


def _inside(path: str, directory: str) -> bool:
    return os.path.commonpath([path, directory]) == directory


def check_demo_location(path: str, root: str = ROOT) -> str:
    """The location rules only (inside the checkout; not under db/ or .git;
    not named ayurcore.db; not the application database; ends in .db).
    Returns the resolved path or raises UnsafeDemoPathError. Shared with
    scripts/demo_v01_walkthrough.py, which opens an EXISTING demo database."""
    root = os.path.realpath(root)
    resolved = os.path.realpath(os.path.abspath(path))
    if resolved == root or not _inside(resolved, root):
        raise UnsafeDemoPathError(f"{resolved} is not inside this checkout ({root}).")
    for protected in ("db", ".git"):
        # Case-insensitive: on a case-insensitive filesystem (macOS default)
        # DB/ and .GIT/ are the same directories as db/ and .git/.
        if _inside(resolved.lower(), os.path.join(root, protected).lower()):
            raise UnsafeDemoPathError(f"{resolved} is under {protected}/; use a separate directory such as demo/.")
    if os.path.basename(resolved).lower() == FORBIDDEN_BASENAME:
        raise UnsafeDemoPathError(f"{resolved} is named {FORBIDDEN_BASENAME}, the application database's name.")
    if os.path.realpath(database.DB_PATH) == resolved:
        raise UnsafeDemoPathError(f"{resolved} is the application database.")
    if not resolved.endswith(".db"):
        raise UnsafeDemoPathError(f"{resolved} must end in .db.")
    return resolved


def validate_demo_db_path(path: str, root: str = ROOT) -> str:
    """Returns the resolved, absolute path for a NEW demo database, or raises
    UnsafeDemoPathError. See the module docstring for the rules."""
    if os.path.lexists(path):
        raise UnsafeDemoPathError(f"{path} already exists; this script only creates a fresh database.")
    resolved = check_demo_location(path, root)
    for candidate in (resolved, *(resolved + s for s in DB_COMPANION_SUFFIXES)):
        if os.path.lexists(candidate):
            raise UnsafeDemoPathError(f"{candidate} already exists; this script only creates a fresh database.")
    return resolved


def setup_demo(db_path: str = DEFAULT_DB_PATH, root: str = ROOT) -> dict:
    """Creates and fills the demo database. Returns a summary without passwords."""
    target = validate_demo_db_path(db_path, root)
    credentials_path = os.path.join(os.path.dirname(target), CREDENTIALS_FILENAME)
    if os.path.lexists(credentials_path):
        raise UnsafeDemoPathError(f"{credentials_path} already exists; remove the old demo setup first.")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # Re-check after creating the directory (a symlink created in between would show here).
    target = validate_demo_db_path(target, root)

    created = [target, *(target + s for s in DB_COMPANION_SUFFIXES), credentials_path]
    saved_db_path = database.DB_PATH
    database.DB_PATH = target
    try:
        summary = _populate(target, credentials_path)
    except BaseException:
        for path in created:
            if os.path.lexists(path):
                os.remove(path)
        raise
    finally:
        database.DB_PATH = saved_db_path
    return summary


def _populate(target: str, credentials_path: str) -> dict:
    from backend.audit_store import audit_chain_validity
    from backend.auth_service import create_user
    from backend.patient_store import create_patient
    from db.migrations import discover_migrations, run_migrations

    applied = run_migrations(integrity_guard=audit_chain_validity)
    expected = [m.version for m in discover_migrations()]
    if applied != expected:
        raise RuntimeError(f"Expected migrations {expected} to be applied, got {applied}.")

    credentials = []
    user_ids = {}
    for username, role in DEMO_USERS:
        password = secrets.token_urlsafe(18)
        user_ids[role] = create_user(username, password, DEMO_CLINIC_ID, role=role)
        credentials.append({"username": username, "role": role, "password": password})

    patient = create_patient(clinic_id=DEMO_CLINIC_ID, created_by_user_id=user_ids["DOCTOR"], **DEMO_PATIENT)

    conn = database.get_connection()
    try:
        conn.executemany(
            "INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(DEMO_CLINIC_ID, *row) for row in DEMO_STOCK],
        )
        conn.commit()
    finally:
        conn.close()

    fd = os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"database": target, "clinic_id": DEMO_CLINIC_ID, "users": credentials}, f, indent=2)

    return {
        "database": target,
        "credentials_file": credentials_path,
        "migrations_applied": applied,
        "clinic_id": DEMO_CLINIC_ID,
        "users": [{"username": u, "role": r} for u, r in DEMO_USERS],
        "patient": {"patient_pk": patient["patient_pk"], "clinic_patient_id": patient["clinic_patient_id"]},
        "stock": {name: grams for name, _, grams, _, _ in DEMO_STOCK},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="demo database path inside this checkout")
    args = parser.parse_args(argv)
    try:
        summary = setup_demo(args.db)
    except UnsafeDemoPathError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
