"""
AI-2 snapshot consistency: the patient lookup, every prescription page (with its items) and every encounter page are read as ONE point in time.

Original defect: ai2.adapters.clinic_stores paged prescriptions and encounters with OFFSET over a newest-first order, each page on its own connection.
A prescription confirmed between two page fetches pushed every older row down one position, so the last row of page 1 came back again on page 2
and the new row was missing; AI-2 then counted, summed and listed the duplicate, and the verifier -- which re-executes on the same snapshot -- marked
the wrong answer verified.

Every test here uses SYNTHETIC data in the isolated sandbox database. The "other process" that confirms a prescription is a separate raw sqlite3
connection that inserts and COMMITS from inside a hook that fires deterministically after the first page has been returned. Expected values are
computed by the test from the rows it seeded itself, never read back through the code under test.

Run only through an isolated sandbox (tests/conftest.py refuses otherwise).
"""
import hashlib
import os
import secrets
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.main as main_module  # noqa: E402
import backend.patient_store as patient_store  # noqa: E402
import db.database as database  # noqa: E402
from ai2 import Ai2Service, SnapshotUnavailable  # noqa: E402
from ai2.adapters.clinic_stores import PAGE, ClinicStoreRecords  # noqa: E402
from ai2.snapshot import Encounter, PatientSnapshot, Prescription, snapshot_from_records  # noqa: E402
from backend.auth_service import create_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

N_RX = 55          # > PAGE (50): the history spans two pages
N_ENC = 52         # > PAGE: the encounters span two pages
NEW_CREATED_AT = "2026-02-01 00:00:00"      # newer than every seeded row


def _raw():
    return sqlite3.connect(database.DB_PATH, timeout=5)


def _rx_created_at(i):
    return f"2026-01-01 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}"


def _rx_herbs(i):
    return [("Neem", 10 + i % 3), ("Brahmi", 5)]


def _add_rx(clinic, patient_pk, created_at, herbs, dosha="Vata"):
    """A confirmed prescription with its items, committed on its own connection (what another worker's confirmation looks like to a reader)."""
    c = _raw()
    try:
        rid = c.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, created_at, patient_pk, dosha_imbalance) VALUES (?,?,?,?,?,?,?)",
                        (clinic, "synthetic", f"Patient: synthetic | Dosha: {dosha}", "CONFIRMED", created_at, patient_pk, dosha)).lastrowid
        for line, (name, grams) in enumerate(herbs, 1):
            c.execute("INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) VALUES (?,?,?,?,?)", (rid, clinic, line, name, grams))
        c.commit()
        return rid
    finally:
        c.close()


def _add_enc(clinic, patient_pk, user_id, created_at):
    c = _raw()
    try:
        ref = secrets.token_hex(8)
        pk = c.execute("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at, encounter_ref) VALUES (?,?,?,?,?,?,?)",
                       (clinic, patient_pk, user_id, "CLOSED", created_at, created_at, ref)).lastrowid
        c.commit()
        return pk, ref
    finally:
        c.close()


def _fingerprint(skip=("sessions",)):
    c = _raw()
    try:
        h = hashlib.sha256()
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            if name in skip:
                continue
            h.update(name.encode() + repr(c.execute(f'SELECT rowid, * FROM "{name}" ORDER BY rowid').fetchall()).encode())
        return h.hexdigest()
    finally:
        c.close()


