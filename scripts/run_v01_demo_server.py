#!/usr/bin/env python3
"""
DEMO ONLY: serve the AyurCore API against the disposable v0.1 demo database.

    ./venv/bin/python scripts/setup_v01_demo.py         # once
    ./venv/bin/python scripts/run_v01_demo_server.py    # http://127.0.0.1:8000

The database must be one made by scripts/setup_v01_demo.py; it is checked with
the walkthrough's open_demo_database (setup's location rules: inside this
checkout, not under db/ or .git, not named ayurcore.db, ends in .db; plus it
must exist with setup's credentials file next to it). db/ayurcore.db and
anything outside this checkout are refused.

db.database.DB_PATH is pointed at the demo database in this process only, and
the app is served from that same process (uvicorn.run with the app object, no
reload), so the application's default database is never opened. The server
listens on 127.0.0.1 only. Demo logins are in the credentials file next to
the database; they are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db.database as database  # noqa: E402
from scripts.demo_v01_walkthrough import open_demo_database  # noqa: E402
from scripts.setup_v01_demo import CREDENTIALS_FILENAME, DEFAULT_DB_PATH, UnsafeDemoPathError  # noqa: E402

HOST = "127.0.0.1"


def main(argv: list[str] | None = None, serve=None, root: str = ROOT) -> int:
    """`serve` and `root` exist for tests only; the command line always uses
    uvicorn.run and this checkout."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="demo database made by setup_v01_demo.py")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        resolved, _ = open_demo_database(args.db, root)
    except UnsafeDemoPathError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2

    print("=" * 64)
    print("  AyurCore v0.1 -- DEMO ONLY. Synthetic data; not for clinical use.")
    print(f"  Database:    {resolved}")
    print(f"  Logins:      {os.path.join(os.path.dirname(resolved), CREDENTIALS_FILENAME)}")
    print(f"  Serving on:  http://{HOST}:{args.port}")
    print("=" * 64)

    saved_db_path = database.DB_PATH
    database.DB_PATH = resolved
    try:
        import backend.main as main_module
        if serve is None:
            import uvicorn
            serve = uvicorn.run
        serve(main_module.app, host=HOST, port=args.port)
    finally:
        database.DB_PATH = saved_db_path
    return 0


if __name__ == "__main__":
    sys.exit(main())
