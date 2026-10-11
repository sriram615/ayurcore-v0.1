"""
A0 no-write-imports -- the static check named in the A0 architecture
report's section 9 as the one that matters most: "the only mechanism here
that fails loudly when someone later adds a convenient helper that happens
to open a writable connection."

Two independent checks, both by AST inspection (no import-and-execute --
this file must be able to catch a bad import even if importing it would
itself trip something):

  1. DIRECT IMPORT ALLOW-LIST. Every import statement literally written in
     a file under agents/a0/ must resolve to something on a short,
     explicit allow-list (stdlib, the google-genai SDK, pydantic, or one
     of exactly three first-party read-only modules: backend.schemas,
     backend.herb_resolver, backend.inventory_read). Anything else is a
     failure, whether or not it happens to be one of the named forbidden
     functions below -- this is deliberately stricter than the named list,
     so a new but merely UNLISTED helper import still fails loudly rather
     than sailing through because nobody thought to add its name.

  2. TRANSITIVE FORBIDDEN-NAME CHECK. Starting from agents/a0/*.py, follow
     every LOCAL (agents./backend./db.) import to its source file and walk
     the whole reachable set, asserting that none of the report's named
     write functions -- deduct_and_prescribe, create_preview,
     mark_confirmed, mark_stale_previews_expired, create_audit_event,
     log_correction, reserve_idempotent_operation,
     complete_idempotent_reservation, create_session, revoke_session,
     create_user -- is ever imported anywhere in that closure, even by a
     module agents/a0/ imports indirectly. This is the "even transitively"
     guarantee: check 1 alone would already block a NEW import from
     widening the surface, but this check also guards against an existing
     allowed module (backend.herb_resolver, backend.inventory_read) later
     growing an import of one of these without anyone touching
     agents/a0/'s own files at all.

get_connection() is deliberately NOT in the transitive forbidden-name set:
it is a connection factory, not a write operation, and
backend/inventory_read.py -- the one module in A0's reachable set that
uses it -- is READ-ONLY by design (see that file's own docstring). Rather
than ban the name everywhere (which would make a legitimate bulk stock
read impossible to write at all), test_the_one_connection_using_module_
issues_only_selects below checks the actual property that matters: the
one file that touches get_connection() issues only SELECT.

Run directly (no pytest required):
    python tests/test_a0_no_write_imports.py
"""
import ast
import os
import pathlib
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
A0_DIR = REPO_ROOT / "agents" / "a0"

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


# Modules agents/a0/*.py may import directly. Stdlib/SDK entries are
# matched by prefix (so "google.genai.types" matches "google.genai" and
# "google"); first-party entries are exact matches only -- deliberately no
# prefix match on "backend" as a whole, so a new "backend.<anything>"
# import has to be added here explicitly, one module at a time.
ALLOWED_EXTERNAL_PREFIXES = {
    "google", "pydantic", "dataclasses", "json", "logging", "os", "sys",
    "time", "typing", "__future__", "inspect",
}
ALLOWED_FIRST_PARTY_MODULES = {
    "backend.schemas",
    "backend.herb_resolver",
    "backend.inventory_read",
}

# The report's named write operations (section 4's "Forbidden write tools
# -- never exposed, directly or transitively" list, plus the write-side
# auth_service functions section 8 names). Checked as bare imported names,
# regardless of which module they came from.
FORBIDDEN_NAMES = {
    "deduct_and_prescribe",
    "create_preview",
    "mark_confirmed",
    "mark_stale_previews_expired",
    "create_audit_event",
    "log_correction",
    "reserve_idempotent_operation",
    "complete_idempotent_reservation",
    "create_session",
    "revoke_session",
    "create_user",
}

# Modules that must never be imported by anything in A0's reachable set,
# by name (not just their individual write functions) -- these exist
# specifically to hold write operations or session/credential state, so
# banning the module is simpler and strictly stronger than banning only
# the one function name a report happened to spell out.
FORBIDDEN_MODULES = {
    "backend.main",
    "backend.auth_service",
    "backend.preview_store",
    "backend.audit_store",
    "backend.corrections_service",
    "backend.idempotency_store",
    "backend.inventory_service",
}


def _iter_a0_files():
    return sorted(A0_DIR.glob("*.py"))


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_of(tree: ast.Module):
    """Yields (module_dotted_path, imported_name_or_None) for every import
    statement in a module. For `import a.b.c`, yields ("a.b.c", None). For
    `from a.b import c, d`, yields ("a.b", "c") and ("a.b", "d")."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                yield module, alias.name


def _module_file(module: str) -> pathlib.Path | None:
    """Resolves a first-party dotted module name to its source file under
    the repo, or None if it isn't one of ours (stdlib/SDK)."""
    if not (module == "agents" or module.startswith(("agents.", "backend.", "backend", "db."))
            or module == "backend" or module == "db"):
        return None
    candidate = REPO_ROOT / module.replace(".", "/")
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").is_file():
        return candidate / "__init__.py"
    return None


