"""
AI-2 deterministic memory foundation (memory/): snapshot, timeline, executor, claims, verifier,
the S2 dataset and its materialisation through the real API -- no model anywhere.

Unit tests use hand-made store-shaped records. Integration tests build S2 once into a
temporary database (never db/ayurcore.db) through the real app and check that snapshots
reproduce the specification, that unknown values stay unknown, that clinics and patients
are isolated, and that every rendered claim verifies while forged ones are rejected.
"""
import ast
import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import s2_build
from memory.answer import render
from memory.executor import SUPPORTED, Query, QueryError, execute
from memory.snapshot import PatientNotFound, PatientSnapshot, build_snapshot, snapshot_from_records
from memory.timeline import build_timeline
from memory.verifier import verify_claim

_spec = importlib.util.spec_from_file_location("s2_generator", ROOT / "scripts" / "s2_generator.py")
s2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s2)
S2_FINGERPRINT = "d25c3235c33e36febb386f74aeb868c456f723452559e0e3a18f14e2ac50c440"


def rx(pid, at, dosha, herbs):
    return {"prescription_id": pid, "created_at": at, "status": "Active", "dosha_imbalance": dosha,
            "prescribed_herbs": None if herbs is None else [{"name": n, "grams": g} for n, g in herbs]}


def enc(pk, at, closed=None):
    return {"encounter_pk": pk, "encounter_ref": f"ref{pk}", "status": "CLOSED" if closed else "OPEN",
            "created_at": at, "closed_at": closed}


# oldest -> newest: 11, 12 (unknown herbs and dosha), 13, 14
SNAP = snapshot_from_records("c1", 7, [
    rx(14, "2026-01-04 10:00:00", "Pitta", [("Neem", 40), ("Brahmi", 25)]),
    rx(11, "2026-01-01 10:00:00", "Vata", [("Ashwagandha", 50), ("Brahmi", 20)]),
    rx(13, "2026-01-03 10:00:00", "Vata", [("Ashwagandha", 60), ("Neem", 30)]),
    rx(12, "2026-01-02 10:00:00", None, None),
], [enc(2, "2026-01-03 09:59:00", "2026-01-03 10:30:00"), enc(1, "2026-01-01 10:00:00", "2026-01-01 10:20:00")])
KNOWN = snapshot_from_records("c1", 8, [
    rx(21, "2026-02-01 10:00:00", "Kapha", [("Pippali", 20)]),
    rx(22, "2026-02-02 10:00:00", "Kapha", [("Pippali", 30), ("Vasaka", 40)]),
], [])
EMPTY = PatientSnapshot("c1", 9)


def battery(snapshot) -> list[Query]:
    herbs = sorted({n for p in snapshot.prescriptions for n, _ in (p.herbs or ())}) or ["Neem"]
    doshas = sorted({p.dosha for p in snapshot.prescriptions if p.dosha}) or ["Vata"]
    qs = [Query("first", "prescription"), Query("last", "prescription"), Query("count", "prescription"),
          Query("list", "prescription"), Query("first", "dosha"), Query("last", "dosha"), Query("list", "dosha"),
          Query("first", "encounter"), Query("last", "encounter"), Query("count", "encounter"),
          Query("list", "encounter"), Query("list", "herb"), Query("diff", "prescription"),
          Query("compare", "dosha"), Query("count", "prescription", last_n=2), Query("count", "dosha", dosha=doshas[0])]
    for h in herbs[:3]:
        qs += [Query("first", "herb", herb=h), Query("last", "herb", herb=h), Query("count", "herb", herb=h),
               Query("sum", "herb", herb=h), Query("compare", "herb", herb=h, a="first", b="last")]
    return qs


