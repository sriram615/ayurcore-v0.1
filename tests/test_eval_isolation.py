"""
Evaluation isolation: the harness reads only its own checkout's database.

backend/, db/ and agents/ are namespace packages, so when another checkout is
on PYTHONPATH the import order decides whose db/database.py (and therefore
whose db/ayurcore.db) the harness uses. These tests put a FAKE checkout (in a
temporary directory; never the real clinic path) ahead on PYTHONPATH and check
that this checkout still wins, and that run() refuses outright if the
database it would open is anywhere else. No model is called.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db.database as database

HARNESS = ROOT / "scripts" / "clinical_eval_harness.py"


def _load_harness():
    import importlib.util
    spec = importlib.util.spec_from_file_location("clinical_eval_harness_isolation", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HarnessIsolationTests(unittest.TestCase):
    def test_own_checkout_wins_over_another_checkout_on_pythonpath(self):
        with tempfile.TemporaryDirectory() as other:
            os.makedirs(os.path.join(other, "db"))
            with open(os.path.join(other, "db", "database.py"), "w") as f:
                f.write("DB_PATH = '/nonexistent/other_checkout/ayurcore.db'\n")
            probe = textwrap.dedent(f"""
                import importlib.util
                spec = importlib.util.spec_from_file_location("h", {str(HARNESS)!r})
                h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
                import db.database
                print(db.database.__file__)
                h.assert_own_checkout_database()
            """)
            env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "PYTHONHOME")}
            env["PYTHONPATH"] = other
            result = subprocess.run([sys.executable, "-c", probe], cwd=other, env=env,
                                    capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pathlib.Path(result.stdout.strip()).resolve(), (ROOT / "db" / "database.py").resolve())

    def test_run_refuses_a_database_outside_this_checkout(self):
        harness = _load_harness()
        harness.assert_own_checkout_database()  # the normal case passes
        with tempfile.TemporaryDirectory() as other:
            with mock.patch.object(database, "DB_PATH", os.path.join(other, "ayurcore.db")):
                with self.assertRaisesRegex(RuntimeError, "not inside this checkout"):
                    harness.run("control", "selfcheck", output_dir=pathlib.Path(other))
                self.assertEqual(os.listdir(other), [])  # nothing written, no database created


if __name__ == "__main__":
    unittest.main()