def test_direct_imports_stay_on_the_allow_list():
    """Every import literally written inside agents/a0/*.py must resolve
    to the stdlib/SDK or to one of exactly three first-party, read-only
    modules. This is checked BEFORE the named-forbidden-function check
    below, and is deliberately stricter: it fails on any unlisted import,
    not just a known-dangerous one."""
    violations = []
    for path in _iter_a0_files():
        tree = _parse(path)
        for module, name in _imports_of(tree):
            if module.startswith("agents.a0") or module == "agents":
                continue  # a0's own submodules importing each other is fine
            top = module.split(".")[0]
            if top in ALLOWED_EXTERNAL_PREFIXES:
                continue
            if module in ALLOWED_FIRST_PARTY_MODULES:
                continue
            violations.append(f"{path.relative_to(REPO_ROOT)}: imports '{module}'" + (f".{name}" if name else ""))
    check("every direct import under agents/a0/ is on the allow-list",
          not violations, "; ".join(violations))


def test_forbidden_modules_are_never_imported_directly():
    """The report's explicit list -- backend.main, backend.auth_service,
    and the write-holding service modules -- must not appear as a direct
    import anywhere under agents/a0/."""
    violations = []
    for path in _iter_a0_files():
        tree = _parse(path)
        for module, _name in _imports_of(tree):
            if module in FORBIDDEN_MODULES:
                violations.append(f"{path.relative_to(REPO_ROOT)}: imports forbidden module '{module}'")
    check("no forbidden module is imported directly by agents/a0/",
          not violations, "; ".join(violations))


def test_forbidden_write_functions_are_never_reachable_even_transitively():
    """Walks the whole first-party import closure reachable from
    agents/a0/*.py (agents/a0's own files, plus every local module they
    import, recursively) and asserts none of the report's named write
    functions is ever imported anywhere in it -- not just directly inside
    agents/a0/."""
    seen_files: set[pathlib.Path] = set()
    to_visit = list(_iter_a0_files())
    violations = []

    while to_visit:
        path = to_visit.pop()
        if path in seen_files:
            continue
        seen_files.add(path)
        tree = _parse(path)
        for module, name in _imports_of(tree):
            if name in FORBIDDEN_NAMES:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}: imports forbidden name "
                    f"'{name}' from '{module}'"
                )
            resolved = _module_file(module)
            if resolved is not None and resolved not in seen_files:
                to_visit.append(resolved)

    check("no forbidden write function is importable anywhere in A0's reachable module closure",
          not violations, "; ".join(violations))
    check("the closure walk actually visited more than just agents/a0/ itself "
          "(otherwise this test proves nothing)",
          len(seen_files) > len(list(_iter_a0_files())),
          f"only visited: {sorted(str(p.relative_to(REPO_ROOT)) for p in seen_files)}")


def test_the_one_connection_using_module_issues_only_selects():
    """backend/inventory_read.py is the one module in A0's reachable set
    that imports db.database.get_connection. Rather than banning that name
    outright (which would make the context-engineering contract's bulk
    stock read impossible to implement at all), this checks the actual
    property the report's own enforcement rule cares about: 'a tool is a
    write tool if it can reach get_connection() with anything other than a
    SELECT.'"""
    path = REPO_ROOT / "backend" / "inventory_read.py"
    check("backend/inventory_read.py exists", path.is_file())
    source = path.read_text(encoding="utf-8").upper()
    for forbidden_sql in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER "):
        check(f"backend/inventory_read.py issues no {forbidden_sql.strip()} statement",
              forbidden_sql not in source)
    check("backend/inventory_read.py issues at least one SELECT",
          "SELECT" in source)


def test_no_agents_a0_file_imports_get_connection_directly():
    """Narrower than the transitive check above, and matches the report's
    literal wording: nothing written inside agents/a0/ itself imports
    get_connection -- the one module that legitimately does
    (backend/inventory_read.py) lives outside agents/a0/, one hop away,
    where it is checked on its own terms above."""
    violations = []
    for path in _iter_a0_files():
        tree = _parse(path)
        for _module, name in _imports_of(tree):
            if name == "get_connection":
                violations.append(f"{path.relative_to(REPO_ROOT)}: imports get_connection directly")
    check("no file under agents/a0/ imports get_connection directly",
          not violations, "; ".join(violations))


_ALL_TESTS = [
    test_direct_imports_stay_on_the_allow_list,
    test_forbidden_modules_are_never_imported_directly,
    test_forbidden_write_functions_are_never_reachable_even_transitively,
    test_the_one_connection_using_module_issues_only_selects,
    test_no_agents_a0_file_imports_get_connection_directly,
]


def run_tests():
    print("--- A0 no-write-imports: static import-surface verification ---\n")
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
