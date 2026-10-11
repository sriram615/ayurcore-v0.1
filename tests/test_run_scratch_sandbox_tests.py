"""
The name-based safeguards of scripts/run_scratch_sandbox_tests.py (pure functions; nothing is copied or run).
Restricted material is excluded by NAME before any file is opened, and the plan aborts on an unreviewed path that contains "accept".
"""
import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import run_scratch_sandbox_tests as rs  # noqa: E402


class DenyRulesTests(unittest.TestCase):
    def test_the_acceptance_file_and_restricted_directories_are_denied_by_name(self):
        self.assertIsNotNone(rs.deny_reason("eval/benchmarks/v2_longitudinal/longitudinal_v1_accept.json"))
        self.assertIsNotNone(rs.deny_reason("eval/benchmarks/v2_longitudinal/manifest.json"))
        self.assertIsNotNone(rs.deny_reason("eval/benchmarks/v2_para2/para2_v1.json"))
        self.assertIsNotNone(rs.deny_reason("somewhere/else/Longitudinal_V1_Accept.JSON"))

    def test_databases_keys_env_files_and_generated_state_are_denied(self):
        for p in ("db/ayurcore.db", "x/y.sqlite3", "a/b.pem", "k.key", ".env", "backend/.env.local", "backend/__pycache__/m.pyc", "node_modules/x.js"):
            self.assertIsNotNone(rs.deny_reason(p), p)

    def test_ordinary_source_and_tracked_artifacts_are_allowed(self):
        for p in ("backend/main.py", "ai2/service.py", "tests/test_ai2_api.py", "eval/benchmarks/s3/s3_dev.json", "artifacts/models/t1_tagger/model.json"):
            self.assertIsNone(rs.deny_reason(p), p)


class PlanTests(unittest.TestCase):
    def test_plan_never_returns_a_restricted_path(self):
        tracked = ["backend/main.py", "eval/benchmarks/v2_longitudinal/longitudinal_v1_accept.json", "eval/benchmarks/v2_longitudinal/manifest.json", "db/x.db"]
        keep, denied = rs.plan_copy(tracked)
        self.assertEqual(keep, ["backend/main.py"])
        self.assertEqual(sorted(p for p, _ in denied), sorted(tracked[1:]))

    def test_an_unreviewed_path_containing_accept_aborts_the_plan(self):
        with self.assertRaises(ValueError):
            rs.plan_copy(["backend/main.py", "docs/accepted_terms.md"])

    def test_excluded_tests_are_never_in_the_run_list_and_others_are(self):
        copied = ["tests/test_ai2_api.py", "tests/test_s1_ood_v2.py", "tests/test_ui_ux2.py", "tests/conftest.py", "backend/main.py"]
        self.assertEqual(rs.test_run_list(copied), ["tests/test_ai2_api.py"])

    def test_every_excluded_test_has_a_reason(self):
        self.assertTrue(all(isinstance(v, str) and v for v in rs.EXCLUDED_TESTS.values()))


class DestinationTests(unittest.TestCase):
    def test_a_destination_inside_the_checkout_is_refused(self):
        with self.assertRaises(SystemExit):
            rs.main(["--dest", os.path.join(str(rs.ROOT), "sandbox_here"), "--python", sys.executable, "--dry-run"])


if __name__ == "__main__":
    unittest.main()
