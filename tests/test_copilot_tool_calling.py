"""
Unit test for the agentic tool-calling loop's OWN code in agents/copilot.py
-- scoped to what can be verified without a real network call: that
check_herb_alias/check_stock behave correctly against the real database,
that both get registered as tools on the Gemini call, and that the final
JSON response (fenced or not) is parsed correctly.

What this test deliberately does NOT verify: that Google's automatic
function-calling machinery actually invokes check_herb_alias / check_stock
mid-conversation the way the prompt asks it to. That is real,
network-dependent SDK behavior this can't exercise offline -- the first
real run against a live GEMINI_API_KEY should specifically watch (log or
print) whether each herb in a multi-herb note actually triggers a
check_herb_alias call before trusting this in front of the doctor.

Run directly (no pytest required):
    python tests/test_copilot_tool_calling.py
"""
import json
import os
import sys
import sqlite3

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Records AND enforces one assertion -- raises AssertionError
    immediately on failure instead of only printing a FAIL line and
    incrementing a counter, so that `pytest tests/` genuinely fails when a
    check fails, not just this file's own __main__ block. See
    tests/test_rbac.py's module docstring, "PYTEST COMPATIBILITY", for the
    real incident (a cross-clinic information-disclosure bug that was
    invisible to `pytest tests/ -v`) this fixes across every test file in
    this project that still had the old, non-raising version."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _set_stock(herb_name: str, grams: int):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE inventory SET stock_grams = ? WHERE herb_name = ?", (grams, herb_name))
        conn.commit()
    finally:
        conn.close()


def test_tool_functions_are_real_and_read_only():
    print("\nTest: check_herb_alias / check_stock behave correctly against the real DB")
    import agents.copilot as copilot_module

    known = copilot_module.check_herb_alias("ashwagandha root")
    check("check_herb_alias resolves a known alias", known == {"known": True, "canonical_name": "Ashwagandha"}, known)

    unknown = copilot_module.check_herb_alias("unicorn root")
    check("check_herb_alias reports unknown herbs honestly", unknown == {"known": False, "canonical_name": None}, unknown)

    _set_stock("Ashwagandha", 1234)
    stock = copilot_module.check_stock("Ashwagandha")
    check("check_stock reads the real, current stock value", stock == {"herb_name": "Ashwagandha", "stock_grams": 1234}, stock)


class _FakeChat:
    def __init__(self, final_text):
        self._final_text = final_text
        self.sent_messages = []

    def send_message(self, prompt):
        self.sent_messages.append(prompt)

        class _Resp:
            pass

        resp = _Resp()
        resp.text = self._final_text
        return resp


class _FakeChats:
    def __init__(self, final_text, captured_configs):
        self._final_text = final_text
        self._captured_configs = captured_configs

    def create(self, model, config):
        self._captured_configs.append(config)
        return _FakeChat(self._final_text)


class _FakeGenAIClient:
    def __init__(self, final_text, captured_configs):
        self.chats = _FakeChats(final_text, captured_configs)


def _with_fake_client(final_text, captured_configs):
    import agents.copilot as copilot_module

    real_client_cls = copilot_module.genai.Client
    original_api_key = os.environ.get("GEMINI_API_KEY")

    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot_module.genai.Client = lambda api_key: _FakeGenAIClient(final_text, captured_configs)

    def restore():
        copilot_module.genai.Client = real_client_cls
        if original_api_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_api_key

    return restore


def test_final_response_is_parsed_and_tools_are_wired_in():
    print("\nTest: tools are registered on the call, final JSON response parses")
    import agents.copilot as copilot_module

    captured_configs = []
    canned_final_text = json.dumps({
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}],
    })
    restore = _with_fake_client(canned_final_text, captured_configs)
    try:
        result = copilot_module.parse_clinical_notes("[[PAT_ABCDEF01]] needs 20g Ashwagandha for Vata imbalance.")

        check(
            "parse_clinical_notes returns a valid ClinicalParse",
            result.dosha_imbalance == "Vata" and result.prescribed_herbs[0].name == "Ashwagandha",
            result,
        )
        check("exactly one chat session was created", len(captured_configs) == 1, len(captured_configs))
        if captured_configs:
            tool_names = {getattr(t, "__name__", str(t)) for t in captured_configs[0].tools}
            check("both tools were registered on the call", tool_names == {"check_herb_alias", "check_stock"}, tool_names)
    finally:
        restore()


def test_markdown_fenced_response_is_still_parsed():
    print("\nTest: a ```json ... ``` fenced response is still parsed correctly")
    import agents.copilot as copilot_module

    captured_configs = []
    fenced_text = "```json\n" + json.dumps({
        "dosha_imbalance": "Pitta",
        "prescribed_herbs": [{"name": "Brahmi", "grams": 5}],
    }) + "\n```"
    restore = _with_fake_client(fenced_text, captured_configs)
    try:
        result = copilot_module.parse_clinical_notes("[[PAT_ABCDEF02]] needs 5g Brahmi for Pitta imbalance.")
        check(
            "fenced JSON response still parses",
            result.dosha_imbalance == "Pitta" and result.prescribed_herbs[0].name == "Brahmi",
            result,
        )
    finally:
        restore()


_ALL_TESTS = [
    test_tool_functions_are_real_and_read_only,
    test_final_response_is_parsed_and_tools_are_wired_in,
    test_markdown_fenced_response_is_still_parsed,
]


if __name__ == "__main__":
    # check() now raises AssertionError on a failed assertion (see its own
    # docstring, and tests/test_rbac.py's module docstring "PYTEST
    # COMPATIBILITY") -- so each test function is called through this
    # loop, which catches that raise per-function and moves on to the
    # next one, rather than a flat list of bare calls that would abort
    # the whole script at the FIRST failing assertion anywhere. This
    # keeps direct execution's long-standing contract ("run every test
    # function, print a full tally at the end") intact while making every
    # individual check() a real, hard assertion under pytest too.
    print("--- Copilot Tool-Calling Unit Test (own code only -- SDK's real tool-invocation behavior needs a live GEMINI_API_KEY run) ---")
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")

    print(f"\n--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    if _failed:
        sys.exit(1)
