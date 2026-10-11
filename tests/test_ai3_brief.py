"""
AI-3 Consultation Brief: verified facts, tamper rejection, determinism, read-only behaviour, model integrity and the evaluation's one-shot lock.
Synthetic data only; no database, no network. Runs under plain unittest or pytest.
"""
import ast
import dataclasses
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ai3.brief as brief_mod  # noqa: E402
from ai2.snapshot import PatientSnapshot, snapshot_from_records  # noqa: E402
from ai3.brief import load_model, make_brief, verified_facts  # noqa: E402
from ai3.evaluate import evaluate  # noqa: E402
from ai3.facts import Fact, candidate_facts, fact_supported  # noqa: E402
from ai3.salience import SalienceModel  # noqa: E402
from ai3.synthetic import fingerprint, generate_cases, reference_policy  # noqa: E402

AS_OF = "2026-10-10"


def rx(i, day, dosha, herbs):
    return {"prescription_id": i, "created_at": f"{day} 09:00:00", "status": "CONFIRMED", "dosha_imbalance": dosha,
            "prescribed_herbs": None if herbs is None else [{"name": n, "grams": g} for n, g in herbs]}


def enc(pk, ref, day, closed=True):
    return {"encounter_pk": pk, "encounter_ref": ref * 16, "status": "CLOSED" if closed else "OPEN", "created_at": f"{day} 09:00:00",
            "closed_at": f"{day} 10:00:00" if closed else None}


def snap(rxs, encs=()):
    return snapshot_from_records("synthetic", 1, rxs, list(encs))


CHANGED = snap([rx(1, "2026-08-01", "Vata", [("Neem", 10), ("Brahmi", 5)]), rx(2, "2026-09-20", "Pitta", [("Neem", 20), ("Guggulu", 12)])],
               [enc(1, "a", "2026-09-20"), enc(2, "b", "2026-10-02", closed=False)])
LEGACY = snap([rx(1, "2026-03-01", None, None), rx(2, "2026-03-02", "Vata", None)])


class FactTests(unittest.TestCase):
    def kinds(self, s):
        return sorted(f.kind for f in candidate_facts(s))

    def test_a_changed_patient_has_the_expected_facts_and_all_are_supported(self):
        self.assertEqual(self.kinds(CHANGED), sorted(["last_prescription", "first_prescription", "herbs_changed", "dosha_changed", "count_prescriptions",
                                                      "open_visit", "count_visits"]))
        for f in candidate_facts(CHANGED):
            self.assertEqual(fact_supported(CHANGED, f), (True, ""), f.text)

    def test_the_sentences_state_only_recorded_values(self):
        by = {f.kind: f.text for f in candidate_facts(CHANGED)}
        self.assertEqual(by["dosha_changed"], "Dosha changed from Vata to Pitta since #1.")
        self.assertEqual(by["herbs_changed"], "Herbs since #1: added Guggulu 12 g; removed Brahmi 5 g; changed Neem 10 g to 20 g.")
        self.assertEqual(by["open_visit"], "A visit opened 2026-10-02 is still open.")
        self.assertEqual(by["last_prescription"], "Latest prescription (#2, 2026-09-20): dosha Pitta; Neem 20 g, Guggulu 12 g.")

    def test_unknown_values_are_reported_as_unknown_never_filled_in(self):
        facts = {f.kind: f for f in candidate_facts(LEGACY)}
        self.assertIn("herbs not recorded", facts["last_prescription"].text)
        unknown = [f.text for f in candidate_facts(LEGACY) if f.kind == "unknown_data"]
        self.assertEqual(sorted(unknown), ["Dosha is not recorded on: #1.", "Herbs are not recorded on: #1, #2."])
        self.assertFalse(facts["last_prescription"].exact)
        self.assertNotIn("herbs_changed", facts)            # nothing can be compared when herbs are unknown
        self.assertNotIn("no_change", facts)

    def test_an_empty_history_has_no_facts_and_an_empty_brief(self):
        empty = snap([])
        self.assertEqual(candidate_facts(empty), [])
        b = make_brief(empty, AS_OF)
        self.assertEqual((b.items, b.omitted, b.rejected), ((), 0, 0))

    def test_no_change_is_stated_only_when_both_herbs_and_dosha_are_known_and_equal(self):
        same = snap([rx(1, "2026-08-01", "Vata", [("Neem", 10)]), rx(2, "2026-09-01", "Vata", [("Neem", 10)])])
        self.assertIn("no_change", [f.kind for f in candidate_facts(same)])
        self.assertNotIn("herbs_changed", [f.kind for f in candidate_facts(same)])


