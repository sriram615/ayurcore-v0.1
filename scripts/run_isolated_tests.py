"""
Run the backend test suite in an isolated sandbox -- never against the real
database.

Most test files open db/ayurcore.db of the checkout they run from, so the
only safe way to run them is from a COPY of the checkout with its own fresh
database. This script automates the procedure documented in CLAUDE.md:

  1. copies the files Git knows about (tracked + untracked-but-not-ignored)
     into a new temporary directory. Git-ignored files -- *.db, .env, venv/,
     logs/, artifacts/ -- are never copied, and frontend/ is skipped;
  2. creates a fresh database there with every migration (init_db);
  3. seeds it with db/seed.py (which deletes ALL inventory rows -- it only
     ever runs inside the copy);
  4. writes the sandbox marker db/.isolated_test_sandbox, which
     tests/conftest.py requires before pytest will run at all;
  5. runs pytest inside the copy, with secret environment variables and
     import-path overrides (PYTHONPATH, PYTHONHOME) removed, so nothing can
     be imported from the original checkout.

The sandbox is created under the system temporary directory, which is
resolved and checked FIRST: if it lies inside this checkout, the runner
refuses before creating anything.

    ./venv/bin/python scripts/run_isolated_tests.py                  # complete suite
    ./venv/bin/python scripts/run_isolated_tests.py tests/test_rbac.py -q
    ./venv/bin/python scripts/run_isolated_tests.py -k rbac -x       # options may come first
    ./venv/bin/python scripts/run_isolated_tests.py --keep -q tests/ # keep the sandbox

Every argument except the runner's own `--keep` and `-h/--help` is passed
to pytest unchanged, in order. After `--`, everything is passed to pytest
verbatim (e.g. `-- --help` for pytest's own help).

The exit code is pytest's. No test file is modified; tests behave exactly as
in the manual procedure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SANDBOX_MARKER = os.path.join("db", ".isolated_test_sandbox")
SKIP_PREFIXES = ("frontend/",)
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")
SECRET_ENV_VARS = ("GEMINI_API_KEY", "SARVAM_API_KEY", "AYURCORE_BACKUP_PASSPHRASE", "AYURCORE_AUDIT_ANCHOR_KEY")
# Could make the child import modules from outside the sandbox (e.g. the
# original checkout, whose db/database.py points at the real database).
IMPORT_REDIRECT_ENV_VARS = ("PYTHONPATH", "PYTHONHOME")
DEFAULT_PYTEST_ARGS = ["tests/", "-q", "-p", "no:cacheprovider"]


def parse_args(argv: list[str]) -> tuple[bool, list[str]]:
    """Returns (keep, pytest_args). Only `--keep` and `-h/--help` belong to
    the runner; every other argument goes to pytest unchanged and in order,
    whether it is an option (`-k rbac`, `-x`, `-q`) or a path. Everything
    after the first `--` goes to pytest verbatim."""
    keep, pytest_args, passthrough = False, [], False
    for arg in argv:
        if passthrough:
            pytest_args.append(arg)
        elif arg == "--":
            passthrough = True
        elif arg == "--keep":
            keep = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            pytest_args.append(arg)
    return keep, pytest_args or list(DEFAULT_PYTEST_ARGS)


def child_env(environ: dict) -> dict:
    """The environment for every child process: no secrets, no import-path
    overrides, no bytecode written into the sandbox."""
    env = {k: v for k, v in environ.items() if k not in SECRET_ENV_VARS + IMPORT_REDIRECT_ENV_VARS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def sandbox_parent(root: str = ROOT, base: str | None = None) -> str:
    """Resolves the directory the sandbox will be created in (the system
    temporary directory unless `base` is given) and refuses -- before
    anything is created -- if it is the checkout or inside it."""
    root = os.path.realpath(root)
    parent = os.path.realpath(base if base is not None else tempfile.gettempdir())
    if parent == root or parent.startswith(root + os.sep):
        raise SystemExit(f"refusing: the temporary directory {parent} is inside the project checkout {root}; "
                         "set TMPDIR to a directory outside it")
    return parent


def _files_to_copy() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8")
    files = []
    for rel in filter(None, out.split("\0")):
        if rel.startswith(SKIP_PREFIXES) or rel.endswith(FORBIDDEN_SUFFIXES) or os.path.basename(rel).startswith(".env"):
            continue
        if os.path.isfile(os.path.join(ROOT, rel)):
            files.append(rel)
    return files


def _run(cmd: list[str], cwd: str, env: dict) -> None:
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def main(argv: list[str] | None = None) -> int:
    keep, pytest_args = parse_args(sys.argv[1:] if argv is None else argv)
    parent = sandbox_parent()  # validated BEFORE anything is created
    work = tempfile.mkdtemp(prefix="ayurcore_isolated_tests_", dir=parent)
    sandbox = os.path.realpath(os.path.join(work, "checkout"))
    try:
        if sandbox == ROOT or sandbox.startswith(ROOT + os.sep):
            raise SystemExit("refusing: the sandbox must be outside the project checkout")
        for rel in _files_to_copy():
            dest = os.path.join(sandbox, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, rel), dest)
        if any(f.endswith(FORBIDDEN_SUFFIXES) for _, _, fs in os.walk(sandbox) for f in fs):
            raise SystemExit("refusing: a database file was copied into the sandbox")

        env = child_env(dict(os.environ))
        # Fresh database with every migration, created INSIDE the sandbox.
        _run([sys.executable, "-c",
              "import os, sys; from db.database import DB_PATH, init_db; "
              "p = os.path.realpath(DB_PATH); "
              f"sys.exit('refusing: DB_PATH outside the sandbox: ' + p) if not p.startswith({sandbox + os.sep!r}) else None; "
              "init_db(); print('sandbox database:', p)"], sandbox, env)
        # Seed stock -- only ever inside the sandbox (seed.py resolves its path from its own location).
        _run([sys.executable, "db/seed.py"], sandbox, env)
        with open(os.path.join(sandbox, SANDBOX_MARKER), "w", encoding="utf-8") as f:
            f.write(sandbox + "\n")

        print(f"running pytest in sandbox {sandbox}: {' '.join(pytest_args)}", flush=True)
        return subprocess.run([sys.executable, "-m", "pytest", *pytest_args], cwd=sandbox, env=env).returncode
    finally:
        if keep:
            print(f"sandbox kept at {sandbox}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
