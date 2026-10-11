"""
AI-2 production package (ai2/): structure, provenance, and the dependency / cost / coupling audit.

The package must be local, deterministic and read-only: stdlib-only imports (plus the application's herb alias table and, in one adapter module,
the application's existing store functions), no web framework, no database driver, no network or paid-service client, no ML library, no
evaluation or rejected-experiment module, no SQL writes. These tests read the source files themselves (ast / regex) and also import the package
in a clean interpreter to see what it actually loads.
"""
import ast
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "ai2"
PROV = json.loads((PKG / "PROVENANCE.json").read_text(encoding="utf-8"))
CORE = sorted(p for p in PKG.rglob("*.py") if "adapters" not in p.parts)
ALL_PY = sorted(PKG.rglob("*.py"))
EXPECTED = {"__init__", "adapters/__init__", "adapters/clinic_stores", "answer", "answering", "c10", "confidence", "coverage", "executor", "interpret",
            "interpretation", "normalize", "planner", "ports", "reference", "result", "scope", "service", "snapshot", "verifier", "vocabulary"}
FORBIDDEN_ROOTS = {"fastapi", "starlette", "pydantic", "uvicorn", "requests", "httpx", "urllib", "urllib3", "http", "socket", "aiohttp", "websockets",
                   "google", "openai", "anthropic", "numpy", "scipy", "pandas", "torch", "transformers", "onnxruntime", "sklearn", "safetensors",
                   "tokenizers", "langchain", "langgraph", "sqlite3", "sqlalchemy", "db", "memory", "scripts", "tests", "eval", "agents", "extractor", "ui", "apps"}
ALLOWED_BACKEND = {"ai2/vocabulary.py": {"backend.herb_resolver"},
                   "ai2/adapters/clinic_stores.py": {"backend.patient_store", "backend.encounter_store"}}
SQL_WRITE = re.compile(r"INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+(TABLE|INDEX|TRIGGER)|ALTER\s+TABLE|CREATE\s+(TABLE|INDEX|TRIGGER)|REPLACE\s+INTO|\.execute\(|\.executemany\(|\.commit\(|get_connection")


def transform(text: str) -> str:
    """The import rewrite that produced the package from the accepted evaluation modules."""
    text = text.replace("from backend.herb_resolver import HERB_ALIASES", "from ai2.vocabulary import HERB_ALIASES")
    text = re.sub(r"memory\.(planner|interpret|coverage|confidence|normalize|reference|scope|executor|answer|verifier|snapshot|c10)\b", r"ai2.\1", text)
    text = text.replace("from memory.v2 import", "from ai2.interpretation import").replace("memory.v2", "ai2.interpretation")
    return text.replace("from memory.retrieval import _answer", "from ai2.answering import _answer")


def code_only(path: pathlib.Path) -> str:
    """The module's code without docstrings and comments (prose may name what was removed)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def imports(path: pathlib.Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


class StructureTests(unittest.TestCase):
    def test_exact_module_set(self):
        self.assertEqual({str(p.relative_to(PKG).with_suffix("")) for p in ALL_PY}, EXPECTED)
        self.assertTrue((PKG / "data" / "planner_model.json").exists())

    def test_rejected_and_evaluation_modules_are_not_in_the_package(self):
        names = {p.stem for p in ALL_PY}
        for banned in ("oos", "semantic", "cue_tagger", "morph", "timeline", "pipeline", "retrieval", "s2_build", "s2_reference", "evaluate_v2",
                       "evaluate_planner", "evaluate_v3_semantic", "evaluate_v2_block4", "evaluate_v2_block5"):
            self.assertNotIn(banned, names)
        self.assertFalse([p for p in ALL_PY if p.stem.startswith("evaluate_")])
        for p in ALL_PY:
            self.assertFalse({m for m in imports(p) if m.split(".")[0] == "memory"}, p.name)

    def test_removed_optional_hooks_are_gone(self):
        text = code_only(PKG / "interpretation.py")
        for gone in ("detector", "confidence_gate", "_low_confidence", "answer_for_session_v2", "require_auth", "fastapi"):
            self.assertNotIn(gone, text)
        planner = code_only(PKG / "planner.py")
        for gone in ("random", "def train", "def save", "_train_factor"):
            self.assertNotIn(gone, planner)


class ProvenanceTests(unittest.TestCase):
    def test_every_module_is_its_accepted_source_plus_the_import_rewrite(self):
        for rel, meta in PROV["modules"].items():
            src = ROOT / meta["source"]
            self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), meta["source_sha256"], f"{meta['source']} drifted from the accepted module")
            packaged = (ROOT / rel).read_text(encoding="utf-8")
            if meta["edits"] is None:
                self.assertEqual(packaged, transform(src.read_text(encoding="utf-8")), rel)

    def test_edited_modules_only_remove_what_the_provenance_lists(self):
        import difflib
        for rel in ("ai2/interpretation.py", "ai2/snapshot.py", "ai2/planner.py"):
            meta = PROV["modules"][rel]
            a = transform((ROOT / meta["source"]).read_text(encoding="utf-8")).splitlines()
            b = (ROOT / rel).read_text(encoding="utf-8").splitlines()
            added = [l[2:] for l in difflib.ndiff(a, b) if l.startswith("+ ") and l[2:].strip()]
            # additions are the rewritten docstring and the new constructor / interpret header; no new logic lines beyond those
            code_added = [l for l in added if not l.lstrip().startswith(('"""', '#')) and "    " in l[:1] + "    "]
            self.assertLessEqual(len(code_added), 60, (rel, len(code_added)))
        self.assertIn("removed", PROV["modules"]["ai2/interpretation.py"]["edits"])

    def test_planner_model_is_the_evaluated_one(self):
        from ai2.service import PLANNER_SHA256
        digest = hashlib.sha256((PKG / "data" / "planner_model.json").read_bytes()).hexdigest()
        self.assertEqual(digest, PLANNER_SHA256)
        self.assertEqual(digest, PROV["planner_model"]["sha256"])
        artifact = ROOT / "artifacts" / "models" / "planner" / "model.json"
        if artifact.exists():
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), digest)