class TamperTests(unittest.TestCase):
    def fact(self, kind):
        return next(f for f in candidate_facts(CHANGED) if f.kind == kind)

    def test_an_invented_number_is_rejected(self):
        f = self.fact("herbs_changed")
        bad = dataclasses.replace(f, text=f.text.replace("20 g", "25 g"))
        self.assertFalse(fact_supported(CHANGED, bad)[0])

    def test_an_invented_herb_is_rejected(self):
        f = self.fact("last_prescription")
        bad = dataclasses.replace(f, text=f.text + " Triphala 5 g.")
        self.assertFalse(fact_supported(CHANGED, bad)[0])

    def test_a_claim_that_disagrees_with_the_snapshot_is_rejected_by_the_verifier(self):
        f = self.fact("count_prescriptions")
        claim = json.loads(json.dumps(f.claim))
        claim["value"] = 3
        ok, why = fact_supported(CHANGED, dataclasses.replace(f, claim=claim, text="3 prescriptions on record."))
        self.assertFalse(ok)
        self.assertIn("verifier", why)

    def test_a_citation_that_is_not_in_the_snapshot_is_rejected(self):
        f = self.fact("last_prescription")
        claim = json.loads(json.dumps(f.claim))
        claim["sources"] = ["rx:999"]
        self.assertFalse(fact_supported(CHANGED, dataclasses.replace(f, claim=claim, labels=("rx:999",)))[0])


class BriefTests(unittest.TestCase):
    def test_a_brief_is_deterministic_and_does_not_change_the_snapshot(self):
        before = hash(CHANGED)
        a, b = make_brief(CHANGED, AS_OF), make_brief(CHANGED, AS_OF)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(hash(CHANGED), before)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            CHANGED.patient_pk = 2

    def test_both_arms_show_at_most_five_verified_cited_facts_and_count_the_rest(self):
        for arm in ("template", "learned"):
            b = make_brief(CHANGED, AS_OF, arm=arm)
            self.assertLessEqual(len(b.items), 5)
            self.assertEqual(len(b.items) + b.omitted, len(candidate_facts(CHANGED)))
            self.assertEqual(b.rejected, 0)
            for item in b.items:
                self.assertTrue(item.labels)
                self.assertTrue(all(CHANGED.prescription(l) or any(e.label == l for e in CHANGED.encounters) for l in item.labels))
            self.assertIn("not advice", b.caveat)
            self.assertEqual([i.rank for i in b.items], list(range(1, len(b.items) + 1)))

    def test_the_budget_is_respected(self):
        self.assertEqual(len(make_brief(CHANGED, AS_OF, max_items=2).items), 2)

    def test_the_learned_arm_puts_a_dosha_change_and_an_open_visit_before_the_counts_and_the_template_arm_does_not(self):
        learned = [i.kind for i in make_brief(CHANGED, AS_OF, arm="learned", max_items=3).items]
        template = [i.kind for i in make_brief(CHANGED, AS_OF, arm="template", max_items=3).items]
        self.assertIn("dosha_changed", learned)
        self.assertIn("open_visit", learned)
        self.assertNotIn("dosha_changed", template)             # the fixed order lists it after the counts

    def test_unknown_data_on_the_latest_prescription_is_surfaced_by_the_learned_arm(self):
        self.assertIn("unknown_data", [i.kind for i in make_brief(LEGACY, AS_OF, max_items=3).items])

    def test_an_unknown_arm_is_refused(self):
        with self.assertRaises(ValueError):
            make_brief(CHANGED, AS_OF, arm="oracle")

    def test_every_generated_fact_of_every_synthetic_patient_is_supported(self):
        bad = 0
        for s, as_of in generate_cases(777, 60):
            facts, rejected = verified_facts(s)
            bad += rejected
            for f in facts:
                bad += 0 if fact_supported(s, f)[0] else 1
        self.assertEqual(bad, 0)