class Interleave:
    """Runs `action` exactly once, deterministically, around the store reads of `patient_pk`:
    which="rx"  -> right AFTER the first prescription page has been returned (the original defect's window)
    which="enc" -> right AFTER the first encounter page has been returned
    which="before_enc" -> right BEFORE the first encounter fetch (between the prescription reads and the encounter reads)
    `committed` is True once the action's writes were committed WHILE the snapshot read was still open."""

    def __init__(self, patient_pk, which, action):
        self.pk, self.which, self.action, self.committed, self._calls = patient_pk, which, action, False, 0
        self._orig_rx, self._orig_enc = patient_store.get_prescription_history, None
        import backend.encounter_store as encounter_store
        self._orig_enc = encounter_store.list_encounters_for_patient
        self._enc_mod = encounter_store

    def _fire(self):
        if not self.committed:
            self.action()
            self.committed = True

    def __enter__(self):
        def rx(clinic_id, patient_pk, limit, offset, conn=None):
            result = self._orig_rx(clinic_id, patient_pk, limit, offset, conn=conn)
            if self.which == "rx" and patient_pk == self.pk:
                self._fire()
            return result

        def enc(clinic_id, patient_pk, limit, offset, conn=None):
            if self.which == "before_enc" and patient_pk == self.pk:
                self._fire()
            result = self._orig_enc(clinic_id, patient_pk, limit, offset, conn=conn)
            if self.which == "enc" and patient_pk == self.pk:
                self._fire()
            return result

        self._patches = [mock.patch("backend.patient_store.get_prescription_history", rx), mock.patch("backend.encounter_store.list_encounters_for_patient", enc)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class SnapshotCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = TestClient(main_module.app, raise_server_exceptions=False)
        cls.client = cls.cm.__enter__()
        cls.clinic = f"snapc_a_{secrets.token_hex(4)}"
        cls.other_clinic = f"snapc_b_{secrets.token_hex(4)}"
        c = _raw()
        try:
            for clinic in (cls.clinic, cls.other_clinic):
                c.executemany("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) VALUES (?,?,?,?,?,?)",
                              [(clinic, h, "test botanical name", 100000, "TEST-BATCH", 90) for h in ("Neem", "Brahmi")])
            c.commit()
        finally:
            c.close()
        cls.headers = cls.login(cls.clinic)
        cls.headers_b = cls.login(cls.other_clinic)
        c = _raw()
        try:
            cls.user_id = c.execute("SELECT user_id FROM users WHERE clinic_id = ? LIMIT 1", (cls.clinic,)).fetchone()[0]
            cls.journal_mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            c.close()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)

    @classmethod
    def login(cls, clinic):
        user, pw = f"snapc_{secrets.token_hex(6)}", "snapshot-test-password-not-a-real-credential"
        create_user(user, pw, clinic_id=clinic, role="DOCTOR")
        resp = cls.client.post("/auth/login", json={"username": user, "password": pw})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    def patient(self, clinic=None, headers=None):
        resp = self.client.post("/patients", json={"clinic_patient_id": f"snapc-{secrets.token_hex(4)}", "display_name": "Snapshot Synthetic"},
                                headers=headers or self.headers)
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["patient_pk"]

    def seed_rx(self, pk, n=N_RX):
        """n confirmed prescriptions, oldest first; returns [(id, herbs)] -- the test's own record of what exists."""
        return [(_add_rx(self.clinic, pk, _rx_created_at(i), _rx_herbs(i)), _rx_herbs(i)) for i in range(n)]

    def seed_enc(self, pk, n=N_ENC):
        return [_add_enc(self.clinic, pk, self.user_id, f"2026-01-01 {i // 60:02d}:{i % 60:02d}:00") for i in range(n)]

    def ask(self, pk, question, headers=None):
        return self.client.post(f"/patients/{pk}/ask", json={"question": question}, headers=headers or self.headers)

    def new_rx_action(self, pk):
        return lambda: setattr(self, "_new_rx", _add_rx(self.clinic, pk, NEW_CREATED_AT, [("Neem", 7), ("Brahmi", 5)]))

    def new_enc_action(self, pk):
        return lambda: setattr(self, "_new_enc", _add_enc(self.clinic, pk, self.user_id, NEW_CREATED_AT))


