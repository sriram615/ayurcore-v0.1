"""
A6.1 logging pipeline diagnosis (Sep 2026) -- run this directly on the Mac:

    ./venv/bin/python3 scripts/a61_logging_diagnosis.py

Standalone, NOT a pytest file (deliberately outside tests/ naming so pytest
never collects it). Imports backend.main -- the real production app object,
the SAME module every test in tests/test_auth_endpoints.py imports -- and
inspects the EXACT "ayurcore" logger object it uses, then drives one real
wrong-password login through it and reports whether a capture handler
attached directly to that logger receives the resulting record.

Prints only: logger/handler/level diagnostics, and whatever text the
production `event=login_failed` log line itself contains (which, by
design, is only `event=login_failed username=<username>` -- the username
below is a fixed, clearly-fake placeholder, never a real credential).
Never prints a password, a token, an Authorization header, or patient PII.
"""
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

print("=" * 70)
print("STEP 1: bare logging state BEFORE importing backend.main")
print("=" * 70)
print("root logger level (raw):", logging.root.level)
print("root logger effective level:", logging.root.getEffectiveLevel())
pre_ayurcore = logging.getLogger("ayurcore")
print("ayurcore level (raw), before import:", pre_ayurcore.level)
print("ayurcore effective level, before import:", pre_ayurcore.getEffectiveLevel())
print()

print("=" * 70)
print("STEP 2: import backend.main (the real production app)")
print("=" * 70)
import backend.main as main_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.auth_service import create_user  # noqa: E402

ayurcore_logger = logging.getLogger("ayurcore")
print("logger object is the same singleton:", ayurcore_logger is pre_ayurcore)
print("logger.name:", ayurcore_logger.name)
print("logger.level (raw):", ayurcore_logger.level, f"({logging.getLevelName(ayurcore_logger.level)})")
print("logger.getEffectiveLevel():", ayurcore_logger.getEffectiveLevel(), f"({logging.getLevelName(ayurcore_logger.getEffectiveLevel())})")
print("logger.isEnabledFor(INFO):", ayurcore_logger.isEnabledFor(logging.INFO))
print("logger.disabled:", ayurcore_logger.disabled)
print("logger.propagate:", ayurcore_logger.propagate)
print("logger.handlers:", ayurcore_logger.handlers)
print("logger.filters:", ayurcore_logger.filters)
print("root logger level (raw), after import:", logging.root.level, f"({logging.getLevelName(logging.root.level)})")
print("root logger handlers:", logging.root.handlers)
print()

print("=" * 70)
print("STEP 3: verify the exact call site is reachable, via source inspection")
print("=" * 70)
import inspect  # noqa: E402
src = inspect.getsource(main_module)
call_site_present = 'logger.info("event=login_failed username=%s", request.username)' in src
print("main.py contains the exact expected call site:", call_site_present)
print()

print("=" * 70)
print("STEP 4: one real wrong-password login through the real app, with a")
print("capture handler attached DIRECTLY to the 'ayurcore' logger object")
print("=" * 70)
DUMMY_USERNAME = "a61_diag_" + os.urandom(4).hex()
DUMMY_PASSWORD = "diagnosis-only-not-a-real-credential"  # never logged; production never logs passwords

captured = []


class _DiagHandler(logging.Handler):
    def emit(self, record):
        captured.append(self.format(record))


handler = _DiagHandler()
ayurcore_logger.addHandler(handler)
try:
    with TestClient(main_module.app) as client:
        create_user(DUMMY_USERNAME, DUMMY_PASSWORD, clinic_id="default_clinic")
        resp = client.post("/auth/login", json={"username": DUMMY_USERNAME, "password": "definitely-wrong"})
        print("login response status:", resp.status_code, "(expect 401)")
finally:
    ayurcore_logger.removeHandler(handler)

print("number of records captured on 'ayurcore':", len(captured))
print("captured records (log text only -- never contains secrets by design):")
for line in captured:
    print("   ", line)
print()
print("=> contains 'event=login_failed':", any("event=login_failed" in line for line in captured))
print()

print("=" * 70)
print("STEP 5: does the SAME logger already work if forced to INFO?")
print("(non-destructive: reverts the level change before exiting)")
print("=" * 70)
original_level = ayurcore_logger.level
ayurcore_logger.setLevel(logging.INFO)
captured_forced = []
handler2 = _DiagHandler()
handler2.emit = lambda record: captured_forced.append(handler2.format(record))
ayurcore_logger.addHandler(handler2)
try:
    with TestClient(main_module.app) as client:
        resp2 = client.post("/auth/login", json={"username": DUMMY_USERNAME, "password": "definitely-wrong-again"})
        print("second login response status:", resp2.status_code, "(expect 401)")
finally:
    ayurcore_logger.removeHandler(handler2)
    ayurcore_logger.setLevel(original_level)  # revert -- this script must not leave global state changed

print("number of records captured WITH logger.setLevel(logging.INFO) forced:", len(captured_forced))
for line in captured_forced:
    print("   ", line)
print("=> contains 'event=login_failed' once level is forced to INFO:", any("event=login_failed" in line for line in captured_forced))
print()
print("logger level reverted back to:", ayurcore_logger.level, "(should match STEP 2's raw value)")
print()
print("=" * 70)
print("DONE. Paste this entire output back verbatim.")
print("=" * 70)