class ModelTests(unittest.TestCase):
    def test_the_packaged_model_is_the_evaluated_one(self):
        self.assertEqual(hashlib.sha256(pathlib.Path(brief_mod.MODEL_FILE).read_bytes()).hexdigest(), brief_mod.MODEL_SHA256)
        self.assertIsInstance(load_model(), SalienceModel)

    def test_another_model_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "m.json"
            path.write_bytes(pathlib.Path(brief_mod.MODEL_FILE).read_bytes() + b" ")
            with self.assertRaises(RuntimeError):
                load_model(path)

    def test_a_model_for_other_features_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "m.json"
            path.write_text(json.dumps({"kinds": ["x"], "context": [], "weights": {}}))
            with self.assertRaises(ValueError):
                SalienceModel.load(path)


class BoundaryTests(unittest.TestCase):
    SERVICE = ("__init__", "facts", "salience", "brief")

    def imports(self, name):
        tree = ast.parse((ROOT / "ai3" / f"{name}.py").read_text(encoding="utf-8"))
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
        return {m.split(".")[0] for m in mods}

    def test_the_service_modules_import_only_stdlib_ai2_and_ai3(self):
        allowed = set(sys.stdlib_module_names) | {"ai2", "ai3"}
        for name in self.SERVICE:
            self.assertEqual(self.imports(name) - allowed, set(), name)

    def test_nothing_in_ai3_touches_a_database_the_network_or_files_for_writing(self):
        for p in (ROOT / "ai3").glob("*.py"):
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"sqlite3|\.execute\(|socket|urllib|requests|httpx|subprocess|write_text|write_bytes|open\([^)]*['\"]w", p.name)
            self.assertNotIn("backend", self.imports(p.stem), p.name)


class EvaluationTests(unittest.TestCase):
    def test_a_small_evaluation_reports_all_arms_with_nothing_unsupported(self):
        res = evaluate(generate_cases(555, 40), load_model(), 5)
        self.assertEqual(set(res["arms"]), {"template", "learned", "reference"})
        for arm in res["arms"].values():
            self.assertEqual(arm["unsupported_shown_facts"], 0)
            self.assertTrue(0 <= arm["recall"] <= 1 and 0 <= arm["precision"] <= 1)

    def test_the_synthetic_splits_are_reproducible_and_match_the_recorded_dev_fingerprint(self):
        self.assertEqual(fingerprint(generate_cases(20261102, 600)), json.loads((ROOT / "eval" / "ai3" / "dev_report.json").read_text())["fingerprints"]["dev"])

    def test_the_reference_policy_is_the_documented_one(self):
        ctx = {"n_rx": 0.3, "n_enc": 0.0, "has_diff": 1.0, "herbs_changed": 1.0, "dosha_changed": 0.0, "any_unknown": 0.0, "last_unknown": 0.0,
               "open_visit": 0.0, "days_since_rx": 100 / 365, "days_since_visit": 1.0, "inexact": 0.0}
        self.assertTrue(reference_policy("herbs_changed", ctx))
        self.assertFalse(reference_policy("herbs_changed", {**ctx, "days_since_rx": 200 / 365}))
        self.assertTrue(reference_policy("dosha_changed", ctx))
        self.assertFalse(reference_policy("count_visits", ctx))
        self.assertFalse(reference_policy("first_prescription", ctx))

    def test_the_final_evaluation_refuses_to_run_twice(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ai3_train_eval", ROOT / "scripts" / "ai3_train_eval.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            lock = pathlib.Path(tmp) / "FINAL_DONE"
            lock.write_text("x")
            with mock.patch.object(mod, "LOCK", lock), mock.patch.object(mod, "evaluate", side_effect=AssertionError("must not evaluate")):
                self.assertEqual(mod.main(["final"]), 2)


if __name__ == "__main__":
    unittest.main()
