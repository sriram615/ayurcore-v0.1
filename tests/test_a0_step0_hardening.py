"""
A0 Step 0 hardening -- focused regression tests for the two defect fixes.

Both defects were reproduced against the unmodified code first; see
scripts/a0_step0_verify_defects.py, which is the recorded "before". This file
locks in the "after" so neither can silently regress.

DEFECT A -- check_stock read a hardcoded DEFAULT_CLINIC_ID regardless of who
was asking. A doctor authenticated to clinic B triggered a Gemini tool call
that read clinic A's inventory (reproduced end to end, HTTP 200).

  Fixed by threading the authenticated session's clinic_id from
  backend/main.py through parse_clinical_notes() into a request-scoped
  ContextVar the tool reads.

  Deliberately NOT fixed by adding a clinic_id PARAMETER to check_stock: the
  google-genai SDK builds the tool declaration Gemini sees from the function
  signature, so a parameter would be something the MODEL supplies -- letting
  a prompt injection in the clinical notes pick which tenant's inventory to
  read. Which clinic a caller may see is an authorization fact resolved
  server-side (backend/rbac.py's doctrine), not an argument the AI gets a
  say in. test_model_cannot_choose_the_clinic below pins that.

DEFECT B -- no application-level total wall-clock budget. GEMINI_TIMEOUT_SECONDS
bounds ONE HTTP request; the SDK's automatic function calling makes up to 10
round trips inside a single chat.send_message(). Worst case 10 x 45s = 450s
on one FastAPI threadpool thread, against ui/app.py's 60s timeout.

  Fixed with GEMINI_TOTAL_BUDGET_SECONDS (40s, monotonic), enforced by a
  bounded wait around the blocking send_message() -- not an after-the-fact
  elapsed-time check, which would report an overrun without preventing one.

WHAT THIS FILE DOES NOT CHANGE: tests/test_gemini_timeout.py still pins
http_options.timeout to exactly GEMINI_TIMEOUT_SECONDS * 1000, and that
assertion still passes untouched. The 45s per-request value was deliberately
left alone as a ceiling; the 40s total budget sits underneath it.

Run directly (no pytest required):
    python tests/test_a0_step0_hardening.py
"""
import inspect
import json
import os
import secrets
import sqlite3
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

