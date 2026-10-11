"""
Pre-deployment SQLite compatibility check.

Migration 017 (guardrail shadow recording) uses SQLite's JSON functions
json_valid() and json_type() in its CHECK constraints, built in since SQLite
3.38. Migrations run automatically at API startup, and a failed migration
stops startup, so run this with the SAME Python that will run the API before
deploying a release that includes migration 017:

    ./venv/bin/python scripts/check_sqlite_compat.py

It opens only an in-memory database -- never db/ayurcore.db or any other
file -- and changes nothing. Exit code 0 means compatible; 1 means not, with
the reasons printed.
"""
import sqlite3
import sys

MINIMUM_SQLITE = (3, 38, 0)


def problems(sqlite_version: str = sqlite3.sqlite_version, connect=sqlite3.connect) -> list[str]:
    """Everything that would stop migration 017 from applying or working on
    this runtime; an empty list means compatible."""
    found = []
    try:
        version = tuple(int(part) for part in sqlite_version.split("."))
    except ValueError:
        return [f"unrecognised SQLite version {sqlite_version!r}"]
    if version < MINIMUM_SQLITE:
        found.append(f"SQLite {sqlite_version} is older than the required "
                     f"{'.'.join(map(str, MINIMUM_SQLITE))} (JSON functions built in)")
    conn = connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (x TEXT CHECK (x IS NULL OR CASE WHEN json_valid(x) "
                     "THEN json_type(x) = 'array' ELSE 0 END))")
        conn.execute("INSERT INTO t VALUES ('[1]')")
        try:
            conn.execute("INSERT INTO t VALUES ('{}')")
            found.append("a CHECK using json_valid/json_type did not reject a JSON object")
        except sqlite3.IntegrityError:
            pass
    except sqlite3.DatabaseError as e:
        found.append(f"SQLite JSON functions are not usable: {e}")
    finally:
        conn.close()
    return found


def main() -> int:
    found = problems()
    if found:
        print(f"INCOMPATIBLE -- Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}:")
        for problem in found:
            print(f"  - {problem}")
        return 1
    print(f"OK -- Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version} "
          f"(>= {'.'.join(map(str, MINIMUM_SQLITE))}, JSON functions usable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
