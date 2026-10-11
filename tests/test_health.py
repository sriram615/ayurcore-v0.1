"""
GET /health -- liveness endpoint.

Verifies it returns 200 with a fixed, minimal body, needs no
authentication, discloses nothing sensitive, touches neither the database
nor Gemini, and rejects other methods with 405.

Run directly (no pytest required):
    python tests/test_health.py
"""
import os
import sqlite3
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")


def _client():
    from fastapi.testclient import TestClient
    import backend.main as main_module
    return TestClient(main_module.app)


def _counts():
    conn = sqlite3.connect(DB_PATH)
    try:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("inventory", "prescriptions", "prescription_previews", "audit_events", "sessions"))
    finally:
        conn.close()


def test_health_returns_200_with_fixed_body_and_no_auth():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_response_contains_nothing_sensitive():
    resp = _client().get("/health", headers={"Authorization": "Bearer not-a-real-token"})
    body = resp.text.lower()
    for forbidden in ("gemini", "api_key", "key", "token", "clinic", "patient", "sqlite", "ayurcore.db",
                      "/users/", "traceback", "version", "model", "a0"):
        assert forbidden not in body, forbidden
    real_key = os.environ.get("GEMINI_API_KEY")
    if real_key:
        assert real_key not in resp.text
    assert set(resp.json()) == {"status"}


def test_health_does_not_touch_database_or_gemini():
    import google.genai as genai_real
    import agents.copilot as copilot

    def _boom(*args, **kwargs):
        raise AssertionError("GET /health must never call Gemini")

    original_client, original_parse = genai_real.Client, copilot.parse_clinical_notes
    genai_real.Client, copilot.parse_clinical_notes = _boom, _boom
    try:
        before = _counts()
        assert _client().get("/health").status_code == 200
        assert _counts() == before
    finally:
        genai_real.Client, copilot.parse_clinical_notes = original_client, original_parse


def test_health_rejects_other_methods():
    client = _client()
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/health").status_code == 405, method


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
        print(f"=> PASS: {name}")
    print(f"{len(tests)} passed")