class PrescriptionPagingTests(SnapshotCase):
    def test_setup_is_meaningful(self):
        self.assertEqual(self.journal_mode.lower(), "wal", "the scenario assumes WAL: a reader must not block the other process's commit")
        self.assertGreater(N_RX, PAGE)
        self.assertGreater(N_ENC, PAGE)

    def test_prescription_committed_between_pages_is_excluded_without_duplicate_or_omission(self):
        pk = self.patient()
        seeded = self.seed_rx(pk)
        with Interleave(pk, "rx", self.new_rx_action(pk)) as hook:
            snap = ClinicStoreRecords().load_snapshot(self.clinic, pk)
        self.assertTrue(hook.committed, "the other process's commit must succeed while the snapshot read is open")
        ids = [p.prescription_id for p in snap.prescriptions]
        self.assertEqual(ids, [rid for rid, _ in seeded], "exactly the prescriptions that existed at the first read, oldest first")
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(self._new_rx, ids)
        self.assertEqual([[(n, g) for n, g in p.herbs] for p in snap.prescriptions], [[tuple(h) for h in herbs] for _, herbs in seeded])

    def test_a_later_ask_sees_the_committed_prescription(self):
        pk = self.patient()
        self.seed_rx(pk)
        with Interleave(pk, "rx", self.new_rx_action(pk)):
            ClinicStoreRecords().load_snapshot(self.clinic, pk)
        later = ClinicStoreRecords().load_snapshot(self.clinic, pk)
        self.assertEqual(len(later.prescriptions), N_RX + 1)
        self.assertIn(self._new_rx, [p.prescription_id for p in later.prescriptions])

    def test_http_answers_equal_the_independent_oracle_under_the_original_interleaving(self):
        questions = {"count_rx": "How many prescriptions are recorded?", "count_neem": "How many prescriptions contain Neem?",
                     "sum_neem": "What is the total grams of Neem?", "list_rx": "List all prescriptions"}
        for key, question in questions.items():
            with self.subTest(question=key):
                pk = self.patient()
                seeded = self.seed_rx(pk)
                ids = [rid for rid, _ in seeded]
                expected_neem_grams = sum(g for _, herbs in seeded for name, g in herbs if name == "Neem")
                with Interleave(pk, "rx", self.new_rx_action(pk)) as hook:
                    resp = self.ask(pk, question)
                self.assertTrue(hook.committed)
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertEqual(body["category"], "answer")
                self.assertTrue(body["verified"])
                expected = {"count_rx": N_RX, "count_neem": N_RX, "sum_neem": expected_neem_grams, "list_rx": [f"rx:{i}" for i in ids]}[key]
                self.assertEqual(body["value"], expected)
                evidence_ids = [e["prescription_id"] for e in body["evidence"]]
                self.assertEqual(sorted(evidence_ids), sorted(ids), "evidence is exactly the set of prescriptions the answer was computed from")
                self.assertEqual(len(evidence_ids), len(set(evidence_ids)))
                self.assertNotIn(self._new_rx, evidence_ids)
                # and the very next question, asked after the commit, includes the new prescription
                after = self.ask(pk, questions["count_rx"]).json()
                self.assertEqual(after["value"], N_RX + 1)

    def test_default_store_calls_without_a_connection_are_unchanged(self):
        pk = self.patient()
        self.seed_rx(pk, 7)
        plain, more = patient_store.get_prescription_history(self.clinic, pk, 4, 0)
        with patient_store.read_snapshot() as conn:
            shared, more_shared = patient_store.get_prescription_history(self.clinic, pk, 4, 0, conn=conn)
        self.assertEqual((plain, more), (shared, more_shared))
        self.assertTrue(more)
        self.assertEqual(patient_store.get_patient_for_clinic(pk, self.clinic)["patient_pk"], pk)
        self.assertIsNone(patient_store.get_patient_for_clinic(pk, self.other_clinic))


class EncounterPagingTests(SnapshotCase):
    def test_encounter_committed_between_pages_is_excluded_without_duplicate_or_omission(self):
        pk = self.patient()
        seeded = self.seed_enc(pk)
        with Interleave(pk, "enc", self.new_enc_action(pk)) as hook:
            snap = ClinicStoreRecords().load_snapshot(self.clinic, pk)
        self.assertTrue(hook.committed)
        refs = [e.encounter_ref for e in snap.encounters]
        self.assertEqual(refs, [ref for _, ref in seeded])
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual([e.encounter_pk for e in snap.encounters], [pk_ for pk_, _ in seeded])
        self.assertNotIn(self._new_enc[1], refs)

    def test_http_visit_answers_equal_the_oracle(self):
        pk = self.patient()
        seeded = self.seed_enc(pk)
        with Interleave(pk, "enc", self.new_enc_action(pk)) as hook:
            counted = self.ask(pk, "How many visits are recorded?")
        self.assertTrue(hook.committed)
        self.assertEqual(counted.status_code, 200, counted.text)
        body = counted.json()
        self.assertEqual((body["category"], body["value"], body["verified"]), ("answer", N_ENC, True))
        self.assertEqual(sorted(e["encounter_ref"] for e in body["evidence"]), sorted(ref for _, ref in seeded))
        self.assertEqual(self.ask(pk, "How many visits are recorded?").json()["value"], N_ENC + 1)


