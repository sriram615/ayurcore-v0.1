"""
Safety guard: pytest refuses to run outside an isolated test sandbox.

Most test files open db/ayurcore.db of the checkout they run from -- in the
real checkout, that is the development/clinic database. On 2026-09-27 one
such run wrote test data into it (documentation/docs/TECHNICAL_RISK_REGISTER.md, R9).

This guard changes no test. It only stops pytest before any test module is
imported unless this checkout carries the sandbox marker written by
scripts/run_isolated_tests.py, and the marker names this exact checkout.
Run tests with:

    ./venv/bin/python scripts/run_isolated_tests.py [pytest args]

Note: running a test file directly (`python tests/test_x.py`) bypasses
pytest and therefore this guard -- do that only inside a sandbox.
"""
import os

import pytest

_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MARKER = os.path.join(_ROOT, "db", ".isolated_test_sandbox")


def pytest_configure(config):
    try:
        with open(_MARKER, encoding="utf-8") as f:
            marked_root = f.read().strip()
    except OSError:
        marked_root = None
    if marked_root != _ROOT:
        pytest.exit(
            "Refusing to run tests: this checkout is not an isolated test sandbox, so tests "
            "would read and write its db/ayurcore.db. Run them with "
            "`./venv/bin/python scripts/run_isolated_tests.py [pytest args]` instead.",
            returncode=4,
        )


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Reset the in-memory rate limiter so tests do not pollute each other's budgets."""
    try:
        from backend.rate_limit import reset
        reset()
    except ImportError:
        pass
