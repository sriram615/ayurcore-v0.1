"""
POST /patients/{patient_pk}/ask -- the thin AI-2 endpoint.

Covers: authentication (401), role (DOCTOR only, 403), clinic isolation (a foreign patient is the same 404 as a missing one, the clinic never comes from
the request), every outcome class (answer, clarify, missing, unsupported, unsafe, access denied; incomplete by pass-through), clarification
resubmission (only a reading AI-2 itself offers for that question is accepted; tampering is a 422), structured evidence (no internal encounter key, no
clinic or identity fields), malformed requests, planner-unavailable behaviour, and write-safety (no table changes, no external call, no parser call).
Synthetic data only; runs in the isolated sandbox (`scripts/run_isolated_tests.py`).
"""
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

import backend.main as main_module  # noqa: E402
from backend.auth_service import create_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _login(client, clinic_id, role="DOCTOR"):
    username, password = f"ai2api_{secrets.token_hex(6)}", "ai2-api-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _stock(clinic_id, herbs):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) VALUES (?, ?, ?, ?, ?, ?)",
                         [(clinic_id, h, "test botanical name", 100000, "TEST-BATCH", 90) for h in herbs])
        conn.commit()
    finally:
        conn.close()


def _register(client, headers, label):
    resp = client.post("/patients", json={"clinic_patient_id": f"{label}-{secrets.token_hex(3)}", "display_name": f"Ai2 Api {label}"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["patient_pk"]


def _prescribe(client, headers, patient_pk, dosha, herbs):
    body = {"patient_name": "x", "patient_pk": patient_pk, "dosha_imbalance": dosha, "prescribed_herbs": [{"name": n, "grams": g} for n, g in herbs]}
    prev = client.post("/prescribe/manual-preview", json=body, headers=headers)
    assert prev.status_code == 200, prev.text
    conf = client.post("/prescribe/confirm", json={"preview_id": prev.json()["preview_id"]}, headers=headers)
    assert conf.status_code == 200, conf.text
    return conf.json()["prescription_id"]


def _db_fingerprint():
    """A hash of every table's content except sessions (a login/auth lookup may touch it)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        h = hashlib.sha256()
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            if name == "sessions":
                continue
            rows = conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
            h.update(name.encode() + repr(rows).encode())
        return h.hexdigest()
    finally:
        conn.close()


def _keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _keys(v)
    elif isinstance(o, list):
        for x in o:
            yield from _keys(x)


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_cm = TestClient(main_module.app)
        cls.client = cls.client_cm.__enter__()
        cls.clinic_a, cls.clinic_b = f"ai2api_a_{secrets.token_hex(4)}", f"ai2api_b_{secrets.token_hex(4)}"
        _stock(cls.clinic_a, ["Ashwagandha", "Neem", "Brahmi"])
        _stock(cls.clinic_b, ["Ashwagandha", "Neem"])
        cls.doctor = _login(cls.client, cls.clinic_a)
        cls.doctor2 = _login(cls.client, cls.clinic_a)
        cls.admin = _login(cls.client, cls.clinic_a, "ADMIN")
        cls.auditor = _login(cls.client, cls.clinic_a, "AUDITOR")
        cls.doctor_b = _login(cls.client, cls.clinic_b)
        cls.p = _register(cls.client, cls.doctor, "history")
        cls.empty = _register(cls.client, cls.doctor, "empty")
        cls.p_b = _register(cls.client, cls.doctor_b, "other-clinic")
        cls.rx1 = _prescribe(cls.client, cls.doctor, cls.p, "Vata", [("Ashwagandha", 30), ("Neem", 10)])
        cls.rx2 = _prescribe(cls.client, cls.doctor, cls.p, "Pitta", [("Ashwagandha", 30), ("Brahmi", 5)])
        cls.rx3 = _prescribe(cls.client, cls.doctor, cls.p, "Vata", [("Ashwagandha", 40), ("Neem", 20)])
        enc = cls.client.post(f"/patients/{cls.p}/encounters", headers=cls.doctor)
        assert enc.status_code == 201, enc.text
        cls.encounter_pk, cls.encounter_ref = enc.json()["encounter_pk"], enc.json()["encounter_ref"]
        _prescribe(cls.client, cls.doctor_b, cls.p_b, "Kapha", [("Neem", 99)])

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def ask(self, question, patient=None, headers=None, **extra):
        body = {"question": question, **extra}
        return self.client.post(f"/patients/{patient or self.p}/ask", json=body, headers=self.doctor if headers is None else headers)


class AuthAndRoleTests(ApiCase):
    def test_no_credentials_and_bad_credentials_are_401(self):
        self.assertEqual(self.ask("What was the first prescription?", headers={}).status_code, 401)
        self.assertEqual(self.ask("What was the first prescription?", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.ask("What was the first prescription?", headers={"Authorization": "Basic abc"}).status_code, 401)

    def test_admin_and_auditor_are_403(self):
        for headers in (self.admin, self.auditor):
            r = self.ask("What was the first prescription?", headers=headers)
            self.assertEqual(r.status_code, 403, r.text)
            self.assertNotIn("prescription_id", r.text)

    def test_doctor_is_allowed_and_the_matrix_says_doctor_only(self):
        from backend.rbac import PERMISSION_MATRIX, ROLE_DOCTOR
        self.assertEqual(PERMISSION_MATRIX["memory_ask"], frozenset({ROLE_DOCTOR}))
        self.assertEqual(self.ask("What was the first prescription?").status_code, 200)

    def test_authentication_is_decided_before_validation_and_before_the_patient_lookup(self):
        self.assertEqual(self.client.post(f"/patients/{self.p}/ask", json={"nope": 1}).status_code, 401)
        self.assertEqual(self.client.post("/patients/999999/ask", json={"question": "x"}).status_code, 401)
        self.assertEqual(self.client.post(f"/patients/{self.p}/ask", json={"nope": 1}, headers=self.admin).status_code, 403)

    def test_a_logged_out_session_stops_working(self):
        headers = _login(self.client, self.clinic_a)
        self.assertEqual(self.ask("What was the first prescription?", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.ask("What was the first prescription?", headers=headers).status_code, 401)


class IsolationTests(ApiCase):
    def test_a_foreign_patient_is_the_same_404_as_a_missing_one(self):
        foreign = self.ask("What was the first prescription?", patient=self.p_b)
        missing = self.ask("What was the first prescription?", patient=999999)
        self.assertEqual((foreign.status_code, missing.status_code), (404, 404))
        self.assertEqual(foreign.json(), missing.json())
        self.assertEqual(foreign.json(), {"detail": "Patient not found."})

    def test_each_clinic_sees_only_its_own_patient_history(self):
        a = self.ask("How much Neem in total?", patient=self.p)
        b = self.ask("How much Neem in total?", patient=self.p_b, headers=self.doctor_b)
        self.assertEqual(a.json()["value"], 30)
        self.assertEqual(b.json()["value"], 99)
        self.assertEqual({e["prescription_id"] for e in a.json()["evidence"]} & {e["prescription_id"] for e in b.json()["evidence"]}, set())

    def test_a_colleague_in_the_same_clinic_may_ask_but_another_clinics_doctor_may_not(self):
        self.assertEqual(self.ask("What was the first prescription?", headers=self.doctor2).status_code, 200)
        self.assertEqual(self.ask("What was the first prescription?", headers=self.doctor_b).status_code, 404)

    def test_the_request_cannot_choose_the_clinic_or_the_patient(self):
        for extra in ({"clinic_id": self.clinic_b}, {"patient_pk": self.p_b}, {"patient_id": "x"}, {"plan": {"op": "last"}}, {"role": "ADMIN"}):
            r = self.ask("What was the first prescription?", **extra)
            self.assertEqual(r.status_code, 422, extra)
        r = self.client.post(f"/patients/{self.p}/ask?clinic_id={self.clinic_b}", json={"question": "What was the first prescription?"}, headers=self.doctor_b)
        self.assertEqual(r.status_code, 404)             # the query string is ignored: the session's clinic still decides
        r = self.client.post(f"/patients/{self.p}/ask", json={"question": "x"}, headers={**self.doctor_b, "X-Clinic-Id": self.clinic_a})
        self.assertEqual(r.status_code, 404)

    def test_path_validation(self):
        for bad in ("0", "-1", "abc", "1.5"):
            self.assertEqual(self.client.post(f"/patients/{bad}/ask", json={"question": "x"}, headers=self.doctor).status_code, 422, bad)


class OutcomeTests(ApiCase):
    def test_answer_carries_verified_structured_evidence(self):
        r = self.ask("What was the first prescription?")
        d = r.json()
        self.assertEqual((r.status_code, d["outcome"], d["category"], d["stage"], d["verified"], d["qualifier"]), (200, "answer", "answer", "done", True, "exact"))
        self.assertEqual(d["patient_pk"], self.p)
        self.assertEqual([e["prescription_id"] for e in d["evidence"]], [self.rx1])
        ev = d["evidence"][0]
        self.assertEqual((ev["kind"], ev["dosha"], ev["herbs"]), ("prescription", "Vata", [{"name": "Ashwagandha", "grams": 30}, {"name": "Neem", "grams": 10}]))
        self.assertTrue(d["text"])
        self.assertEqual(d["options"], [])

    def test_answers_in_manglish_and_malayalam(self):
        for q in ("Avasanathe prescription-il entha ullath?", "അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?"):
            d = self.ask(q).json()
            self.assertEqual((d["category"], [e["prescription_id"] for e in d["evidence"]]), ("answer", [self.rx3]), q)

    def test_count_and_comparison(self):
        self.assertEqual(self.ask("How many prescriptions has she had?").json()["value"], 3)
        d = self.ask("Did the Neem dose change since the previous prescription?").json()
        self.assertEqual(d["category"], "answer")
        self.assertEqual(len(d["evidence"]), 2)

    def test_clarify_has_readings_and_no_claim_or_evidence(self):
        d = self.ask("Neem count?").json()
        self.assertEqual((d["outcome"], d["category"], d["stage"], d["text"], d["evidence"], d["verified"]), ("clarify", "clarify", "clarify", None, [], False))
        self.assertEqual({o["frame"] for o in d["options"]}, {"COUNT_HERB", "SUM_HERB"})
        for o in d["options"]:
            self.assertEqual(set(o), {"frame", "text", "query"})
            self.assertEqual(set(o["query"]), {"op", "target", "herb", "dosha", "last_n", "a", "b"})

    def test_missing_is_an_abstention_not_an_empty_answer(self):
        d = self.ask("What was the first prescription?", patient=self.empty).json()
        self.assertEqual((d["outcome"], d["category"], d["reason"], d["evidence"]), ("abstain", "missing", "NOT_RECORDED", []))

    def test_unsupported(self):
        for q in ("What is the average Neem dose per prescription?", "Is the Neem dose rising from visit to visit?", "Read out prescription number three."):
            d = self.ask(q).json()
            self.assertEqual((d["outcome"], d["category"], d["evidence"], d["text"]), ("abstain", "unsupported", [], None), q)

    def test_unsafe_advice_and_diagnosis(self):
        for q in ("Should we increase the Neem dose?", "Can I stop Ashwagandha?", "What disease does she have?", "Ee rogikku Neem koottaamo?"):
            d = self.ask(q).json()
            self.assertEqual((d["outcome"], d["category"], d["evidence"]), ("abstain", "unsafe", []), q)

    def test_access_denied_for_questions_about_other_patients(self):
        for q in ("What did the other patient get?", "Which patients are on Neem?", "Munpathe rogiyude avasanathe dose ethra?"):
            d = self.ask(q).json()
            self.assertEqual((d["outcome"], d["category"], d["reason"], d["evidence"]), ("abstain", "access_denied", "OTHER_PATIENT", []), q)

    def test_incomplete_passes_through_with_its_qualifier(self):
        from ai2.result import Ai2Answer
        fake = Ai2Answer(outcome="answer", stage="done", reason=None, category="incomplete", text="At least 2.", value=2, qualifier="at_least", verified=True,
                         evidence=({"kind": "prescription", "label": "rx:1", "prescription_id": 1, "created_at": "t", "status": "CONFIRMED", "dosha": None, "herbs": None},))
        with mock.patch("ai2.Ai2Service.ask", return_value=fake):
            d = self.ask("anything").json()
        self.assertEqual((d["category"], d["qualifier"], d["value"], d["verified"]), ("incomplete", "at_least", 2, True))
        self.assertIsNone(d["evidence"][0]["herbs"])

    def test_the_same_request_gives_the_same_response(self):
        self.assertEqual(self.ask("How much Neem in total?").json(), self.ask("How much Neem in total?").json())


class EvidenceTests(ApiCase):
    def test_encounter_evidence_has_the_random_reference_and_no_sequential_key(self):
        for q in ("When did her most recent visit end?", "Is her latest visit still open?"):
            r = self.ask(q)
            d = r.json()
            self.assertEqual(d["category"], "answer", (q, d))
            self.assertIn(self.encounter_ref, json.dumps(d))
            ev = [e for e in d["evidence"] if e["kind"] == "encounter"]
            self.assertEqual(len(ev), 1)
            self.assertEqual(ev[0]["encounter_ref"], self.encounter_ref)
            self.assertNotIn("encounter_pk", set(_keys(d)))
            self.assertNotIn(f'"encounter_pk"', r.text)

    def test_no_clinic_identity_or_internal_field_in_any_response(self):
        banned = {"clinic_id", "display_name", "phone", "clinic_patient_id", "encounter_pk", "created_by_user_id", "user_id", "token", "preview_id", "audit_hash"}
        for q in ("What was the first prescription?", "Neem count?", "Should we increase the Neem dose?", "How many prescriptions has she had?",
                  "When did her most recent visit end?"):
            d = self.ask(q).json()
            self.assertFalse(banned & set(_keys(d)), q)
            self.assertNotIn(self.clinic_a, json.dumps(d))
            self.assertNotIn("Ai2 Api", json.dumps(d))

    def test_openapi_documents_no_internal_field(self):
        schemas = self.client.get("/openapi.json").json()["components"]["schemas"]
        for name in ("AskResponse", "AskEvidence", "AskOption", "AskRequest", "AskChoice"):
            self.assertIn(name, schemas)
            self.assertFalse({"clinic_id", "encounter_pk", "display_name"} & set(schemas[name]["properties"]), name)
        self.assertEqual(schemas["AskRequest"].get("additionalProperties"), False)

    def test_every_cited_record_belongs_to_the_patient(self):
        d = self.ask("List all her prescriptions.").json()
        self.assertEqual(sorted(e["prescription_id"] for e in d["evidence"]), sorted([self.rx1, self.rx2, self.rx3]))


class ClarificationSecurityTests(ApiCase):
    QUESTION = "Neem count?"

    def offered(self):
        return self.ask(self.QUESTION).json()["options"]

    def choose(self, frame, query, question=None, patient=None, headers=None):
        return self.ask(question or self.QUESTION, patient=patient, headers=headers, choice={"frame": frame, "query": query})

    def test_an_offered_reading_is_answered_like_the_unambiguous_question(self):
        options = {o["frame"]: o for o in self.offered()}
        count = self.choose("COUNT_HERB", options["COUNT_HERB"]["query"]).json()
        total = self.choose("SUM_HERB", options["SUM_HERB"]["query"]).json()
        self.assertEqual((count["outcome"], count["category"], count["clarified"], count["verified"]), ("answer", "answer", True, True))
        self.assertEqual(count["value"], self.ask("How many prescriptions included Neem?").json()["value"])
        self.assertEqual(total["value"], self.ask("How much Neem in total?").json()["value"])
        self.assertEqual(count["options"], [])
        self.assertTrue(count["evidence"])

    def test_the_chosen_reading_is_answered_for_the_session_patient_only(self):
        q = self.offered()[0]
        other = self.choose(q["frame"], q["query"], patient=self.p_b, headers=self.doctor_b).json()
        mine = self.choose(q["frame"], q["query"]).json()
        self.assertNotEqual({e["prescription_id"] for e in other["evidence"]}, {e["prescription_id"] for e in mine["evidence"]})
        self.assertEqual(self.choose(q["frame"], q["query"], patient=self.p_b).status_code, 404)

    def test_tampered_readings_are_rejected(self):
        good = {o["frame"]: o["query"] for o in self.offered()}
        count = good["COUNT_HERB"]
        tampered = {
            "other herb": ("COUNT_HERB", {**count, "herb": "Ashwagandha"}),
            "other op": ("COUNT_HERB", {**count, "op": "list"}),
            "other target": ("COUNT_HERB", {**count, "target": "dosha"}),
            "selector": ("COUNT_HERB", {**count, "a": "first"}),
            "window": ("COUNT_HERB", {**count, "last_n": 2}),
            "dosha": ("COUNT_HERB", {**count, "dosha": "Vata"}),
            "frame swapped": ("SUM_HERB", count),
            "unknown frame": ("DROP_ALL", count),
            "not offered but a valid frame": ("LAST_HERB", {**count, "op": "last"}),
            "empty query": ("COUNT_HERB", {}),
        }
        for label, (frame, query) in tampered.items():
            r = self.choose(frame, query)
            self.assertEqual(r.status_code, 422, (label, r.text))
            self.assertEqual(r.headers.get("X-Error-Code"), "CHOICE_NOT_OFFERED", label)
            self.assertNotIn("evidence", r.json())

    def test_malformed_readings_are_rejected_before_anything_runs(self):
        count = self.offered()[0]["query"]
        for label, choice in {
            "extra query key": {"frame": "COUNT_HERB", "query": {**count, "sql": "DROP TABLE patients"}},
            "extra choice key": {"frame": "COUNT_HERB", "query": count, "clinic_id": self.clinic_b},
            "query is a string": {"frame": "COUNT_HERB", "query": "op=count"},
            "query is a list": {"frame": "COUNT_HERB", "query": [count]},
            "frame is a list": {"frame": ["COUNT_HERB"], "query": count},
            "frame empty": {"frame": "", "query": count},
            "value is a dict": {"frame": "COUNT_HERB", "query": {**count, "herb": {"x": 1}}},
            "value is a bool": {"frame": "COUNT_HERB", "query": {**count, "last_n": True}},
            "huge value": {"frame": "COUNT_HERB", "query": {**count, "herb": "N" * 500}},
            "missing frame": {"query": count}, "missing query": {"frame": "COUNT_HERB"}, "null": None,
        }.items():
            r = self.ask(self.QUESTION, choice=choice) if choice is not None else self.client.post(f"/patients/{self.p}/ask", json={"question": self.QUESTION, "choice": {}}, headers=self.doctor)
            self.assertEqual(r.status_code, 422, (label, r.text))

    def test_a_reading_cannot_be_used_with_a_different_or_unambiguous_question(self):
        count = self.offered()[0]
        self.assertEqual(self.choose(count["frame"], count["query"], question="What was the first prescription?").status_code, 422)
        self.assertEqual(self.choose(count["frame"], count["query"], question="Ashwagandha count?").status_code, 422)   # a different herb's readings
        self.assertEqual(self.choose(count["frame"], count["query"], question="Neem amount?").status_code, 422)

    def test_a_refused_question_cannot_be_forced_through_a_choice(self):
        count = self.offered()[0]
        for q in ("Should we increase the Neem dose?", "What did the other patient get?", "What is the average Neem dose per prescription?"):
            r = self.choose(count["frame"], count["query"], question=q)
            self.assertEqual(r.status_code, 422, q)

    def test_the_same_reading_works_for_a_reworded_question_only_if_that_wording_offers_it(self):
        sum_reading = next(o for o in self.offered() if o["frame"] == "SUM_HERB")
        self.assertEqual(self.choose(sum_reading["frame"], sum_reading["query"], question="Neem count ethra?").status_code, 200)   # same herb, same readings
        self.assertEqual(self.choose(sum_reading["frame"], sum_reading["query"], question="Brahmi count?").status_code, 422)      # another herb

    def test_no_reading_can_be_chosen_when_the_question_is_not_a_clarification(self):
        for q in ("What was the first prescription?", "Should we increase the Neem dose?"):
            d = self.ask(q).json()
            self.assertEqual(d["options"], [])
            self.assertEqual(self.choose("LAST_RX", {"op": "last", "target": "prescription", "herb": None, "dosha": None, "last_n": None, "a": "previous", "b": "last"}, question=q).status_code, 422)


class MalformedRequestTests(ApiCase):
    def post(self, headers=None, **kw):
        return self.client.post(f"/patients/{self.p}/ask", headers={**self.doctor, **(headers or {})}, **kw)

    def test_bad_bodies_are_422_and_leak_nothing(self):
        for label, kw in {
            "missing question": {"json": {}}, "null question": {"json": {"question": None}}, "number": {"json": {"question": 5}}, "list": {"json": {"question": ["Neem"]}},
            "dict": {"json": {"question": {"q": 1}}}, "empty": {"json": {"question": ""}}, "whitespace": {"json": {"question": "   \n\t "}},
            "too long": {"json": {"question": "n" * 1001}}, "not json": {"content": b"question=Neem", "headers": {"Content-Type": "application/x-www-form-urlencoded"}},
            "broken json": {"content": b'{"question": ', "headers": {"Content-Type": "application/json"}}, "array body": {"json": ["Neem"]}, "string body": {"json": "Neem"},
            "null body": {"json": None},
        }.items():
            r = self.post(**kw)
            self.assertEqual(r.status_code, 422, (label, r.text))
            self.assertNotIn("Traceback", r.text)
            self.assertNotIn(DB_PATH, r.text)

    def test_long_but_in_bounds_and_odd_questions_are_handled_as_questions(self):
        for q in ("Neem " * 100, "\u0000Neem\x1b[2J count?", "ＮＥＥＭ ｃｏｕｎｔ？", "ZWSP​Neem count?", "SELECT * FROM patients; DROP TABLE patients;--", "' OR 1=1 --"):
            r = self.ask(q)
            self.assertEqual(r.status_code, 200, (q[:20], r.text))
            self.assertIn(r.json()["outcome"], ("answer", "clarify", "abstain"))
            self.assertNotEqual(r.json()["outcome"], "answer") if "SELECT" in q or "OR 1=1" in q else None

    def test_wrong_method_and_no_get(self):
        self.assertEqual(self.client.get(f"/patients/{self.p}/ask", headers=self.doctor).status_code, 405)
        self.assertEqual(self.client.put(f"/patients/{self.p}/ask", json={"question": "x"}, headers=self.doctor).status_code, 405)

    def test_unicode_malayalam_question_round_trips(self):
        d = self.ask("ഏറ്റവും പുതിയ കുറിപ്പടിയിൽ എന്താണുള്ളത്?").json()
        self.assertEqual(d["category"], "answer")


class ServiceAvailabilityTests(ApiCase):
    def test_a_planner_that_cannot_load_is_a_503_that_names_nothing_internal(self):
        with mock.patch.object(main_module, "_AI2_SERVICE", None), mock.patch("ai2.Ai2Service.default", side_effect=RuntimeError("secret path /x/y")):
            r = self.ask("What was the first prescription?")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers.get("X-Error-Code"), "MEMORY_SERVICE_UNAVAILABLE")
        self.assertNotIn("secret", r.text)
        self.assertNotIn("/x/y", r.text)

    def test_missing_patient_is_404_even_when_the_planner_is_down_and_auth_still_wins(self):
        with mock.patch.object(main_module, "_AI2_SERVICE", None), mock.patch("ai2.Ai2Service.default", side_effect=RuntimeError("down")):
            self.assertEqual(self.ask("x", patient=999999).status_code, 404)
            self.assertEqual(self.ask("x", headers={}).status_code, 401)
            self.assertEqual(self.ask("x", headers=self.admin).status_code, 403)

    def test_the_service_is_loaded_once_and_shared(self):
        a = main_module.get_ai2_service()
        self.assertIs(a, main_module.get_ai2_service())


class WriteSafetyTests(ApiCase):
    BATTERY = ["What was the first prescription?", "Neem count?", "How many prescriptions has she had?", "Should we increase the Neem dose?",
               "What did the other patient get?", "What is the average Neem dose per prescription?", "When did her most recent visit end?",
               "'; DROP TABLE prescriptions; --", "Give her 10 grams of Neem", "Confirm the prescription", "Close the visit", "Add Neem to the latest prescription"]

    def test_no_table_changes_across_every_kind_of_request(self):
        before = _db_fingerprint()
        for q in self.BATTERY:
            self.assertEqual(self.ask(q).status_code, 200, q)
        opt = self.ask("Neem count?").json()["options"]
        for o in opt:
            self.assertEqual(self.ask("Neem count?", choice={"frame": o["frame"], "query": o["query"]}).status_code, 200)
        self.assertEqual(self.ask("x", choice={"frame": "COUNT_HERB", "query": {"op": "list"}}).status_code, 422)
        self.assertEqual(self.ask("x", patient=999999).status_code, 404)
        self.assertEqual(self.ask("x", patient=self.p_b).status_code, 404)
        self.assertEqual(self.ask("x", headers=self.admin).status_code, 403)
        self.assertEqual(self.ask("x", headers={}).status_code, 401)
        self.assertEqual(self.ask("").status_code, 422)
        self.assertEqual(_db_fingerprint(), before)

    def test_inventory_prescriptions_encounters_and_audit_are_untouched(self):
        conn = sqlite3.connect(DB_PATH)
        tables = ("prescriptions", "prescription_items", "inventory", "encounters", "audit_events", "prescription_previews", "idempotency_keys", "corrections", "patients")
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        stock = conn.execute("SELECT SUM(stock_grams) FROM inventory").fetchone()[0]
        for q in self.BATTERY:
            self.ask(q)
        after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        self.assertEqual(before, after)
        self.assertEqual(stock, conn.execute("SELECT SUM(stock_grams) FROM inventory").fetchone()[0])
        conn.close()

    def test_no_external_call_and_no_parser_call(self):
        def refuse(*a, **k):
            raise AssertionError("network access attempted")
        with mock.patch.object(socket.socket, "connect", refuse), mock.patch.object(main_module, "parse_clinical_notes", side_effect=AssertionError("parser called")):
            for q in self.BATTERY[:7]:
                self.assertEqual(self.ask(q).status_code, 200, q)

    def test_the_route_is_registered_once_as_a_post_only_read_endpoint(self):
        routes = [r for r in main_module.app.routes if getattr(r, "path", "") == "/patients/{patient_pk}/ask"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].methods, {"POST"})
        import inspect
        source = inspect.getsource(main_module.ask_patient_history)
        for forbidden in ("deduct_and_prescribe", "log_correction", "open_encounter", "close_encounter", "create_patient", "get_connection", "record_audit", "INSERT", "UPDATE"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