class CrossCollectionTests(SnapshotCase):
    def test_prescriptions_and_encounters_are_the_same_instant(self):
        pk = self.patient()
        rx = self.seed_rx(pk)
        enc = self.seed_enc(pk)

        def both():
            self.new_rx_action(pk)()
            self.new_enc_action(pk)()
        with Interleave(pk, "before_enc", both) as hook:     # committed AFTER the prescription pages, BEFORE the encounter pages
            snap = ClinicStoreRecords().load_snapshot(self.clinic, pk)
        self.assertTrue(hook.committed)
        self.assertEqual([p.prescription_id for p in snap.prescriptions], [rid for rid, _ in rx])
        self.assertEqual([e.encounter_ref for e in snap.encounters], [ref for _, ref in enc])
        self.assertNotIn(self._new_rx, [p.prescription_id for p in snap.prescriptions])
        self.assertNotIn(self._new_enc[1], [e.encounter_ref for e in snap.encounters])

    def test_control_the_evaluation_builder_still_shows_both_original_defects(self):
        """Documents that this harness really reproduces the original interleaving. memory/snapshot.py is the frozen evaluation builder and is
        deliberately not changed; the production adapter no longer shares its behaviour."""
        from memory.snapshot import build_snapshot as legacy_build
        pk = self.patient()
        self.seed_rx(pk)
        with Interleave(pk, "rx", self.new_rx_action(pk)):
            legacy = legacy_build(self.clinic, pk)
        ids = [p.prescription_id for p in legacy.prescriptions]
        self.assertGreater(len(ids), len(set(ids)), "the paged builder returns a prescription twice")
        self.assertNotIn(self._new_rx, ids)

        pk2 = self.patient()
        self.seed_rx(pk2, 3)
        self.seed_enc(pk2, 2)

        def both():
            self.new_rx_action(pk2)()
            self.new_enc_action(pk2)()
        with Interleave(pk2, "before_enc", both):
            mixed = legacy_build(self.clinic, pk2)
        self.assertNotIn(self._new_rx, [p.prescription_id for p in mixed.prescriptions])
        self.assertIn(self._new_enc[0], [e.encounter_pk for e in mixed.encounters], "the old builder mixes two instants")


