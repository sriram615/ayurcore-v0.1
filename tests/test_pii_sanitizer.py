"""
Unit tests for backend/pii_sanitizer.py.

No server, no network, no pytest required -- run directly:
    python tests/test_pii_sanitizer.py

Mirrors the print-PASS/FAIL style of tests/test_golden.py so both suites
can be run the same way.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.pii_sanitizer import (
    tokenize_payload,
    assert_no_pii_leak,
    rehydrate_payload,
    find_unresolved_tokens,
    expand_name_variants,
    PIIViolationError,
)

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


def test_basic_tokenization_removes_name():
    print("\nTest: basic tokenization removes the patient name")
    notes = "John Mathew has Vata imbalance. John requires 20g Ashwagandha."
    sanitized, mapping = tokenize_payload(notes, ["John Mathew"])

    check("real name absent from sanitized text", "John Mathew" not in sanitized)
    check("exactly one token minted for one PII value", len(mapping) == 1, f"mapping={mapping}")
    token = next(iter(mapping))
    check("token format matches [[PAT_xxxxxxxx]]", token.startswith("[[PAT_") and token.endswith("]]"))
    check("token contains no fragment of the real name", "john" not in token.lower() and "mathew" not in token.lower())
    check(
        "the bare first-name mention ('John requires...') is untouched when only the full name was supplied",
        "John requires" in sanitized,
    )


def test_name_variants_collapse_to_one_token():
    print("\nTest: expand_name_variants makes a bare first-name mention collapse to the SAME token as the full name")
    notes = "John Mathew has Vata imbalance. John requires 20g Ashwagandha."
    pii_values = expand_name_variants("John Mathew")
    check("expand_name_variants includes the full name and both parts", set(pii_values) == {"John Mathew", "John", "Mathew"}, pii_values)

    sanitized, mapping = tokenize_payload(notes, pii_values)
    check("real full name gone", "John Mathew" not in sanitized)
    check("bare 'John' mention also gone", "John requires" not in sanitized and "John" not in sanitized)
    check("only ONE token was minted (not three)", len(mapping) == 1, f"mapping={mapping}")
    token = next(iter(mapping))
    check("that one token appears at both mention sites", sanitized.count(token) == 2, sanitized)
    check("the mapping restores the FULL name, not just 'John'", mapping[token] == "John Mathew", mapping)


def test_case_insensitive_and_repeated_mentions():
    print("\nTest: case-insensitive matching, same token for every mention")
    notes = "Patient: Rahul Menon. rahul menon came today. Mr. RAHUL MENON needs care."
    sanitized, mapping = tokenize_payload(notes, ["Rahul Menon"])
    check("no case variant of the name survives", "rahul" not in sanitized.lower())
    check("only one token was minted for the one distinct value", len(mapping) == 1)
    token = next(iter(mapping))
    check("the single token appears 3 times (once per mention)", sanitized.count(token) == 3, sanitized)


def test_longest_first_prevents_partial_collision():
    print("\nTest: longest-value-first ordering avoids partial-name corruption")
    notes = "Mrs. Priya Menon and her relative Menon Nair both visited."
    sanitized, mapping = tokenize_payload(notes, ["Priya Menon", "Menon Nair"])
    check("neither full name survives", "Priya Menon" not in sanitized and "Menon Nair" not in sanitized)
    check("two distinct tokens were minted", len(mapping) == 2, f"mapping={mapping}")


def test_empty_and_none_values_are_ignored():
    print("\nTest: empty/None PII values are ignored, not tokenized as blanks")
    notes = "Some clinical notes with no PII in them."
    sanitized, mapping = tokenize_payload(notes, [None, "", "   "])
    check("text is untouched", sanitized == notes)
    check("no tokens minted", len(mapping) == 0)


def test_multiple_pii_types_name_phone_id():
    print("\nTest: name + phone + patient ID all get tokenized (not just name)")
    notes = "Patient Anjali Pillai, phone 9876543210, MRN AYC-1029, reports fatigue."
    sanitized, mapping = tokenize_payload(notes, ["Anjali Pillai", "9876543210", "AYC-1029"])
    check("name gone", "Anjali Pillai" not in sanitized)
    check("phone gone", "9876543210" not in sanitized)
    check("patient ID gone", "AYC-1029" not in sanitized)
    check("three tokens minted", len(mapping) == 3, f"mapping={mapping}")


def test_assert_no_pii_leak_passes_on_clean_text():
    print("\nTest: assert_no_pii_leak passes silently on clean sanitized text")
    notes = "Patient [[PAT_ABCDEF01]] needs 20g Ashwagandha."
    try:
        assert_no_pii_leak(notes, ["John Mathew"])
        check("no exception raised for clean text", True)
    except PIIViolationError as e:
        check("no exception raised for clean text", False, str(e))


def test_assert_no_pii_leak_catches_real_leak():
    print("\nTest: assert_no_pii_leak raises when PII is still present (THE key security test)")
    leaked_notes = "Patient John Mathew needs 20g Ashwagandha."
    raised = False
    try:
        assert_no_pii_leak(leaked_notes, ["John Mathew"])
    except PIIViolationError:
        raised = True
    check("PIIViolationError raised on a real leak", raised)


def test_assert_no_pii_leak_catches_partial_leak():
    print("\nTest: assert_no_pii_leak catches a leak of just ONE of several PII values")
    notes = "Patient [[PAT_ABCDEF01]] phone 9876543210 needs care."  # phone not tokenized!
    raised = False
    try:
        assert_no_pii_leak(notes, ["John Mathew", "9876543210"])
    except PIIViolationError:
        raised = True
    check("PIIViolationError raised even though only the phone leaked", raised)


def test_rehydrate_restores_real_identity():
    print("\nTest: rehydrate_payload restores the real value from a token")
    _, mapping = tokenize_payload("John Mathew has fatigue.", ["John Mathew"])
    token = next(iter(mapping))
    ai_json = {
        "patient_name": token,
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}],
    }
    restored = rehydrate_payload(ai_json, mapping)
    check("patient_name restored to the real name", restored["patient_name"] == "John Mathew", restored)
    check("unrelated fields untouched", restored["dosha_imbalance"] == "Vata" and restored["prescribed_herbs"][0]["name"] == "Ashwagandha")


def test_rehydrate_is_recursive_and_case_insensitive():
    print("\nTest: rehydrate_payload works through nested lists/dicts and tolerates token case changes")
    _, mapping = tokenize_payload("Priya Menon needs care.", ["Priya Menon"])
    token = next(iter(mapping))
    ai_json = {
        "notes_summary": [f"Regarding {token.lower()}: stable.", {"nested": token}],
    }
    restored = rehydrate_payload(ai_json, mapping)
    check(
        "token restored inside a nested list/dict even with different casing",
        "Priya Menon" in restored["notes_summary"][0] and restored["notes_summary"][1]["nested"] == "Priya Menon",
        restored,
    )


def test_find_unresolved_tokens_flags_unmapped_token():
    print("\nTest: find_unresolved_tokens flags a token the mapping doesn't know about")
    fabricated = {"patient_name": "[[PAT_DEADBEEF]]", "dosha_imbalance": "Vata"}
    leftovers = find_unresolved_tokens(rehydrate_payload(fabricated, {}))
    check("unmapped token detected", leftovers == ["[[PAT_DEADBEEF]]"], leftovers)


def test_network_boundary_style_assertion():
    print("\nTest: end-to-end -- what would actually leave the server contains no PII")
    real_name, real_phone = "John Mathew", "9876543210"
    original_notes = "Patient John Mathew (phone 9876543210) needs 20g Ashwagandha for Vata imbalance."
    outgoing_payload, mapping = tokenize_payload(original_notes, [real_name, real_phone])
    assert_no_pii_leak(outgoing_payload, [real_name, real_phone])  # would raise & abort if this failed
    check("real name NOT in outgoing payload", real_name not in outgoing_payload)
    check("real phone NOT in outgoing payload", real_phone not in outgoing_payload)

    # Simulate the AI's structured response, then rehydrate + persist.
    ai_response = {"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}]}
    final_result = rehydrate_payload(ai_response, mapping)
    final_result["patient_name"] = real_name  # supplied locally, never by the AI
    check("final persisted record has the real patient name", final_result["patient_name"] == real_name)


_ALL_TESTS = [
    test_basic_tokenization_removes_name,
    test_name_variants_collapse_to_one_token,
    test_case_insensitive_and_repeated_mentions,
    test_longest_first_prevents_partial_collision,
    test_empty_and_none_values_are_ignored,
    test_multiple_pii_types_name_phone_id,
    test_assert_no_pii_leak_passes_on_clean_text,
    test_assert_no_pii_leak_catches_real_leak,
    test_assert_no_pii_leak_catches_partial_leak,
    test_rehydrate_restores_real_identity,
    test_rehydrate_is_recursive_and_case_insensitive,
    test_find_unresolved_tokens_flags_unmapped_token,
    test_network_boundary_style_assertion,
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
    print("--- PII Air-Gap Golden Tests (backend/pii_sanitizer.py) ---")
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