class SnapshotTimelineTests(unittest.TestCase):
    def test_chronological_and_unknown_preserved(self):
        self.assertEqual([p.prescription_id for p in SNAP.prescriptions], [11, 12, 13, 14])
        unknown = SNAP.prescriptions[1]
        self.assertIsNone(unknown.dosha)
        self.assertIsNone(unknown.herbs)
        self.assertIsNone(unknown.grams_of("Neem"))
        self.assertEqual(SNAP.prescriptions[2].grams_of("Brahmi"), 0)

    def test_snapshot_carries_no_identity(self):
        fields = set(PatientSnapshot.__dataclass_fields__)
        self.assertEqual(fields, {"clinic_id", "patient_pk", "prescriptions", "encounters"})

    def test_timeline_order_and_same_second_flag(self):
        events = build_timeline(SNAP)
        self.assertEqual([e.at for e in events], sorted(e.at for e in events))
        same = [e for e in events if e.at == "2026-01-01 10:00:00"]
        self.assertEqual([e.kind for e in same], ["ENCOUNTER_OPENED", "PRESCRIPTION_CONFIRMED"])
        self.assertTrue(all(e.same_second for e in same))
        self.assertFalse(any(e.same_second for e in events if e.at == "2026-01-04 10:00:00"))


class ExecutorTests(unittest.TestCase):
    def check(self, q, status, value, labels=None, snapshot=SNAP):
        r = execute(snapshot, q)
        self.assertEqual((r.status, r.value), (status, value), q)
        if labels is not None:
            self.assertEqual(r.labels, frozenset(labels), q)
        return r

    def test_first_last(self):
        self.check(Query("last", "dosha"), "ok", {"prescription": "rx:14", "dosha": "Pitta"}, ["rx:14"])
        self.check(Query("first", "herb", herb="Ashwagandha"), "ok",
                   {"prescription": "rx:11", "created_at": "2026-01-01 10:00:00", "grams": 50}, ["rx:11"])
        self.check(Query("first", "herb", herb="Neem"), "incomplete",  # rx:12 (unknown) lies before the first known Neem
                   {"prescription": "rx:13", "created_at": "2026-01-03 10:00:00", "grams": 30}, ["rx:13", "rx:12"])
        self.check(Query("last", "herb", herb="Neem"), "ok",
                   {"prescription": "rx:14", "created_at": "2026-01-04 10:00:00", "grams": 40}, ["rx:14"])
        self.check(Query("last", "herb", herb="Kutki"), "unknown", None, ["rx:12"])
        self.check(Query("last", "herb", herb="Kutki"), "not_recorded", None, [], snapshot=KNOWN)
        self.check(Query("first", "encounter"), "ok",
                   {"encounter": "enc:ref1", "opened_at": "2026-01-01 10:00:00", "closed_at": "2026-01-01 10:20:00"})
        self.assertEqual(execute(SNAP, Query("first", "prescription")).status, "ok")

    def test_unknown_dosha_is_not_inferred(self):
        self.check(Query("last", "dosha", last_n=None), "ok", {"prescription": "rx:14", "dosha": "Pitta"})
        unknown_last = snapshot_from_records("c1", 1, [rx(1, "2026-01-01 00:00:00", "Vata", [("Neem", 5)]),
                                                       rx(2, "2026-01-02 00:00:00", None, None)], [])
        self.check(Query("last", "dosha"), "unknown", None, ["rx:2"], snapshot=unknown_last)
        r = self.check(Query("last", "prescription"), "incomplete",
                       {"prescription": "rx:2", "created_at": "2026-01-02 00:00:00", "dosha": None, "herbs": None},
                       snapshot=unknown_last)
        self.assertIn("rx:2", r.unknown_sources)

    def test_count_sum_list(self):
        self.check(Query("count", "prescription"), "ok", 4)
        self.check(Query("count", "encounter"), "ok", 2)
        self.check(Query("count", "herb", herb="Brahmi"), "incomplete", 2, ["rx:11", "rx:14", "rx:12"])
        self.check(Query("sum", "herb", herb="Brahmi"), "incomplete", 45)
        self.check(Query("sum", "herb", herb="Pippali"), "ok", 50, ["rx:21", "rx:22"], snapshot=KNOWN)
        self.check(Query("sum", "herb", herb="Neem"), "not_recorded", None, snapshot=KNOWN)
        self.check(Query("count", "herb", herb="Neem"), "ok", 0, snapshot=KNOWN)
        self.check(Query("count", "dosha", dosha="Vata"), "incomplete", 2)
        self.check(Query("count", "prescription", last_n=2), "ok", 2, ["rx:13", "rx:14"])
        self.check(Query("list", "herb"), "incomplete", ["Ashwagandha", "Brahmi", "Neem"])
        self.check(Query("list", "dosha"), "incomplete",
                   [{"prescription": "rx:11", "dosha": "Vata"}, {"prescription": "rx:13", "dosha": "Vata"},
                    {"prescription": "rx:14", "dosha": "Pitta"}])
        self.check(Query("list", "prescription"), "ok", ["rx:11", "rx:12", "rx:13", "rx:14"])

    def test_diff_and_compare(self):
        self.check(Query("diff", "prescription"), "ok",
                   {"a": "rx:13", "b": "rx:14", "added": [["Brahmi", 25]], "removed": [["Ashwagandha", 60]],
                    "changed": [["Neem", 30, 40]], "dosha_a": "Vata", "dosha_b": "Pitta", "dosha_changed": True})
        r = self.check(Query("diff", "prescription", a="rx:12", b="last"), "incomplete",
                       {"a": "rx:12", "b": "rx:14", "added": None, "removed": None, "changed": None,
                        "dosha_a": None, "dosha_b": "Pitta", "dosha_changed": None})
        self.assertIn("rx:12", r.unknown_sources)
        self.check(Query("compare", "herb", herb="Ashwagandha", a="first", b="previous"), "ok",
                   {"a": "rx:11", "b": "rx:13", "grams_a": 50, "grams_b": 60, "delta": 10})
        self.check(Query("compare", "herb", herb="Neem", a="rx:12", b="last"), "unknown", None, ["rx:12"])
        self.check(Query("compare", "dosha", a="first", b="last"), "ok",
                   {"a": "rx:11", "b": "rx:14", "dosha_a": "Vata", "dosha_b": "Pitta", "changed": True})

    def test_not_recorded_on_empty_history(self):
        for q in battery(EMPTY):
            with self.subTest(q):
                r = execute(EMPTY, q)
                self.assertIn(r.status, ("not_recorded", "ok"))
                if r.status == "ok":
                    self.assertEqual(r.value, 0)

    def test_closed_vocabulary(self):
        bad = [Query("average", "herb", herb="Neem"), Query("sum", "prescription"), Query("first", "patient"),
               Query("count", "herb"), Query("count", "dosha"), Query("list", "prescription", herb="Neem"),
               Query("count", "prescription", last_n=0), Query("count", "encounter", last_n=2),
               Query("diff", "prescription", a="DROP TABLE"), Query("diff", "prescription", a="last", b="last")]
        for q in bad:
            with self.subTest(q):
                with self.assertRaises(QueryError):
                    execute(SNAP, q)
        with self.assertRaises(QueryError):
            Query.from_dict({"op": "count", "target": "prescription", "sql": "SELECT 1"})

    def test_single_prescription_has_nothing_to_compare(self):
        one = snapshot_from_records("c1", 3, [rx(31, "2026-03-01 10:00:00", "Vata", [("Neem", 10)])], [])
        self.check(Query("compare", "herb", herb="Neem", a="first", b="last"), "not_recorded", None, snapshot=one)
        self.check(Query("diff", "prescription"), "not_recorded", None, snapshot=one)

    def test_selector_cannot_reach_another_patient(self):
        self.check(Query("compare", "herb", herb="Pippali", a="rx:21", b="last"), "not_recorded", None)

    def test_every_supported_pair_is_exercised(self):
        used = {(q.op, q.target) for q in battery(SNAP)}
        self.assertEqual(used, SUPPORTED)


