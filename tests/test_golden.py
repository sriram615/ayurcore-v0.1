"""
The Three Golden Tests -- core invariants of the preview/confirm split:
unknown herbs get rejected, insufficient stock rolls back cleanly, and
concurrent confirms on a shared resource never over-deduct.

Rewritten (Sep 2026) to stop talking to a separately-managed live uvicorn
process over `requests`, and to stop depending on GEMINI_API_KEY being
absent from the environment. Both of those were real, repeat sources of
false-alarm failures that had nothing to do with the code under test:

  - The live-server-over-HTTP design already caused one false alarm this
    project (a stale uvicorn process silently squatting on port 8000 gave
    404s that looked like a routing bug). scripts/verify_live_integration.py
    switched to FastAPI's in-process TestClient specifically to avoid this
    class of bug -- this file hadn't been, until now.
  - This file's "Hallucination Test" and "Transaction Rollback Test" rely
    on agents/copilot.py's hardcoded mock parser, which only activates
    when GEMINI_API_KEY is unset. Once a real key exists in .env (as it
    now does, from this project's live-verification work), every
    /prescribe/preview call here silently became a REAL Gemini call
    instead -- with no control over what it returns, and subject to the
    same free-tier rate limit that has caused 502s elsewhere in this
    project. That's what the "Not specified" dosha and the two 502s were:
    not a bug, just this test accidentally going live.

Uses the same in-process TestClient + fake-Gemini-client pattern as
tests/test_pii_airgap.py, test_preview_lifecycle.py, and
test_audit_export.py, so this file is deterministic regardless of what's
in .env, needs no separate server running, and can never hit a real port
or a real API quota again.

Pytest collection fix (Sep 2026): the three golden tests used to live
inside one non-`test_`-prefixed function (`_run_golden_tests`), sharing a
single TestClient/login across all three -- `pytest tests/test_golden.py`
collected zero items because nothing here matched pytest's `test_*`
discovery pattern. Each golden test is now its own top-level `test_`
function, fully self-contained (its own TestClient, its own login, its
own fake-Gemini patch) exactly like every other test_*.py file in this
project (see tests/test_audit_export.py) -- so pytest now collects and
reports each one individually:
    pytest tests/test_golden.py -v
Splitting them out also meant each assertion had to become a real
`assert` (they were previously a print-only "=> FAIL" with no raise, so
a failure was never actually surfaced as a failure to anything -- not
pytest, and not even direct execution's own exit code). That's the same
non-raising-check bug already fixed across the other 9 test_*.py files in
this project; it's fixed the same way here, via plain `assert` rather
than this project's `check()` helper, since this file never used that
helper's pass/fail counters to begin with. No assertion's underlying
condition was changed -- only "print FAIL and keep going" became "assert,
which raises".

Run directly (no pytest required):
    python tests/test_golden.py
"""
import json
import os
import secrets
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'db', 'ayurcore.db')


def _login_new_user(client, clinic_id: str = "default_clinic") -> dict:
    """Phase A, A2: every prescribe endpoint now requires real
    authentication (previously it accepted any/no Authorization header).
    Seeds a fresh real user (real Argon2id hash via create_user -- this
    file only ever runs where FastAPI is installed, and Argon2-cffi is
    expected to be available in that same real environment) and logs in
    through the actual /auth/login endpoint, returning a ready-to-use
    Authorization header for every other call in this file."""
    from backend.auth_service import create_user

    username = f"golden_test_{secrets.token_hex(6)}"
    password = "golden-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def get_stock(herb_name: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT stock_grams FROM inventory WHERE herb_name = ?", (herb_name,))
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def set_stock(herb_name: str, grams: int):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE inventory SET stock_grams = ? WHERE herb_name = ?", (grams, herb_name))
        conn.commit()
    finally:
        conn.close()


class _FakeChat:
    def __init__(self, canned_response: dict):
        self._canned_response = canned_response

    def send_message(self, prompt):
        class _FakeResponse:
            pass
        resp = _FakeResponse()
        resp.text = json.dumps(self._canned_response)
        return resp


class _FakeChats:
    def __init__(self, canned_response: dict):
        self._canned_response = canned_response

    def create(self, model, config):
        return _FakeChat(self._canned_response)


class _FakeGenAIClient:
    def __init__(self, canned_response: dict):
        self.chats = _FakeChats(canned_response)


