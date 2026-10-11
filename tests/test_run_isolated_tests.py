"""
scripts/run_isolated_tests.py -- the isolated test runner's own safety logic.

Coverage:
  - argument parsing: pytest options may come first (`-k rbac`, `-q`, `-x`),
    paths and options mix in any order and keep their order, `--keep` is
    recognised anywhere, `--` passes everything after it verbatim, `-h`
    exits before any sandbox exists;
  - child environment: secrets and import-path overrides (PYTHONPATH,
    PYTHONHOME) are removed, and a child process started with it cannot
    import from a directory the parent's PYTHONPATH named;
  - temporary-directory validation: a temp location that is (or resolves,
    via a symlink, to) the checkout or somewhere inside it is refused
    before anything is created; main() refuses without creating a sandbox,
    directory or database.

  - serial execution: no parallel-test plugin installed or requested, one
    pytest process per invocation.

The runner is imported, never executed recursively; filesystem checks use a
throwaway fake project root. No database is opened.

Run directly (no pytest required):
    python tests/test_run_isolated_tests.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail=""):
    """Raises AssertionError immediately on a failed assertion -- same
    convention as every other test file in this project."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail != "" else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _runner():
    spec = importlib.util.spec_from_file_location("run_isolated_tests", os.path.join(ROOT, "scripts", "run_isolated_tests.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raises_system_exit(fn):
    try:
        fn()
    except SystemExit as e:
        return e
    return None


def test_pytest_options_may_come_first_and_mix_with_paths():
    r = _runner()
    cases = [
        (["-k", "rbac"], (False, ["-k", "rbac"])),
        (["-q"], (False, ["-q"])),
        (["-x"], (False, ["-x"])),
        (["-k", "rbac", "-x", "-q"], (False, ["-k", "rbac", "-x", "-q"])),
        (["tests/test_rbac.py", "-q"], (False, ["tests/test_rbac.py", "-q"])),
        (["-x", "tests/test_rbac.py", "-k", "role", "-p", "no:cacheprovider"],
         (False, ["-x", "tests/test_rbac.py", "-k", "role", "-p", "no:cacheprovider"])),
        (["--keep", "-q", "tests/"], (True, ["-q", "tests/"])),
        (["-q", "tests/", "--keep"], (True, ["-q", "tests/"])),
        (["-k", "rbac", "--", "--keep", "-h"], (False, ["-k", "rbac", "--keep", "-h"])),
        (["--", "-x"], (False, ["-x"])),
    ]
    for argv, expected in cases:
        check(f"parse_args({argv}) -> {expected}", r.parse_args(argv) == expected, r.parse_args(argv))
    keep, args = r.parse_args([])
    check("no arguments -> the complete suite", (keep, args) == (False, r.DEFAULT_PYTEST_ARGS))
    args.append("mutated")
    check("the default list is a copy (callers can't mutate the module default)", "mutated" not in r.DEFAULT_PYTEST_ARGS)
    for flag in ("-h", "--help"):
        e = _raises_system_exit(lambda: r.parse_args([flag]))
        check(f"{flag} exits with code 0 before any sandbox is created", e is not None and e.code == 0)


def test_child_environment_drops_secrets_and_import_overrides():
    r = _runner()
    parent = {"PATH": "/usr/bin", "HOME": "/home/x", "PYTHONPATH": "/somewhere/checkout", "PYTHONHOME": "/somewhere",
              "GEMINI_API_KEY": "synthetic-not-a-real-key", "SARVAM_API_KEY": "synthetic", "AYURCORE_BACKUP_PASSPHRASE": "synthetic",
              "AYURCORE_AUDIT_ANCHOR_KEY": "synthetic"}
    snapshot = dict(parent)
    env = r.child_env(parent)
    check("PYTHONPATH and PYTHONHOME removed", "PYTHONPATH" not in env and "PYTHONHOME" not in env)
    check("secret variables removed", not any(k in env for k in r.SECRET_ENV_VARS))
    check("ordinary variables kept", env.get("PATH") == "/usr/bin" and env.get("HOME") == "/home/x")
    check("bytecode writing disabled in the sandbox", env.get("PYTHONDONTWRITEBYTECODE") == "1")
    check("the parent environment is not mutated", parent == snapshot)


def test_child_process_cannot_import_from_the_original_checkout():
    """A parent PYTHONPATH naming another checkout must not reach a child
    started with child_env(): that checkout's modules are not importable."""
    r = _runner()
    work = tempfile.mkdtemp(prefix="runner_env_test_")
    try:
        original = os.path.join(work, "original_checkout")
        sandbox = os.path.join(work, "sandbox")
        os.makedirs(os.path.join(original, "only_in_original"))
        with open(os.path.join(original, "only_in_original", "__init__.py"), "w") as f:
            f.write("WHERE = 'original checkout'\n")
        os.makedirs(sandbox)
        probe = "import importlib.util as u; print(u.find_spec('only_in_original') is not None)"
        polluted = dict(os.environ, PYTHONPATH=original)
        leaked = subprocess.run([sys.executable, "-c", probe], cwd=sandbox, env=polluted, capture_output=True, text=True).stdout.strip()
        clean = subprocess.run([sys.executable, "-c", probe], cwd=sandbox, env=r.child_env(polluted), capture_output=True, text=True).stdout.strip()
        check("without sanitisation the original checkout IS importable (the test is meaningful)", leaked == "True", leaked)
        check("with child_env() the original checkout is NOT importable", clean == "False", clean)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_temp_dir_inside_the_checkout_is_refused_before_creation():
    r = _runner()
    work = tempfile.mkdtemp(prefix="runner_tmp_test_")
    try:
        fake_root = os.path.join(work, "checkout")
        inside = os.path.join(fake_root, "docs")
        outside = os.path.join(work, "elsewhere")
        link_into_root = os.path.join(work, "link_to_docs")
        for d in (inside, outside):
            os.makedirs(d)
        os.symlink(inside, link_into_root)
        before = sorted(os.listdir(fake_root)), sorted(os.listdir(inside))

        for label, base in (("the checkout itself", fake_root), ("a directory inside it", inside),
                            ("a symlink resolving inside it", link_into_root)):
            e = _raises_system_exit(lambda b=base: r.sandbox_parent(fake_root, b))
            check(f"temp dir = {label} -> refused", e is not None and "inside the project checkout" in str(e.code), e and e.code)
        check("outside the checkout -> accepted (resolved path)", r.sandbox_parent(fake_root, outside) == os.path.realpath(outside))
        check("nothing was created inside the checkout", (sorted(os.listdir(fake_root)), sorted(os.listdir(inside))) == before)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_main_refuses_without_creating_anything_when_tmp_is_inside_the_checkout():
    """main() validates the temp location before mkdtemp: with the temp dir
    pointed inside the checkout it exits and leaves no sandbox, directory or
    database behind. (Under the runner, ROOT is itself a sandbox copy.)"""
    r = _runner()
    target = os.path.join(r.ROOT, "docs")
    before = sorted(os.listdir(target))
    saved = tempfile.tempdir
    tempfile.tempdir = target
    try:
        e = _raises_system_exit(lambda: r.main(["-q"]))
    finally:
        tempfile.tempdir = saved
    check("main() refuses", e is not None and "inside the project checkout" in str(e.code), e and e.code)
    after = sorted(os.listdir(target))
    check("no sandbox or directory created in the checkout", after == before, sorted(set(after) - set(before)))
    check("no database file created there", not any(f.endswith((".db", ".db-wal", ".db-shm")) for f in after))



def test_tests_run_serially_in_one_process():
    """Tests that patch shared module state (tests/test_shadow_preview.py
    replaces recorder, engine and sys.modules entries inside try/finally)
    rely on pytest running serially, in one process per runner invocation,
    in that invocation's own sandbox copy. Pinned here so a parallel plugin
    or option cannot slip in unnoticed."""
    import importlib.util
    r = _runner()
    check("no parallel-test plugin is installed",
          all(importlib.util.find_spec(name) is None for name in ("xdist", "pytest_xdist", "pytest_parallel")))
    check("the default arguments request no parallelism",
          not any(a in ("-n", "--numprocesses", "--dist") or a.startswith(("-n", "--numprocesses=", "--dist="))
                  for a in r.DEFAULT_PYTEST_ARGS), r.DEFAULT_PYTEST_ARGS)
    source = open(os.path.join(ROOT, "scripts", "run_isolated_tests.py"), encoding="utf-8").read()
    check("the runner starts exactly one pytest process", source.count('"-m", "pytest"') == 1)

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
