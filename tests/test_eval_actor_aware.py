"""Qualification tests for scripts/eval_actor_aware.py (gate A6 evaluator). Synthetic fakes only; the real-route run is test_real_route_*."""
import json
import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import eval_actor_aware as ev  # noqa: E402

QS = ("q one", "q two")
STATUS = {"unauthenticated": 401, "forbidden": 403, "not_found": 404, "allowed": 200}


def fake(admin_allowed=False, deny_everything=False, never_deny=False, leaky=False, wording_dependent=False, reads_snapshot=False):
    def call(actor, owner, question):
        if never_deny:
            return {"status": 200, "body": "ok", "snapshot_called": True}
        if deny_everything:
            return {"status": 403, "body": {"detail": "x"}, "snapshot_called": False}
        if actor == "no_token":
            s = 401
        elif actor == "auditor_a":
            s = 403
        elif actor == "admin_a":
            s = 200 if admin_allowed else 403
        elif owner in ("B", "MISSING"):
            s = 404
        else:
            s = 200
        if wording_dependent and actor == "auditor_a" and question == "q two":
            s = 200
        body = "ok" if s == 200 else {"detail": "Patient not found."}
        if leaky and owner == "B":
            body = {"detail": "Patient exists in another clinic."}
        return {"status": s, "body": body, "snapshot_called": reads_snapshot and s in (401, 403)}
    return call


class ScoringTests(unittest.TestCase):
    def test_correct_system_passes_under_each_policy(self):
        self.assertTrue(ev.evaluate(fake(admin_allowed=False), QS, "v1_2")["A6_pass"])
        self.assertTrue(ev.evaluate(fake(admin_allowed=True), QS, "v1_1")["A6_pass"])

    def test_doctor_only_route_fails_the_v1_1_admin_control(self):
        r = ev.evaluate(fake(admin_allowed=False), QS, "v1_1")
        self.assertFalse(r["A6_pass"])
        self.assertTrue(any(c == "admin_a_own_patient" for c, _ in r["failures"]))

    def test_deny_everything_fails_the_controls(self):
        self.assertFalse(ev.evaluate(fake(deny_everything=True), QS, "v1_2")["A6_pass"])

    def test_never_deny_fails_the_denials(self):
        self.assertFalse(ev.evaluate(fake(never_deny=True), QS, "v1_2")["A6_pass"])

    def test_snapshot_reading_denial_is_detected(self):
        r = ev.evaluate(fake(reads_snapshot=True), QS, "v1_2")
        self.assertFalse(r["A6_pass"])
        self.assertTrue(any("snapshot" in why for _, why in r["failures"]))

    def test_leaky_not_found_is_detected(self):
        r = ev.evaluate(fake(leaky=True), QS, "v1_2")
        self.assertFalse(r["A6_pass"])
        self.assertTrue(r["cross_clinic_leak_wordings"] > 0)

    def test_wording_dependent_denial_is_detected(self):
        r = ev.evaluate(fake(wording_dependent=True), QS, "v1_2")
        self.assertFalse(r["A6_pass"])

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            ev.evaluate(fake(), QS, "v9")

    def test_accept_output_path_refused(self):
        self.assertEqual(ev.main(["--policy", "v1_2", "--out", "/x/longitudinal_v1_accept.json"]), 2)


class RealRouteTests(unittest.TestCase):
    """Runs the evaluator against the shipping /ask route on a throwaway database (the checkout's own database is never opened)."""

    def test_real_route_v1_2_passes_and_v1_1_fails_only_on_the_admin_control(self):
        import db.database as database
        before = database.DB_PATH
        good = ev.run("v1_2", QS)
        self.assertTrue(good["A6_pass"], good["failures"])
        self.assertFalse(good["credentials_in_report"])
        bad = ev.run("v1_1", QS)
        self.assertFalse(bad["A6_pass"])
        self.assertEqual({c for c, _ in bad["failures"]}, {"admin_a_own_patient"})
        self.assertEqual(database.DB_PATH, before)
        self.assertNotIn("Bearer", json.dumps(good))


if __name__ == "__main__":
    unittest.main()
