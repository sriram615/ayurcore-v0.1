#!/usr/bin/env python3
"""
Run the backend tests in a scratch sandbox that structurally cannot contain restricted evaluation material.

Why not scripts/run_isolated_tests.py: that runner copies every file Git knows about, untracked ones included, and the
acceptance file of the longitudinal benchmark is untracked, so the runner would copy it (and one test hashes it). This
script is the procedure documented in documentation/docs/PORTFOLIO_READINESS_ROADMAP.md (decision D-04); it does NOT modify the runner.

    python scripts/run_scratch_sandbox_tests.py --dest <new dir outside the checkout> --python <venv python> --dry-run
    python scripts/run_scratch_sandbox_tests.py --dest <new dir outside the checkout> --python <venv python> [--collect-only]

What it does, in order:
  1. Lists the TRACKED files only (`git ls-files --cached`): untracked files (the acceptance file, held datasets, helper
     scripts, anything not yet reviewed) are never candidates. The names are read; no file is opened for this.
  2. Applies the deny rules below to the NAMES (restricted prefixes, any path naming the acceptance file, databases, keys,
     .env files, generated state). Any other path containing "accept" aborts the run so a person looks at it.
  3. Refuses to continue if a tracked file differs from HEAD (the sandbox must be the committed tree) unless --allow-dirty.
  4. Copies the allowed files to <dest>/checkout, writes a MANIFEST with the SHA-256 of every copy, and proves the sandbox
     holds no restricted name, database, secret file or generated state.
  5. Creates the sandbox database INSIDE the sandbox (path asserted), seeds it, writes the marker tests/conftest.py needs.
  6. Runs pytest on an explicit list of test files under `env -i` with scratch HOME/TMPDIR and no API keys. Test files in
     EXCLUDED_TESTS are never run; each has a recorded reason.
Nothing is written inside the checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Never copied, whatever else is true.
RESTRICTED_PREFIXES = ("eval/benchmarks/v2_longitudinal/", "eval/benchmarks/v2_para2/")
RESTRICTED_NAME = "longitudinal_v1_accept"
SKIP_PREFIXES = ("frontend/", "apps/", ".streamlit/", "logs/")
FORBIDDEN_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".enc", ".pem", ".key", ".p12", ".pfx")
GENERATED_PARTS = ("__pycache__", ".pytest_cache", "node_modules", ".next", "test-results")

# Test files that are NOT run, with the reason (kept in sync with the roadmap, section B.10).
EXCLUDED_TESTS = {
    "tests/test_s1_ood_v2.py": "hashes s1_test.json (the reserved final AI-1 holdout)",
    "tests/test_s1_benchmark.py": "passes s1_test.json to compare(); behaviour unverified, so excluded",
    "tests/test_streamlit_config.py": "legacy UI test: needs streamlit (installing it needs the network)",
    "tests/test_ui_auth_integration.py": "legacy UI test: needs streamlit",
    "tests/test_ui_ux2.py": "legacy UI test: needs streamlit",
    "tests/test_ui_ux3.py": "legacy UI test: needs streamlit",
    "tests/test_ui_error_translation.py": "legacy UI test: imports ui/app.py, which imports streamlit",
    "tests/test_extractor_t1.py": "needs numpy (pinned in documentation/constraints.txt; not installed in the scratch venv, installing needs the network)",
    "tests/test_extractor_t2.py": "needs numpy (see test_extractor_t1.py)",
    "tests/test_extractor_t3.py": "needs numpy (see test_extractor_t1.py)",
}


def deny_reason(path: str) -> str | None:
    """Why this tracked path must not be copied, or None. Pure function of the NAME."""
    low = path.lower()
    if RESTRICTED_NAME in low:
        return "names the acceptance file"
    if any(path.startswith(p) for p in RESTRICTED_PREFIXES):
        return "restricted benchmark directory"
    if any(path.startswith(p) for p in SKIP_PREFIXES):
        return "not needed by the backend tests / generated"
    if low.endswith(FORBIDDEN_SUFFIXES):
        return "database, key or encrypted file"
    if os.path.basename(low).startswith(".env"):
        return "environment file"
    if any(part in GENERATED_PARTS for part in path.split("/")):
        return "generated state"
    return None


def plan_copy(tracked: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """(paths to copy, [(path, reason) denied]). Aborts (ValueError) on an unreviewed path containing 'accept'."""
    keep, denied = [], []
    for p in tracked:
        reason = deny_reason(p)
        if reason:
            denied.append((p, reason))
        elif "accept" in p.lower():
            raise ValueError(f"unreviewed path containing 'accept' (stop and review): {p}")
        else:
            keep.append(p)
    return keep, denied


def test_run_list(copied: list[str]) -> list[str]:
    tests = sorted(p for p in copied if p.startswith("tests/test_") and p.endswith(".py"))
    return [t for t in tests if t not in EXCLUDED_TESTS]


def git_lines(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout
    return [x for x in out.decode("utf-8").split("\0") if x]


def tracked_dirty() -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"], cwd=ROOT, capture_output=True, check=True,
                         env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout.decode()
    return [e[3:] for e in out.split("\0") if e]


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="a NEW directory outside the checkout")
    ap.add_argument("--python", required=True, help="the venv python used to run pytest")
    ap.add_argument("--dry-run", action="store_true", help="print the plan (names only), copy nothing")
    ap.add_argument("--collect-only", action="store_true", help="import the selected test modules and list the tests; run none")
    ap.add_argument("--allow-dirty", action="store_true", help="copy working-tree contents of modified tracked files")
    ap.add_argument("--extra", action="append", default=[], help="an UNTRACKED file (repo-relative path) to copy as well; it must pass the deny rules; repeatable")
    ap.add_argument("--prepare-only", action="store_true", help="build and prove the sandbox, create its database, run no tests")
    ap.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    a = ap.parse_args(argv)

    dest = pathlib.Path(a.dest).resolve()
    if dest == ROOT or ROOT in dest.parents:
        raise SystemExit(f"refusing: {dest} is inside the checkout {ROOT}")
    tracked = git_lines("ls-files", "-z", "--cached")
    for extra in a.extra:
        if extra in tracked or not (ROOT / extra).is_file():
            raise SystemExit(f"--extra {extra}: must be an existing untracked file")
    keep, denied = plan_copy(tracked + list(a.extra))
    tests = test_run_list(keep)
    excluded_present = sorted(t for t in EXCLUDED_TESTS if t in keep)
    print(f"tracked files: {len(keep) + len(denied)}; to copy: {len(keep)}; denied by name: {len(denied)}")
    print(f"test files to run: {len(tests)}; excluded: {len(excluded_present)}")
    for t in excluded_present:
        print(f"  excluded {t}: {EXCLUDED_TESTS[t]}")
    dirty = tracked_dirty()
    if dirty and not a.allow_dirty:
        print("tracked files differ from HEAD (the sandbox must be the committed tree):", dirty[:10])
        return 3
    if a.dry_run:
        for p, r in denied:
            print(f"  denied {p}: {r}")
        return 0

    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"refusing: {dest} exists and is not empty")
    checkout = dest / "checkout"
    for rel in keep:
        target = checkout / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    manifest = {rel: sha256(checkout / rel) for rel in keep}
    (dest / "MANIFEST.json").write_text(json.dumps({"copied": len(keep), "denied": denied, "excluded_tests": EXCLUDED_TESTS, "files": manifest}, indent=1))

    # ---- exclusion proof (names and content-free checks) ----
    problems = []
    for p in checkout.rglob("*"):
        rel = p.relative_to(checkout).as_posix()
        if p.is_file() and (deny_reason(rel) or RESTRICTED_NAME in rel.lower()):
            problems.append(rel)
        if any(part in GENERATED_PARTS for part in rel.split("/")):
            problems.append(rel)
    if problems:
        raise SystemExit("exclusion proof failed: " + ", ".join(sorted(set(problems))[:10]))
    d = checkout
    while d != d.parent:
        if (d / ".env").exists():
            raise SystemExit(f"a .env exists in {d}")
        d = d.parent
    print(f"exclusion proof OK: {len(keep)} files, none restricted, no database/secret/generated state, no .env in any parent")

    env = {"PATH": "/usr/bin:/bin", "HOME": str(dest / "home"), "TMPDIR": str(dest / "tmp"), "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "en_US.UTF-8"}
    (dest / "home").mkdir(exist_ok=True)
    (dest / "tmp").mkdir(exist_ok=True)
    check = ("import os,sys; from db.database import DB_PATH, init_db; p=os.path.realpath(DB_PATH); "
             f"sys.exit('DB outside sandbox: '+p) if not p.startswith({str(checkout.resolve()) + os.sep!r}) else None; init_db(); print('sandbox database:', p)")
    subprocess.run([a.python, "-c", check], cwd=checkout, env=env, check=True)
    subprocess.run([a.python, "db/seed.py"], cwd=checkout, env=env, check=True)
    (checkout / "db" / ".isolated_test_sandbox").write_text(str(checkout.resolve()) + "\n")

    if a.prepare_only:
        print(f"sandbox ready: {checkout}")
        return 0
    cmd = [a.python, "-m", "pytest", *tests, "-v", "-p", "no:cacheprovider", f"--junitxml={dest / 'pytest.xml'}"]
    if a.collect_only:
        cmd.append("--collect-only")
    cmd += a.pytest_args
    print("running:", " ".join(cmd[:4]), f"... ({len(tests)} files)")
    return subprocess.run(cmd, cwd=checkout, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