OTHER_CLINIC_ID = "a0_step0_test_clinic"
DEFAULT_STOCK = 1111
OTHER_STOCK = 2222

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Raises AssertionError immediately on a failed assertion -- same
    convention as every other test file in this project (see
    tests/test_rbac.py's module docstring, "PYTEST COMPATIBILITY")."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _seed_two_clinics():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE inventory SET stock_grams = ? WHERE herb_name = ? AND clinic_id = ?",
            (DEFAULT_STOCK, "Ashwagandha", "default_clinic"),
        )
        conn.execute("DELETE FROM inventory WHERE clinic_id = ?", (OTHER_CLINIC_ID,))
        conn.execute(
            """
            INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (OTHER_CLINIC_ID, "Ashwagandha", "Withania somnifera", OTHER_STOCK, "BATCH-T-01", 90),
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM inventory WHERE clinic_id = ?", (OTHER_CLINIC_ID,))
        conn.commit()
    finally:
        conn.close()


def _count(table: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _get_stock(herb_name: str, clinic_id: str = "default_clinic"):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?",
            (herb_name, clinic_id),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _login_new_user(client, clinic_id: str):
    from backend.auth_service import create_user

    username = f"a0_step0_test_{secrets.token_hex(6)}"
    password = "a0-step0-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# --- Fakes ------------------------------------------------------------------

class _ToolInvokingChat:
    """Mimics the SDK's tool dispatch: really calls the module's tools, then
    answers. Used to prove which clinic the tool actually consulted."""

    def __init__(self, module, tool_results):
        self._module = module
        self._tool_results = tool_results

    def send_message(self, prompt):
        self._tool_results.append(self._module.check_stock("Ashwagandha"))

        class _Resp:
            pass

        resp = _Resp()
        resp.text = json.dumps({
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
        })
        return resp


class _SlowChat:
    """Blocks for `blocking_seconds` inside one send_message(), the way the
    SDK's automatic function-calling loop does across several round trips."""

    def __init__(self, blocking_seconds, started_flag=None):
        self._blocking_seconds = blocking_seconds
        self._started_flag = started_flag

    def send_message(self, prompt):
        if self._started_flag is not None:
            self._started_flag.append(time.monotonic())
        time.sleep(self._blocking_seconds)

        class _Resp:
            pass

        resp = _Resp()
        resp.text = json.dumps({
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
        })
        return resp


def _patch_client(chat_factory):
    """Installs a fake genai.Client whose chats.create() returns
    chat_factory(). Mirrors every other test file's patching technique."""
    import agents.copilot as copilot

    real_client = copilot.genai.Client
    original_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"

    class _Chats:
        def create(self, model, config):
            return chat_factory()

    class _Client:
        def __init__(self, api_key):
            self.chats = _Chats()

    copilot.genai.Client = _Client

    def restore():
        copilot.genai.Client = real_client
        if original_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_key

    return restore


# --- DEFECT A regression tests ---------------------------------------------

def test_check_stock_respects_the_callers_clinic():
    print("\nTest: check_stock reads the CALLER's clinic, not the hardcoded default")
    import agents.copilot as copilot

    _seed_two_clinics()
    try:
        token = copilot._ACTIVE_CLINIC_ID.set(OTHER_CLINIC_ID)
        try:
            observed = copilot.check_stock("Ashwagandha")
        finally:
            copilot._ACTIVE_CLINIC_ID.reset(token)

        check(
            f"with the caller's clinic set to {OTHER_CLINIC_ID!r}, check_stock returns that "
            f"clinic's stock ({OTHER_STOCK}), not default_clinic's ({DEFAULT_STOCK})",
            observed["stock_grams"] == OTHER_STOCK,
            observed,
        )

        default_observed = copilot.check_stock("Ashwagandha")
        check(
            "with no clinic set, check_stock still falls back to DEFAULT_CLINIC_ID "
            "-- pre-existing callers are unaffected",
            default_observed["stock_grams"] == DEFAULT_STOCK,
            default_observed,
        )
    finally:
        _cleanup()


def test_no_hardcoded_default_clinic_on_the_production_path():
    print("\nTest: no hardcoded DEFAULT_CLINIC_ID remains on the tool's production path")
    import agents.copilot as copilot

    src = inspect.getsource(copilot.check_stock)
    offending = [line.strip() for line in src.splitlines() if "clinic_id=DEFAULT_CLINIC_ID" in line]
    check(
        "check_stock no longer passes clinic_id=DEFAULT_CLINIC_ID to get_stock",
        not offending,
        offending,
    )
    check(
        "check_stock reads the request-scoped clinic instead",
        "_ACTIVE_CLINIC_ID.get()" in src,
        src,
    )


def test_model_cannot_choose_the_clinic():
    print("\nTest: the clinic is NOT a model-supplied tool argument")
    import agents.copilot as copilot

    sig = inspect.signature(copilot.check_stock)
    check(
        "check_stock's signature is unchanged (canonical_herb_name only) -- so the tool "
        "declaration Gemini sees carries no clinic argument it could set",
        list(sig.parameters) == ["canonical_herb_name"],
        list(sig.parameters),
    )
    alias_sig = inspect.signature(copilot.check_herb_alias)
    check(
        "check_herb_alias's signature is likewise unchanged",
        list(alias_sig.parameters) == ["raw_name"],
        list(alias_sig.parameters),
    )


def test_preview_threads_the_authenticated_clinic_end_to_end():
    print("\nTest: POST /prescribe/preview scopes the tool to the authenticated doctor's own clinic")
    import agents.copilot as copilot
    import backend.main as main_module
    from fastapi.testclient import TestClient

    _seed_two_clinics()
    tool_results = []
    restore = _patch_client(lambda: _ToolInvokingChat(copilot, tool_results))
    try:
        with TestClient(main_module.app) as client:
            headers = _login_new_user(client, OTHER_CLINIC_ID)
            resp = client.post(
                "/prescribe/preview",
                json={"patient_name": "Clinic Scope Patient", "notes": "Vata. Ashwagandha 10g."},
                headers=headers,
            )
            check("/prescribe/preview succeeded", resp.status_code == 200, resp.text)
            check("the tool was actually invoked", len(tool_results) == 1, tool_results)
            check(
                f"the tool read the doctor's OWN clinic ({OTHER_STOCK}), not default_clinic "
                f"({DEFAULT_STOCK}) -- the cross-tenant read is closed",
                tool_results[0]["stock_grams"] == OTHER_STOCK,
                tool_results[0],
            )
    finally:
        restore()
        _cleanup()


# --- DEFECT B regression tests ---------------------------------------------

def test_total_deadline_is_enforced():
    print("\nTest: the total wall-clock budget genuinely releases the caller")
    import agents.copilot as copilot

    check(
        "GEMINI_TOTAL_BUDGET_SECONDS exists and defaults to 40s",
        copilot.GEMINI_TOTAL_BUDGET_SECONDS == 40.0,
        copilot.GEMINI_TOTAL_BUDGET_SECONDS,
    )

    original_budget = copilot.GEMINI_TOTAL_BUDGET_SECONDS
    copilot.GEMINI_TOTAL_BUDGET_SECONDS = 1.5
    block_for = 6.0
    restore = _patch_client(lambda: _SlowChat(block_for))
    try:
        started = time.monotonic()
        try:
            copilot.parse_clinical_notes("[[PAT_DEADLINE]] Vata. Ashwagandha 10g.")
            check("parse_clinical_notes raised once the budget was spent", False, "no exception")
        except ValueError as e:
            elapsed = time.monotonic() - started
            check(
                "the budget raised ValueError('AI_SERVICE_UNAVAILABLE')",
                str(e) == "AI_SERVICE_UNAVAILABLE",
                str(e),
            )
            check(
                f"the caller was released at ~1.5s even though send_message blocks for {block_for}s "
                f"(actual {elapsed:.2f}s) -- a bound on the blocking call, not an after-the-fact check",
                elapsed < block_for - 1.0,
                f"elapsed={elapsed:.2f}s, block_for={block_for}s",
            )
    finally:
        restore()
        copilot.GEMINI_TOTAL_BUDGET_SECONDS = original_budget


def test_deadline_uses_a_monotonic_clock():
    print("\nTest: the deadline is monotonic, not wall-clock")
    import agents.copilot as copilot

    src = inspect.getsource(copilot)
    check("time.monotonic() is what sets the deadline", "time.monotonic()" in src, "not found")
    check(
        "the deadline helpers exist",
        hasattr(copilot, "_deadline_exceeded") and hasattr(copilot, "_remaining_seconds"),
        dir(copilot),
    )


def test_per_request_ceiling_is_preserved():
    print("\nTest: the existing 45s per-request timeout is preserved as a ceiling")
    import agents.copilot as copilot

    captured = []

    class _CapturingChats:
        def create(self, model, config):
            captured.append(config)
            return _SlowChat(0.0)

    class _CapturingClient:
        def __init__(self, api_key):
            self.chats = _CapturingChats()

    real_client = copilot.genai.Client
    original_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot.genai.Client = _CapturingClient
    try:
        copilot.parse_clinical_notes("[[PAT_CEILING]] Vata. Ashwagandha 10g.")
        check("a config was captured", len(captured) == 1, len(captured))
        check(
            "http_options.timeout is still exactly GEMINI_TIMEOUT_SECONDS * 1000 -- "
            "tests/test_gemini_timeout.py's existing assertion is untouched",
            captured[0].http_options.timeout == int(copilot.GEMINI_TIMEOUT_SECONDS * 1000),
            captured[0].http_options,
        )
        check(
            "the total budget sits UNDER the per-request ceiling",
            copilot.GEMINI_TOTAL_BUDGET_SECONDS < copilot.GEMINI_TIMEOUT_SECONDS,
            (copilot.GEMINI_TOTAL_BUDGET_SECONDS, copilot.GEMINI_TIMEOUT_SECONDS),
        )
    finally:
        copilot.genai.Client = real_client
        if original_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_key


def test_no_database_side_effects_when_the_budget_is_exceeded():
    print("\nTest: a budget-exceeded /prescribe/preview leaves NO preview/inventory/prescription/audit trace")
    import agents.copilot as copilot
    import backend.main as main_module
    from fastapi.testclient import TestClient

    _seed_two_clinics()
    stock_before = _get_stock("Ashwagandha")
    previews_before = _count("prescription_previews")
    prescriptions_before = _count("prescriptions")
    audit_before = _count("audit_events")
    corrections_before = _count("corrections")

    original_budget = copilot.GEMINI_TOTAL_BUDGET_SECONDS
    copilot.GEMINI_TOTAL_BUDGET_SECONDS = 1.0
    restore = _patch_client(lambda: _SlowChat(5.0))
    try:
        with TestClient(main_module.app) as client:
            headers = _login_new_user(client, "default_clinic")
            real_name = "Budget Timeout Patient"
            resp = client.post(
                "/prescribe/preview",
                json={
                    "patient_name": real_name,
                    "patient_phone": "9998887776",
                    "notes": f"Patient {real_name} (phone 9998887776) needs 10g Ashwagandha for Vata.",
                },
                headers=headers,
            )
            check("/prescribe/preview returns 502 when the total budget is exceeded", resp.status_code == 502, resp.text)
            check(
                "the response carries the EXISTING X-Error-Code: AI_SERVICE_UNAVAILABLE contract",
                resp.headers.get("X-Error-Code") == "AI_SERVICE_UNAVAILABLE",
                dict(resp.headers),
            )
            check("the real patient name is absent from the error body", real_name not in resp.text, resp.text)

        check("inventory unchanged", _get_stock("Ashwagandha") == stock_before, (_get_stock("Ashwagandha"), stock_before))
        check("no prescription_previews row created", _count("prescription_previews") == previews_before)
        check("no prescriptions row created", _count("prescriptions") == prescriptions_before)
        check("no audit_events row created", _count("audit_events") == audit_before)
        check("no corrections row created", _count("corrections") == corrections_before)
    finally:
        restore()
        copilot.GEMINI_TOTAL_BUDGET_SECONDS = original_budget
        _cleanup()


def test_abandoned_loop_stops_touching_the_database():
    print("\nTest: once the budget is spent, check_stock performs no further database access")
    import agents.copilot as copilot

    token = copilot._DEADLINE_MONOTONIC.set(time.monotonic() - 1.0)
    try:
        observed = copilot.check_stock("Ashwagandha")
        check(
            "check_stock short-circuits after the deadline instead of querying",
            observed.get("available") is False and observed.get("reason") == "deadline_exceeded",
            observed,
        )
        check("it reports no stock number at all", observed.get("stock_grams") is None, observed)
    finally:
        copilot._DEADLINE_MONOTONIC.reset(token)


# --- Unchanged-behavior regression tests ------------------------------------

def test_offline_mock_behaviour_is_unchanged():
    print("\nTest: the offline mock parser (GEMINI_API_KEY unset) behaves exactly as before")
    import agents.copilot as copilot

    original_key = os.environ.pop("GEMINI_API_KEY", None)
    try:
        result = copilot.parse_clinical_notes("Tulsi and Ashwagandha for this patient")
        check(
            "the Tulsi+Ashwagandha mock branch is unchanged",
            result.dosha_imbalance == "Vata"
            and {h.name: h.grams for h in result.prescribed_herbs} == {"Tulsi": 15, "Ashwagandha": 10},
            result,
        )

        result = copilot.parse_clinical_notes("needs 9000g of Brahmi")
        check(
            "the 9000g-of-Brahmi mock branch is unchanged",
            result.dosha_imbalance == "Pitta"
            and {h.name: h.grams for h in result.prescribed_herbs} == {"Ashwagandha": 5, "Brahmi": 9000},
            result,
        )

        result = copilot.parse_clinical_notes("needs 400g of Ashwagandha")
        check(
            "the 400g-of-Ashwagandha mock branch is unchanged",
            result.dosha_imbalance == "Kapha"
            and {h.name: h.grams for h in result.prescribed_herbs} == {"Ashwagandha": 400},
            result,
        )

        try:
            copilot.parse_clinical_notes("something that matches no mock branch")
            check("an unmatched mock input still raises", False, "no exception")
        except ValueError as e:
            check(
                "an unmatched mock input still raises ValueError('AI_SERVICE_UNAVAILABLE')",
                str(e) == "AI_SERVICE_UNAVAILABLE",
                str(e),
            )
    finally:
        if original_key is not None:
            os.environ["GEMINI_API_KEY"] = original_key


def test_tool_call_semantics_are_unchanged():
    print("\nTest: existing tool-call semantics (registration + return shapes) are unchanged")
    import agents.copilot as copilot

    captured = []

    class _CapturingChats:
        def create(self, model, config):
            captured.append(config)
            return _SlowChat(0.0)

    class _CapturingClient:
        def __init__(self, api_key):
            self.chats = _CapturingChats()

    real_client = copilot.genai.Client
    original_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot.genai.Client = _CapturingClient
    try:
        copilot.parse_clinical_notes("[[PAT_TOOLS]] Vata. Ashwagandha 10g.")
        tool_names = {getattr(t, "__name__", str(t)) for t in captured[0].tools}
        check(
            "both tools are still registered on the call, unchanged",
            tool_names == {"check_herb_alias", "check_stock"},
            tool_names,
        )
    finally:
        copilot.genai.Client = real_client
        if original_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_key

    known = copilot.check_herb_alias("ashwagandha root")
    check(
        "check_herb_alias's known-alias return shape is unchanged",
        known == {"known": True, "canonical_name": "Ashwagandha"},
        known,
    )
    unknown = copilot.check_herb_alias("unicorn root")
    check(
        "check_herb_alias's unknown-herb return shape is unchanged",
        unknown == {"known": False, "canonical_name": None},
        unknown,
    )

    _seed_two_clinics()
    try:
        observed = copilot.check_stock("Ashwagandha")
        check(
            "check_stock's normal (pre-deadline) return shape is unchanged",
            set(observed) == {"herb_name", "stock_grams"} and observed["herb_name"] == "Ashwagandha",
            observed,
        )
    finally:
        _cleanup()


def test_concurrent_requests_do_not_share_clinic_context():
    print("\nTest: two concurrent previews cannot leak each other's clinic (ContextVar isolation)")
    import threading

    import agents.copilot as copilot

    _seed_two_clinics()
    observed = {}

    def _worker(clinic_id, key):
        token = copilot._ACTIVE_CLINIC_ID.set(clinic_id)
        try:
            time.sleep(0.05)  # force overlap
            observed[key] = copilot.check_stock("Ashwagandha")["stock_grams"]
        finally:
            copilot._ACTIVE_CLINIC_ID.reset(token)

    try:
        threads = [
            threading.Thread(target=_worker, args=("default_clinic", "a")),
            threading.Thread(target=_worker, args=(OTHER_CLINIC_ID, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check(
            "the default_clinic thread saw default_clinic's stock",
            observed.get("a") == DEFAULT_STOCK,
            observed,
        )
        check(
            "the other-clinic thread saw its OWN stock, concurrently",
            observed.get("b") == OTHER_STOCK,
            observed,
        )
    finally:
        _cleanup()


_ALL_TESTS = [
    test_check_stock_respects_the_callers_clinic,
    test_no_hardcoded_default_clinic_on_the_production_path,
    test_model_cannot_choose_the_clinic,
    test_preview_threads_the_authenticated_clinic_end_to_end,
    test_total_deadline_is_enforced,
    test_deadline_uses_a_monotonic_clock,
    test_per_request_ceiling_is_preserved,
    test_no_database_side_effects_when_the_budget_is_exceeded,
    test_abandoned_loop_stops_touching_the_database,
    test_offline_mock_behaviour_is_unchanged,
    test_tool_call_semantics_are_unchanged,
    test_concurrent_requests_do_not_share_clinic_context,
]


def run_tests():
    print("--- A0 Step 0: clinic threading + total deadline -- focused verification ---\n")
    failures = []
    for fn in _ALL_TESTS:
        try:
            fn()
        except AssertionError as e:
            failures.append(fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {fn.__name__} ({e})")
        print()
    print(f"--- {_passed} passed, {_failed} failed ---")
    if failures:
        print(f"--- {len(failures)} test function(s) had a failing assertion: {', '.join(failures)} ---")
    return failures


if __name__ == "__main__":
    if run_tests():
        sys.exit(1)
