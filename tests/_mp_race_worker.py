"""
Worker for the multi-process race test in tests/test_security_hardening.py.

Runs in a SEPARATE OS process (multiprocessing "spawn"), so two workers
exercise SQLite's real cross-process locking against one database file --
the way two uvicorn worker processes would -- rather than threads sharing
one interpreter. Not a test module itself (no test_ prefix).
"""
import time


def warm_up(db_path: str) -> bool:
    """Imports the application once per worker process, so the timed race
    below measures requests, not import time."""
    import db.database as database
    database.DB_PATH = db_path
    import backend.main  # noqa: F401
    return True


def race_request(db_path: str, method: str, path: str, headers: dict, json_body, start_at: float):
    """Waits until the shared wall-clock `start_at`, then performs one
    request against the app pointed at `db_path`. Returns (status, body)."""
    import db.database as database
    database.DB_PATH = db_path
    import backend.main as main_module
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app, raise_server_exceptions=False)  # no startup: schema already migrated
    delay = start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    response = client.request(method, path, headers=headers, json=json_body)
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return response.status_code, body