def _with_fake_gemini(canned_response: dict):
    """Forces parse_clinical_notes() to return exactly `canned_response`,
    regardless of whether a real GEMINI_API_KEY is present in .env --
    this is what makes this file deterministic again."""
    import agents.copilot as copilot_module

    real_client_cls = copilot_module.genai.Client
    original_api_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot_module.genai.Client = lambda api_key: _FakeGenAIClient(canned_response)

    def restore():
        copilot_module.genai.Client = real_client_cls
        if original_api_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_api_key

    return restore


def _preview(client, canned_response: dict, patient_name: str, notes: str, headers: dict):
    restore = _with_fake_gemini(canned_response)
    try:
        return client.post("/prescribe/preview", json={"patient_name": patient_name, "notes": notes}, headers=headers)
    finally:
        restore()


def test_hallucination_unknown_herb_rejected_before_inventory_touched():
    """Test 1: The Hallucination Test -- the AI proposes a herb (Tulsi)
    this clinic does NOT stock, alongside one it does. main.py's own
    normalize_herb() re-validation loop (not the AI) must catch this and
    reject the whole /preview call with 400, before anything touches
    inventory -- regardless of what the AI itself would or wouldn't have
    done with a name it couldn't verify."""
    import backend.main as main_module
    from fastapi.testclient import TestClient

    # Phase A, A1 fix: TestClient only runs FastAPI's @app.on_event("startup")
    # hook (which calls run_migrations()) when used as a context manager.
    # `client = TestClient(app)` on its own never triggers ASGI lifespan, so
    # the schema silently never gets migrated. See db/migrations.py and
    # backend/main.py's startup hook for why this is the ONLY sanctioned
    # place schema changes happen.
    with TestClient(main_module.app) as client:
        # Phase A, A2: a session of its own -- every preview created here
        # is confirmed by the SAME session that created it, so real
        # session-binding enforcement (see backend/main.py's
        # /prescribe/confirm) never gets in the way of what this test is
        # actually about. Cross-session rejection is covered separately
        # and specifically by tests/test_preview_lifecycle.py's
        # test_session_binding_full_flow.
        headers = _login_new_user(client)

        print("Test 1: The Hallucination Test (Unknown LLM Synonym)")
        initial_ashwagandha = get_stock("Ashwagandha")
        canned1 = {"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Tulsi", "grams": 15}, {"name": "Ashwagandha", "grams": 10}]}

        resp1 = _preview(client, canned1, "Golden Test Patient 1", "Patient needs 15g of Tulsi and 10g of Ashwagandha.", headers)
        final_ashwagandha = get_stock("Ashwagandha")

        assert resp1.status_code == 400 and final_ashwagandha == initial_ashwagandha, (
            f"Expected 400 from /preview, got {resp1.status_code}. "
            f"Expected Ashwagandha to remain {initial_ashwagandha}, got {final_ashwagandha}. "
            f"Response: {resp1.text}"
        )
        print("=> PASS: /preview rejected with 400 (UnknownHerbError) and inventory not deducted.")
        print(f"   Reason: {resp1.json().get('detail')}")


def test_transaction_rollback_on_insufficient_stock_deducts_nothing():
    """Test 2: The Transaction Rollback Test -- /preview succeeds (both
    herb names ARE known), but /confirm must fail cleanly on
    insufficient stock, deducting nothing at all -- not even the herb
    that would have had enough."""
    import backend.main as main_module
    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)

        print("Test 2: The Transaction Rollback Test (Partial Failure)")
        initial_ashwagandha = get_stock("Ashwagandha")
        set_stock("Brahmi", 3500)  # reset just in case a prior run left it low
        canned2 = {"dosha_imbalance": "Pitta", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 9000}]}

        resp2_preview = _preview(client, canned2, "Golden Test Patient 2", "Give Patient 5g of Ashwagandha and 9000g of Brahmi.", headers)
        assert resp2_preview.status_code == 200, (
            f"Expected /preview to succeed (200), got {resp2_preview.status_code}: {resp2_preview.text}"
        )

        preview_id = resp2_preview.json()["preview_id"]
        resp2_confirm = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        final_ashwagandha = get_stock("Ashwagandha")

        assert resp2_confirm.status_code == 409 and final_ashwagandha == initial_ashwagandha, (
            f"Expected 409 from /confirm, got {resp2_confirm.status_code}. "
            f"Expected Ashwagandha to remain {initial_ashwagandha}, got {final_ashwagandha}. "
            f"Response: {resp2_confirm.text}"
        )
        print("=> PASS: /confirm rejected with 409 (InsufficientStockError) and Ashwagandha was NOT deducted.")
        print(f"   Reason: {resp2_confirm.json().get('detail')}")