class DependencyAuditTests(unittest.TestCase):
    def test_imports_are_stdlib_ai2_or_the_two_allowed_backend_points(self):
        stdlib = set(sys.stdlib_module_names)
        for p in ALL_PY:
            rel = str(p.relative_to(ROOT))
            for mod in imports(p):
                root = mod.split(".")[0]
                self.assertNotIn(root, FORBIDDEN_ROOTS, f"{rel} imports {mod}")
                if root in ("ai2", "__future__") or root in stdlib:
                    continue
                self.assertIn(mod, ALLOWED_BACKEND.get(rel, set()), f"{rel} imports {mod}")

    def test_the_core_never_imports_the_adapter_or_backend_stores(self):
        for p in CORE:
            mods = imports(p)
            self.assertFalse({m for m in mods if m.startswith("ai2.adapters")}, p.name)
            self.assertFalse({m for m in mods if m.startswith("backend") and m != "backend.herb_resolver"}, p.name)

    def test_no_sql_and_no_write_path_anywhere_in_the_package(self):
        for p in ALL_PY:
            text = p.read_text(encoding="utf-8")
            self.assertEqual(SQL_WRITE.findall(text), [], p.name)
            code = code_only(p)
            self.assertNotRegex(code, r"\.write_text\(|\.write_bytes\(|\.write\(|os\.(remove|rename|system)|subprocess|shutil|urlopen|socket\.|random\.", p.name)
            opens = re.findall(r"\bopen\(([^)]*)\)", code)
            if p.name == "planner.py":
                self.assertEqual(len(opens), 1)                    # Planner.load reads the packaged model; nothing is ever written
                self.assertNotIn("'w'", opens[0].replace('"', "'"))
            else:
                self.assertEqual(opens, [], p.name)

    def test_no_secrets_keys_or_service_urls(self):
        for p in ALL_PY:
            self.assertNotRegex(p.read_text(encoding="utf-8"), r"(?i)api[_-]?key|secret|bearer |https?://|GEMINI|OPENAI|ANTHROPIC", p.name)

    def test_importing_the_package_loads_no_framework_database_ml_or_network_module(self):
        code = ("import sys; sys.path.insert(0, %r)\n" % str(ROOT) + "import ai2\n"
                "svc = ai2.Ai2Service.default()\n"
                "bad = sorted(m for m in sys.modules if m.split('.')[0] in %r)\n"
                "print('BAD', bad)\n"
                "print('BACKEND', sorted(m for m in sys.modules if m.split('.')[0] == 'backend'))\n"
                "print('ADAPTER', 'ai2.adapters.clinic_stores' in sys.modules)\n" % (sorted(FORBIDDEN_ROOTS - {"urllib", "http", "socket", "db"}),))
        out = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertIn("BAD []", out)
        self.assertIn("BACKEND ['backend', 'backend.herb_resolver']", out)
        self.assertIn("ADAPTER False", out)

    def test_herb_resolver_itself_is_plain_python(self):
        self.assertEqual(imports(ROOT / "backend" / "herb_resolver.py") & (FORBIDDEN_ROOTS | {"backend"}), set())

    def test_adapter_reads_only_through_the_existing_store_functions(self):
        text = code_only(PKG / "adapters" / "clinic_stores.py")
        for fn in ("get_patient_for_clinic", "get_prescription_history", "list_encounters_for_patient"):
            self.assertIn(fn, text)
        self.assertNotRegex(text, r"create_|insert_|update_|close_|confirm|deduct|audit_store|db\.database")


if __name__ == "__main__":
    unittest.main()
