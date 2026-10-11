"""
A6.2.1 -- Streamlit telemetry disabled (privacy hardening, Sep 2026).

Verifies ONLY that .streamlit/config.toml exists at the project root and
explicitly sets [browser] gatherUsageStats = false -- the config Streamlit
itself reads to disable its default anonymous usage-telemetry phone-home
(see the A6.2 privacy review finding this closes: no .streamlit/config.toml
existed anywhere in this repo, so Streamlit's default -- gatherUsageStats
true -- was silently in effect).

This test does not touch, invoke, or depend on backend authentication, the
PII tokenizer, Gemini, prescriptions, previews, the audit system, the
database schema, or voice/Sarvam -- it only reads one static config file's
contents. Nothing here makes an HTTP call, opens a database connection, or
imports backend/ui code at all.

Pure file I/O and string parsing -- no FastAPI, no argon2-cffi, no
Streamlit runtime needed to run this test, so (unlike most of this
project's test suite) it runs identically in a sandbox with no PyPI access
and on the real Mac venv.

Parses with tomllib (stdlib, Python 3.11+) when available, since that is
the actual mechanism Streamlit itself uses to read this file -- config
correctness should be verified the same way Streamlit parses it, not by a
hand-rolled approximation. Falls back to a conservative line-based check
only if tomllib is unavailable (Python < 3.11), strictly for portability,
never as the primary verification method.

Run directly (no pytest required):
    python3 tests/test_streamlit_config.py
"""
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, ".streamlit", "config.toml")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Raises AssertionError immediately on a failed assertion, so a
    failure is a REAL failure under both `python tests/test_X.py` and
    `pytest tests/` -- same convention every other test file in this
    project uses (see e.g. tests/test_migrations.py)."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def test_streamlit_config_file_exists():
    print("\nTest: .streamlit/config.toml exists at the project root")
    check(
        f"{CONFIG_PATH} exists",
        os.path.isfile(CONFIG_PATH),
        CONFIG_PATH,
    )


def test_streamlit_config_disables_usage_telemetry():
    print("\nTest: [browser] gatherUsageStats = false is set, parsed the same way Streamlit itself reads it")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw_text = f.read()

    try:
        import tomllib
        parsed = tomllib.loads(raw_text)
        browser_section = parsed.get("browser", {})
        check(
            "config.toml has a [browser] section",
            "browser" in parsed,
            list(parsed.keys()),
        )
        check(
            "[browser].gatherUsageStats is present",
            "gatherUsageStats" in browser_section,
            browser_section,
        )
        check(
            '[browser].gatherUsageStats is boolean false (not the string "false", not true, not missing)',
            browser_section.get("gatherUsageStats") is False,
            browser_section.get("gatherUsageStats"),
        )
    except ModuleNotFoundError:
        # Fallback for Python < 3.11 (no stdlib tomllib): conservative
        # line-based check on the raw text. Only exercised if the real
        # TOML parser isn't available -- see module docstring.
        print("    (tomllib unavailable in this interpreter -- falling back to line-based parsing)")
        lines = [line.strip() for line in raw_text.splitlines()]
        check("[browser] section header is present", "[browser]" in lines, lines)
        browser_index = lines.index("[browser]") if "[browser]" in lines else -1
        section_lines = lines[browser_index + 1:] if browser_index >= 0 else []
        end = next((i for i, l in enumerate(section_lines) if l.startswith("[")), len(section_lines))
        section_lines = section_lines[:end]
        gathers = [l for l in section_lines if l.replace(" ", "").startswith("gatherUsageStats=")]
        check("gatherUsageStats is set inside [browser]", len(gathers) == 1, section_lines)
        check(
            "gatherUsageStats is set to false",
            bool(gathers) and gathers[0].replace(" ", "").endswith("=false"),
            gathers[0] if gathers else None,
        )


def test_streamlit_config_is_scoped_to_only_this_setting():
    print("\nTest: config.toml is scoped to exactly this one privacy setting -- no unrelated server/theme/auth overrides sneaking in")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw_text = f.read()
    section_headers = re.findall(r"^\[([^\]]+)\]", raw_text, re.MULTILINE)
    check(
        "config.toml defines exactly one section, [browser] -- nothing broader was added",
        section_headers == ["browser"],
        section_headers,
    )


_ALL_TESTS = [
    test_streamlit_config_file_exists,
    test_streamlit_config_disables_usage_telemetry,
    test_streamlit_config_is_scoped_to_only_this_setting,
]


def run_tests():
    print("--- Starting A6.2.1 Streamlit telemetry config verification ---\n")
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")
        print()
    print(f"--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    return _function_failures


if __name__ == "__main__":
    import sys
    _failures = run_tests()
    if _failures:
        sys.exit(1)