class VerifierTests(unittest.TestCase):
    def test_rendered_claims_verify(self):
        for snapshot in (SNAP, KNOWN, EMPTY):
            for q in battery(snapshot):
                with self.subTest((snapshot.patient_pk, q)):
                    claim = render(snapshot, q)
                    self.assertEqual(verify_claim(snapshot, claim)["verdict"], "verified", claim)

    def test_forged_claims_are_rejected(self):
        base = render(SNAP, Query("sum", "herb", herb="Brahmi"))  # incomplete: at least 45 (rx:12 unknown)
        self.assertEqual(base["qualifier"], "at_least")
        mutations = {
            "value_mismatch": lambda c: c.update(value=46),
            "status_mismatch": lambda c: c.update(status="ok"),
            "qualifier_mismatch": lambda c: c.update(qualifier="exact"),
            "invented_source": lambda c: c["sources"].append("rx:21"),
            "missing_citation": lambda c: c["sources"].remove("rx:12"),
            "extra_citation": lambda c: c["sources"].append("rx:13"),
            "unsupported_number": lambda c: c.update(text=c["text"] + " Usually 90 g."),
            "unsupported_herb": lambda c: c.update(text=c["text"] + " Also Kutki."),
            "foreign_label": lambda c: c.update(text=c["text"] + " See rx:21."),
            "qualifier_text_missing": lambda c: c.update(text="sum herb Brahmi: 45."),
            "unsupported_query": lambda c: c.update(query={**c["query"], "op": "median"}),
            "malformed_claim": lambda c: c.pop("text"),
        }
        for code, mutate in mutations.items():
            with self.subTest(code):
                claim = copy.deepcopy(base)
                mutate(claim)
                frozen = copy.deepcopy(claim)
                out = verify_claim(SNAP, claim)
                self.assertEqual(out["verdict"], "rejected")
                self.assertIn(code, {v["code"] for v in out["violations"]})
                self.assertEqual(claim, frozen, "verifier modified the claim")

    def test_unknown_answer_must_not_state_a_value(self):
        claim = render(SNAP, Query("last", "herb", herb="Kutki"))
        self.assertEqual(claim["qualifier"], "unknown")
        forged = {**claim, "status": "ok", "qualifier": "exact", "value": {"grams": 20}, "text": "Kutki: 20 g."}
        codes = {v["code"] for v in verify_claim(SNAP, forged)["violations"]}
        self.assertTrue({"status_mismatch", "qualifier_mismatch", "value_mismatch"} <= codes)