def test_concurrent_confirms_never_over_deduct_shared_stock():
    """Test 3: The Race Condition Test -- two independent previews for
    more Ashwagandha than is left in stock combined, confirmed
    concurrently. Exactly one must succeed, the other must 409, and the
    final stock must reflect exactly one deduction, never a torn or
    double one."""
    import backend.main as main_module
    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)

        print("Test 3: The Race Condition Test (Concurrent Deduction)")
        set_stock("Ashwagandha", 500)
        canned3 = {"dosha_imbalance": "Kapha", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 400}]}

        try:
            # /preview never touches inventory, so getting two independent
            # previews first doesn't need to race -- the atomicity we're
            # actually testing lives entirely in /confirm's DB transaction.
            preview_a = _preview(client, canned3, "Golden Test Patient 3", "Patient needs 400g of Ashwagandha.", headers)
            preview_b = _preview(client, canned3, "Golden Test Patient 3", "Patient needs 400g of Ashwagandha.", headers)

            assert preview_a.status_code == 200 and preview_b.status_code == 200, (
                f"Expected both previews to succeed. Got {preview_a.status_code} and {preview_b.status_code}."
            )

            preview_id_a = preview_a.json()["preview_id"]
            preview_id_b = preview_b.json()["preview_id"]

            def confirm(preview_id):
                return client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)

            with ThreadPoolExecutor(max_workers=2) as executor:
                future1 = executor.submit(confirm, preview_id_a)
                future2 = executor.submit(confirm, preview_id_b)

                resp_a = future1.result()
                resp_b = future2.result()

            final_ashwagandha = get_stock("Ashwagandha")
            statuses = {resp_a.status_code, resp_b.status_code}

            assert statuses == {200, 409} and final_ashwagandha == 100, (
                f"Expected statuses {{200, 409}}, got {resp_a.status_code}, {resp_b.status_code}. "
                f"Expected final stock 100g, got {final_ashwagandha}. "
                f"Responses: A={resp_a.text}, B={resp_b.text}"
            )
            print("=> PASS: Exactly one 200 and one 409 received from /confirm. Final stock is perfectly 100g.")
        finally:
            # Unconditional, regardless of pass/fail -- mirrors the
            # original script's end-of-run cleanup exactly (it ran after
            # all three tests, no matter their outcome) so a failed
            # assertion here never leaves Ashwagandha/Brahmi stock
            # corrupted for the next run of this file, or for any other
            # test file sharing this same on-disk db/ayurcore.db.
            print("Restoring Ashwagandha and Brahmi stock to their seeded defaults for the next run...")
            set_stock("Ashwagandha", 5000)
            set_stock("Brahmi", 3500)


_ALL_TESTS = [
    test_hallucination_unknown_herb_rejected_before_inventory_touched,
    test_transaction_rollback_on_insufficient_stock_deducts_nothing,
    test_concurrent_confirms_never_over_deduct_shared_stock,
]


def run_tests():
    """Direct-execution entry point (python tests/test_golden.py) -- runs
    all three golden tests in sequence through a per-function try/except
    loop, exactly like every other test_*.py file in this project (see
    e.g. tests/test_migrations.py's __main__ block), so one failing test
    doesn't stop the others from running, while still returning a real,
    non-empty failure list (and a non-zero process exit code, below) when
    one of them actually fails -- unlike the old version of this
    function, which caught and printed exceptions but never propagated
    them anywhere. Catches Exception broadly, not just AssertionError, to
    match the original script's robustness (a setup/network error inside
    one golden test must not abort the other two)."""
    print("--- Starting The Three Golden Tests (preview/confirm split) ---\n")
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except Exception as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> FAIL: {_test_fn.__name__} ({type(e).__name__}: {e})")
        print()

    print(f"--- {len(_ALL_TESTS) - len(_function_failures)} passed, {len(_function_failures)} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    return _function_failures


if __name__ == "__main__":
    _failures = run_tests()
    if _failures:
        sys.exit(1)