class ReadTransactionTests(SnapshotCase):
    def test_one_connection_one_transaction_and_the_first_statement_is_the_patient_lookup(self):
        pk = self.patient()
        self.seed_rx(pk, 3)
        opened, trace = [], []
        real = database.get_connection

        def counting():
            conn = real()
            conn.set_trace_callback(lambda sql: trace.append(" ".join(sql.split())))
            opened.append(conn)
            return conn
        with mock.patch("backend.patient_store.get_connection", counting), mock.patch("backend.encounter_store.get_connection", counting), \
                mock.patch("db.database.get_connection", counting):
            ClinicStoreRecords().load_snapshot(self.clinic, pk)
        self.assertEqual(len(opened), 1, "no helper may open a second connection")
        begin = next(i for i, s in enumerate(trace) if s.upper() == "BEGIN")
        before = trace[:begin]
        self.assertTrue(all(s.upper().startswith("PRAGMA") for s in before), before)
        reads = [s for s in trace[begin + 1:] if s.upper().startswith(("SELECT", "WITH"))]
        self.assertIn("FROM patients", reads[0], "the patient lookup is the first read, so it fixes the snapshot")
        self.assertIn("FROM prescriptions", " ".join(reads))
        self.assertIn("FROM encounters", " ".join(reads))
        forbidden = [s for s in trace if s.upper().startswith(("INSERT", "UPDATE", "DELETE", "COMMIT", "CREATE", "DROP", "ALTER", "REPLACE"))]
        self.assertEqual(forbidden, [])
        self.assertTrue(any("query_only" in s for s in trace[:begin]))

    def test_the_connection_is_closed_after_success_and_after_failure(self):
        pk = self.patient()
        self.seed_rx(pk, 2)
        captured = []
        real = database.get_connection

        def capturing():
            conn = real()
            captured.append(conn)
            return conn
        with mock.patch("backend.patient_store.get_connection", capturing):
            ClinicStoreRecords().load_snapshot(self.clinic, pk)
            with self.assertRaises(Exception):
                ClinicStoreRecords().load_snapshot(self.clinic, 99999999 + pk)       # PatientNotFound
        self.assertEqual(len(captured), 2)
        for conn in captured:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_the_snapshot_connection_cannot_write(self):
        before = _fingerprint()
        with self.assertRaises(patient_store.SnapshotReadError):
            with patient_store.read_snapshot() as conn:
                conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) VALUES ('x','x','x',1,'x',1)")
        self.assertEqual(_fingerprint(), before)

    def test_asking_changes_no_table_but_the_session_touch(self):
        pk = self.patient()
        self.seed_rx(pk, 5)
        self.seed_enc(pk, 2)
        before = _fingerprint()
        for question in ("How many prescriptions are recorded?", "List all prescriptions", "When was the last visit?", "Should I stop Neem?"):
            self.assertEqual(self.ask(pk, question).status_code, 200)
        self.assertEqual(self.ask(pk, "x", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(_fingerprint(), before, "every table except sessions.last_seen_at is untouched")

    def test_authorization_and_clinic_scope_are_unchanged(self):
        pk = self.patient()
        self.seed_rx(pk, 2)
        self.assertEqual(self.client.post(f"/patients/{pk}/ask", json={"question": "How many prescriptions are recorded?"}).status_code, 401)
        foreign = self.ask(pk, "How many prescriptions are recorded?", headers=self.headers_b)
        missing = self.ask(99999999, "How many prescriptions are recorded?", headers=self.headers_b)
        self.assertEqual((foreign.status_code, foreign.json()), (404, {"detail": "Patient not found."}))
        self.assertEqual((missing.status_code, missing.json()), (foreign.status_code, foreign.json()))


class IntegrityGuardTests(SnapshotCase):
    RX = {"prescription_id": 1, "created_at": "2026-01-01 00:00:00", "status": "CONFIRMED", "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Neem", "grams": 5}]}
    ENC = {"encounter_pk": 1, "encounter_ref": "a" * 16, "status": "CLOSED", "created_at": "2026-01-01 00:00:00", "closed_at": "2026-01-01 01:00:00"}

    def test_a_repeated_record_is_refused_never_deduplicated(self):
        with self.assertRaises(SnapshotUnavailable):
            snapshot_from_records("c", 1, [self.RX, dict(self.RX)], [])
        with self.assertRaises(SnapshotUnavailable):
            snapshot_from_records("c", 1, [], [self.ENC, dict(self.ENC)])
        with self.assertRaises(SnapshotUnavailable):
            snapshot_from_records("c", 1, [], [self.ENC, {**self.ENC, "encounter_pk": 2}])        # same reference, different key
        ok = snapshot_from_records("c", 1, [self.RX, {**self.RX, "prescription_id": 2}], [self.ENC, {**self.ENC, "encounter_pk": 2, "encounter_ref": "b" * 16}])
        self.assertEqual((len(ok.prescriptions), len(ok.encounters)), (2, 2))

    def test_the_service_refuses_an_inconsistent_snapshot_instead_of_verifying_an_answer(self):
        rx = Prescription(1, "2026-01-01 00:00:00", "CONFIRMED", "Vata", (("Neem", 5),))
        bad = PatientSnapshot("c", 1, prescriptions=(rx, rx), encounters=())
        service = Ai2Service.default()
        with self.assertRaises(SnapshotUnavailable):
            service.ask(bad, "How many prescriptions are recorded?")
        with self.assertRaises(SnapshotUnavailable):
            service.choose(bad, "Neem count?", "COUNT_HERB", {"op": "count", "target": "herb", "herb": "Neem", "dosha": None, "last_n": None, "a": "previous", "b": "last"})
        enc = Encounter(1, "a" * 16, "CLOSED", "2026-01-01 00:00:00", "2026-01-01 01:00:00")
        with self.assertRaises(SnapshotUnavailable):
            service.ask(PatientSnapshot("c", 1, prescriptions=(), encounters=(enc, enc)), "How many visits are recorded?")
        good = PatientSnapshot("c", 1, prescriptions=(rx,), encounters=())
        self.assertEqual(service.ask(good, "How many prescriptions are recorded?").value, 1)

    def test_the_endpoint_answers_503_for_an_inconsistent_snapshot_and_returns_no_record(self):
        pk = self.patient()
        rx = Prescription(1, "2026-01-01 00:00:00", "CONFIRMED", "Vata", (("Neem", 5),))
        bad = PatientSnapshot(self.clinic, pk, prescriptions=(rx, rx), encounters=())
        with mock.patch.object(ClinicStoreRecords, "load_snapshot", return_value=bad):
            resp = self.ask(pk, "How many prescriptions are recorded?")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers.get("x-error-code"), "MEMORY_SERVICE_UNAVAILABLE")
        self.assertEqual(resp.json(), {"detail": "The question service is unavailable."})


