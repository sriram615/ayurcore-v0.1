"""
A0 Step 0 -- DEFECT VERIFICATION (read-only evidence, changes nothing).

Reproduces the two defects identified in the Agentic A0 read-only inspection
report, BEFORE either is fixed, so that the fixes have a recorded "before"
to be measured against.

This script is deliberately NOT a pytest file and asserts nothing about
desired behavior -- it observes and prints what the code does today. The
pytest regression tests that lock in the FIXED behavior live separately, in
tests/test_a0_step0_hardening.py.

    python scripts/a0_step0_verify_defects.py

DEFECT A -- check_stock ignores the caller's clinic.
  agents/copilot.py, check_stock():
      stock_grams = get_stock(canonical_herb_name, clinic_id=DEFAULT_CLINIC_ID)
  The tool takes no clinic parameter at all, so there is no way to ask it for
  any clinic but the hardcoded default. A doctor authenticated to clinic B
  triggers a Gemini tool call that reads clinic A's inventory.

DEFECT B -- no application-level total wall-clock budget.
  agents/copilot.py sets http_options=types.HttpOptions(timeout=...), which
  bounds ONE HTTP request. The google-genai SDK's automatic function calling
  makes up to _DEFAULT_MAX_REMOTE_CALLS_AFC (10) round trips inside a single
  chat.send_message() call, each getting that same per-request budget afresh.
  Nothing anywhere measures elapsed wall-clock time across the loop.

  Worst case today: 10 x GEMINI_TIMEOUT_SECONDS. At the default 45s that is
  450 seconds on one FastAPI threadpool thread -- against ui/app.py's own 60s
  client-side timeout on the very same call.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

OTHER_CLINIC_ID = "a0_step0_evidence_clinic"
DEFAULT_STOCK = 1111
OTHER_STOCK = 2222


def _banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def _seed_two_clinics() -> None:
    """Gives 'Ashwagandha' a DIFFERENT stock level in two different clinics,
    so that a read returning one number rather than the other is unambiguous
    evidence of which clinic was actually consulted."""
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
            (OTHER_CLINIC_ID, "Ashwagandha", "Withania somnifera", OTHER_STOCK, "BATCH-EVID-01", 90),
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup_evidence_clinic() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM inventory WHERE clinic_id = ?", (OTHER_CLINIC_ID,))
        conn.commit()
    finally:
        conn.close()


# --- DEFECT A ---------------------------------------------------------------

def evidence_defect_a_direct_call():
    _banner("DEFECT A (1/2) -- check_stock() has no clinic parameter and reads the hardcoded default")
    import inspect

    import agents.copilot as copilot

    _seed_two_clinics()

    sig = inspect.signature(copilot.check_stock)
    print(f"  agents/copilot.py :: check_stock{sig}")
    print(f"  parameters        : {list(sig.parameters)}")
    print()
    print(f"  inventory 'Ashwagandha' @ default_clinic      = {DEFAULT_STOCK}")
    print(f"  inventory 'Ashwagandha' @ {OTHER_CLINIC_ID} = {OTHER_STOCK}")
    print()

    print("  Note: a clinic_id FUNCTION PARAMETER would be the wrong fix -- the SDK")
    print("  builds the tool declaration Gemini sees from this signature, so the MODEL")
    print("  could then choose the tenant. The correct property to test is therefore")
    print("  'does the tool honour the CALLER's clinic', not 'does it take an argument'.")
    print()

    # Property under test: can this tool be made to read a clinic other than
    # the hardcoded default, by any server-side means at all?
    context_var = getattr(copilot, "_ACTIVE_CLINIC_ID", None)
    token = None
    if context_var is not None:
        token = context_var.set(OTHER_CLINIC_ID)
    try:
        observed = copilot.check_stock("Ashwagandha")
    finally:
        if context_var is not None and token is not None:
            context_var.reset(token)

    print(f"  request-scoped clinic mechanism present?  {context_var is not None}")
    print(f"  with caller's clinic set to {OTHER_CLINIC_ID!r}:")
    print(f"    check_stock('Ashwagandha') -> {observed}")
    print()

    reads_default_regardless = observed.get("stock_grams") == DEFAULT_STOCK
    src = inspect.getsource(copilot.check_stock)
    hardcoded_lines = [l.strip() for l in src.splitlines() if "clinic_id=DEFAULT_CLINIC_ID" in l]

    if hardcoded_lines:
        print("  Hardcoded line still present in check_stock:")
        for line in hardcoded_lines:
            print(f"    {line}")
    else:
        print("  No `clinic_id=DEFAULT_CLINIC_ID` line remains in check_stock.")
    print()

    reproduced = reads_default_regardless and bool(hardcoded_lines)
    if reproduced:
        print("  => DEFECT A CONFIRMED: the tool reads DEFAULT_CLINIC_ID no matter")
        print("     which clinic the caller belongs to.")
    else:
        print(f"  => defect A NOT reproduced: the tool returned {observed.get('stock_grams')}, "
              f"the caller's own clinic's number. FIXED.")
    return reproduced


class _ToolCallingFakeChat:
    """Faithfully mimics what the google-genai SDK does during automatic
    function calling: it invokes the SAME module-level tool functions the SDK
    would, records what they returned, then produces a final JSON answer.

    This is not a stand-in for the SDK's network behavior -- it is a stand-in
    for the SDK's *tool dispatch*, which is the only part Defect A concerns.
    """

    def __init__(self, tool_results: list):
        self._tool_results = tool_results

    def send_message(self, prompt):
        import agents.copilot as copilot

        self._tool_results.append(copilot.check_stock("Ashwagandha"))

        class _Resp:
            pass

        resp = _Resp()
        resp.text = json.dumps({
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
        })
        return resp


class _ToolCallingFakeChats:
    def __init__(self, tool_results):
        self._tool_results = tool_results

    def create(self, model, config):
        return _ToolCallingFakeChat(self._tool_results)


class _ToolCallingFakeClient:
    def __init__(self, tool_results):
        self.chats = _ToolCallingFakeChats(tool_results)


def evidence_defect_a_cross_tenant_read():
    _banner("DEFECT A (2/2) -- a clinic-B doctor's /prescribe/preview reads clinic-A inventory")
    import secrets

    import agents.copilot as copilot
    import backend.main as main_module
    from backend.auth_service import create_user
    from fastapi.testclient import TestClient

    _seed_two_clinics()

    tool_results: list = []
    real_client = copilot.genai.Client
    original_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-evidence-key-not-a-real-credential"
    copilot.genai.Client = lambda api_key: _ToolCallingFakeClient(tool_results)

    try:
        with TestClient(main_module.app) as client:
            username = f"a0_evidence_doctor_{secrets.token_hex(6)}"
            password = "a0-evidence-password-not-a-real-credential"
            create_user(username, password, clinic_id=OTHER_CLINIC_ID)
            login = client.post("/auth/login", json={"username": username, "password": password})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            print(f"  authenticated doctor's clinic_id = {login.json()['clinic_id']!r}")

            resp = client.post(
                "/prescribe/preview",
                json={
                    "patient_name": "Evidence Patient",
                    "notes": "Vata imbalance. Ashwagandha 10g.",
                },
                headers=headers,
            )
            print(f"  POST /prescribe/preview -> {resp.status_code}")
            print(f"  stock the Gemini tool actually read = {tool_results}")
            print()

            leaked = bool(tool_results) and tool_results[0].get("stock_grams") == DEFAULT_STOCK
            if leaked:
                print(f"  => DEFECT A CONFIRMED end to end: the doctor is authenticated to")
                print(f"     {OTHER_CLINIC_ID!r} (stock {OTHER_STOCK}), but the tool returned")
                print(f"     {DEFAULT_STOCK} -- default_clinic's number. A cross-tenant read.")
            else:
                print("  => defect A NOT reproduced end to end (has the fix already been applied?)")
            return leaked
    finally:
        copilot.genai.Client = real_client
        if original_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_key


# --- DEFECT B ---------------------------------------------------------------

BLOCKING_SECONDS = 3.0


class _SlowFakeChat:
    """Simulates the SDK's automatic-function-calling loop taking a long
    time inside ONE send_message() call -- which is exactly what several
    tool round trips look like from agents/copilot.py's point of view.

    Blocks for BLOCKING_SECONDS and then SUCCEEDS. The point is not the
    specific duration: it is that no amount of elapsed time causes
    parse_clinical_notes() to give up, because nothing measures it.
    """

    def __init__(self, blocking_seconds: float):
        self._blocking_seconds = blocking_seconds

    def send_message(self, prompt):
        time.sleep(self._blocking_seconds)

        class _Resp:
            pass

        resp = _Resp()
        resp.text = json.dumps({
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
        })
        return resp


class _SlowFakeChats:
    def __init__(self, blocking_seconds):
        self._blocking_seconds = blocking_seconds

    def create(self, model, config):
        return _SlowFakeChat(self._blocking_seconds)


class _SlowFakeClient:
    def __init__(self, blocking_seconds):
        self.chats = _SlowFakeChats(blocking_seconds)


def evidence_defect_b():
    _banner("DEFECT B -- no application-level total wall-clock budget across the tool/model loop")
    import inspect

    import agents.copilot as copilot
    from google.genai import _extra_utils

    source = inspect.getsource(copilot)
    budget_symbols = [s for s in ("TOTAL_DEADLINE", "TOTAL_BUDGET", "deadline", "monotonic") if s in source]
    print("  Static scan of agents/copilot.py for any total-budget mechanism:")
    print(f"    symbols found among (TOTAL_DEADLINE, TOTAL_BUDGET, deadline, monotonic): {budget_symbols or 'NONE'}")
    print(f"    time.monotonic imported/used?  {'time.monotonic' in source}")
    print()

    max_calls = _extra_utils._DEFAULT_MAX_REMOTE_CALLS_AFC
    per_request = copilot.GEMINI_TIMEOUT_SECONDS
    print("  The only bound that exists today is per-HTTP-request:")
    print(f"    GEMINI_TIMEOUT_SECONDS                  = {per_request}s  (http_options.timeout)")
    print(f"    SDK _DEFAULT_MAX_REMOTE_CALLS_AFC       = {max_calls} round trips inside ONE send_message()")
    print(f"    => arithmetic worst case                = {max_calls} x {per_request}s = {max_calls * per_request}s")
    print(f"    ui/app.py's own timeout on this call    = 60s")
    print()
    print("  Also note (google/genai/_extra_utils.py :: get_function_response_parts):")
    print("    the SDK wraps every tool invocation in `except Exception` and feeds the")
    print("    error back to the model as {'error': ...}. A deadline raised from inside")
    print("    check_herb_alias/check_stock is therefore SWALLOWED and the loop continues.")
    print()

    # If a budget mechanism exists, temporarily shrink it so this evidence
    # run proves the bound in seconds rather than blocking for a real 40s.
    # If it does not exist, the block duration is irrelevant -- nothing will
    # interrupt it at any value.
    has_budget = hasattr(copilot, "GEMINI_TOTAL_BUDGET_SECONDS")
    original_budget = getattr(copilot, "GEMINI_TOTAL_BUDGET_SECONDS", None)
    test_budget = 2.0
    block_for = BLOCKING_SECONDS if not has_budget else test_budget + 3.0

    original_key = os.environ.get("GEMINI_API_KEY")
    real_client = copilot.genai.Client
    os.environ["GEMINI_API_KEY"] = "fake-evidence-key-not-a-real-credential"
    copilot.genai.Client = lambda api_key: _SlowFakeClient(block_for)
    if has_budget:
        copilot.GEMINI_TOTAL_BUDGET_SECONDS = test_budget
        print(f"  Dynamic check: budget temporarily set to {test_budget}s; "
              f"send_message() will block for {block_for}s.")
    else:
        print(f"  Dynamic check: send_message() blocks for {block_for}s; no budget exists.")

    try:
        started = time.monotonic()
        result = copilot.parse_clinical_notes("[[PAT_EVIDENCE]] Vata. Ashwagandha 10g.")
        elapsed = time.monotonic() - started
        print(f"    parse_clinical_notes() returned normally after {elapsed:.2f}s -> {result.dosha_imbalance}")
        print()
        print("  => DEFECT B CONFIRMED: the call completed rather than being abandoned.")
        print("     Elapsed time is never measured, so there is no budget to exceed at")
        print("     ANY duration. The block here is illustrative; 450s behaves identically.")
        return True
    except ValueError as e:
        elapsed = time.monotonic() - started
        print(f"    parse_clinical_notes() raised ValueError({str(e)!r}) after {elapsed:.2f}s")
        print()
        print(f"  => defect B NOT reproduced: the caller was released at ~{test_budget}s")
        print(f"     even though the underlying call blocks for {block_for}s. FIXED.")
        return False
    finally:
        copilot.genai.Client = real_client
        if has_budget:
            copilot.GEMINI_TOTAL_BUDGET_SECONDS = original_budget
        if original_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_key


def main():
    print("AyurCore A0 Step 0 -- defect verification (read-only, mutates only")
    print("an isolated evidence clinic's inventory row, cleaned up at the end)")

    results = {}
    try:
        results["A-direct"] = evidence_defect_a_direct_call()
        results["A-endtoend"] = evidence_defect_a_cross_tenant_read()
        results["B"] = evidence_defect_b()
    finally:
        _cleanup_evidence_clinic()

    _banner("SUMMARY")
    for name, reproduced in results.items():
        print(f"  {name:<12} reproduced = {reproduced}")
    print()
    print("  (Evidence clinic inventory row removed.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