class S2DatasetTests(unittest.TestCase):
    def test_frozen_and_deterministic(self):
        self.assertEqual(s2.verify(), [])
        spec = s2.generate(s2.SEED)
        self.assertEqual(s2.fingerprint(spec), S2_FINGERPRINT)
        self.assertEqual(json.loads(s2.DATA_FILE.read_text(encoding="utf-8")), spec)
        c = s2.counts(spec)
        self.assertEqual((c["clinics"], c["patients"]), (2, 20))
        self.assertGreaterEqual(c["legacy_unknown"], 2)
        ids = [[p["clinic_patient_id"] for p in cl["patients"]] for cl in spec["clinics"]]
        self.assertEqual(ids[0], ids[1], "both clinics reuse the same clinic-patient IDs")

    def test_build_refuses_unsafe_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = pathlib.Path(tmp) / "x.db"
            existing.write_bytes(b"")
            for path in (pathlib.Path(tmp) / "ayurcore.db", existing):
                with self.subTest(path.name), self.assertRaises(s2_build.UnsafeBuildTarget):
                    s2_build.build(s2.generate(), str(path))


class MaterialisedS2Tests(unittest.TestCase):
    """S2 built once through the real API into a temporary database."""

    @classmethod
    def setUpClass(cls):
        import db.database as database
        cls.database = database
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(pathlib.Path(cls.tmp.name) / "s2_build.db")
        cls.saved = database.DB_PATH
        cls.spec = s2.generate()
        cls.mapping = s2_build.build(cls.spec, cls.db_path)
        assert database.DB_PATH == cls.saved, "build must restore DB_PATH"
        database.DB_PATH = cls.db_path  # read side for the tests below

    @classmethod
    def tearDownClass(cls):
        cls.database.DB_PATH = cls.saved
        cls.tmp.cleanup()

    def patients(self):
        for clinic in self.spec["clinics"]:
            for p in clinic["patients"]:
                yield clinic["clinic_id"], p, self.mapping["clinics"][clinic["clinic_id"]]["patients"][p["key"]]

    def test_snapshots_reproduce_the_specification(self):
        for cid, p, m in self.patients():
            with self.subTest(p["key"]):
                snap = build_snapshot(cid, m["patient_pk"])
                visits_rx = [v for v in p["visits"] if v["kind"] != "encounter_only"]
                self.assertEqual([x.prescription_id for x in snap.prescriptions], m["prescription_ids"])
                self.assertEqual(len(snap.encounters), sum(v["kind"] in ("rx", "encounter_only") for v in p["visits"]))
                for visit, got in zip(visits_rx, snap.prescriptions):
                    if visit["kind"] == "legacy_unknown":
                        self.assertEqual((got.dosha, got.herbs), (None, None))  # stays unknown
                    else:
                        self.assertEqual(got.dosha, visit["dosha"])
                        self.assertEqual([list(h) for h in got.herbs], visit["herbs"])

    def test_clinic_and_patient_isolation(self):
        a = self.mapping["clinics"]["s2_clinic_a"]["patients"]
        b = self.mapping["clinics"]["s2_clinic_b"]["patients"]
        for key_a, key_b in zip(sorted(a), sorted(b)):
            with self.subTest(key_a):
                with self.assertRaises(PatientNotFound):
                    build_snapshot("s2_clinic_b", a[key_a]["patient_pk"])
                with self.assertRaises(PatientNotFound):
                    build_snapshot("s2_clinic_a", b[key_b]["patient_pk"])
                self.assertNotEqual(a[key_a]["patient_pk"], b[key_b]["patient_pk"])
        with self.assertRaises(PatientNotFound):
            build_snapshot("s2_clinic_a", 10 ** 9)
        labels = {}
        for cid, p, m in self.patients():
            for label in build_snapshot(cid, m["patient_pk"]).labels():
                self.assertNotIn(label, labels, "a record label belongs to two patients")
                labels[label] = p["key"]

    def test_claims_on_materialised_histories(self):
        snaps = [(p["key"], build_snapshot(cid, m["patient_pk"])) for cid, p, m in self.patients()]
        statuses = {}
        for key, snap in snaps:
            for q in battery(snap):
                claim = render(snap, q)
                out = verify_claim(snap, claim)
                self.assertEqual(out["verdict"], "verified", (key, q, out))
                statuses[claim["status"]] = statuses.get(claim["status"], 0) + 1
        self.assertTrue({"ok", "incomplete", "unknown", "not_recorded"} <= set(statuses), statuses)
        print(f"\nS2 claims verified by status: {dict(sorted(statuses.items()))}")
        # a claim citing another patient's record is rejected for this patient
        (_, s1), (_, s2_snap) = [(k, s) for k, s in snaps if s.prescriptions][:2]
        claim = render(s1, Query("count", "prescription"))
        claim["sources"].append(next(iter(s2_snap.prescriptions)).label)
        self.assertIn("invented_source", {v["code"] for v in verify_claim(s1, claim)["violations"]})


EXPERIMENT_MODULES_USING_NUMPY = {"semantic.py", "evaluate_v3_semantic.py"}


class BoundaryTests(unittest.TestCase):
    def test_memory_module_has_no_sql_writes_or_models(self):
        for path in sorted((ROOT / "memory").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                      {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            with self.subTest(path.name):
                # numpy is allowed ONLY in the two rejected cue-tagger / semantic experiment modules (Block 5A, never part of ai2/);
                # every other forbidden import stays forbidden everywhere.
                forbidden = ("google", "openai", "anthropic", "agents", "extractor", "langchain") + (() if path.name in EXPERIMENT_MODULES_USING_NUMPY else ("numpy",))
                self.assertFalse({m for m in modules if m.split(".")[0] in forbidden})
                if path.name != "s2_build.py":  # only the S2 builder writes, and only to its own new database
                    self.assertNotRegex(source, r"\b(INSERT|UPDATE|DELETE|SELECT)\b")
                    self.assertNotIn(".execute(", source)


if __name__ == "__main__":
    unittest.main()