class FailureHandlingTests(SnapshotCase):
    def assert_safe_503(self, resp):
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.headers.get("x-error-code"), "MEMORY_SERVICE_UNAVAILABLE")
        self.assertEqual(resp.json(), {"detail": "The question service is unavailable."})
        for leak in ("locked", "sqlite", "OperationalError", "disk", "traceback", "Traceback"):
            self.assertNotIn(leak, resp.text)

    def test_a_connection_failure_is_a_safe_503_and_the_next_ask_recovers(self):
        pk = self.patient()
        self.seed_rx(pk, 3)
        with mock.patch("backend.patient_store.get_connection", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(SnapshotUnavailable):
                ClinicStoreRecords().load_snapshot(self.clinic, pk)
            self.assert_safe_503(self.ask(pk, "How many prescriptions are recorded?"))
        ok = self.ask(pk, "How many prescriptions are recorded?")
        self.assertEqual((ok.status_code, ok.json()["value"]), (200, 3))

    def test_a_failure_on_the_second_page_returns_nothing_partial(self):
        pk = self.patient()
        self.seed_rx(pk)
        real, calls = patient_store.get_prescription_history, []

        def failing(clinic_id, patient_pk, limit, offset, conn=None):
            calls.append(offset)
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(clinic_id, patient_pk, limit, offset, conn=conn)
        with mock.patch("backend.patient_store.get_prescription_history", failing):
            with self.assertRaises(SnapshotUnavailable):
                ClinicStoreRecords().load_snapshot(self.clinic, pk)
            self.assertEqual(calls, [0, PAGE])
            calls.clear()                      # the HTTP request below must fail on ITS second page too
            resp = self.ask(pk, "How many prescriptions are recorded?")
            self.assertEqual(calls, [0, PAGE])
        self.assert_safe_503(resp)
        self.assertNotIn("evidence", resp.json())

    def test_a_failure_on_an_encounter_page_returns_nothing_partial(self):
        pk = self.patient()
        self.seed_rx(pk, 3)
        self.seed_enc(pk)
        import backend.encounter_store as encounter_store
        real, calls = encounter_store.list_encounters_for_patient, []

        def failing(clinic_id, patient_pk, limit, offset, conn=None):
            calls.append(offset)
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return real(clinic_id, patient_pk, limit, offset, conn=conn)
        with mock.patch("backend.encounter_store.list_encounters_for_patient", failing):
            self.assert_safe_503(self.ask(pk, "How many prescriptions are recorded?"))

    def test_a_failed_read_leaves_no_lock_behind(self):
        pk = self.patient()
        self.seed_rx(pk, 2)
        with mock.patch("backend.patient_store.get_prescription_history", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(SnapshotUnavailable):
                ClinicStoreRecords().load_snapshot(self.clinic, pk)
        c = sqlite3.connect(database.DB_PATH, timeout=1)
        try:
            busy, _, _ = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()   # busy=1 would mean a reader still holds a read snapshot
            self.assertEqual(busy, 0, "a failed read must not leave its read transaction open")
        finally:
            c.close()

    def test_an_unknown_patient_is_still_a_404_not_a_503(self):
        self.assertEqual(self.ask(99999999, "How many prescriptions are recorded?").status_code, 404)


if __name__ == "__main__":
    unittest.main()
