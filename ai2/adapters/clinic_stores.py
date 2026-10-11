"""
Read-only adapter from the existing AyurCore store functions to ai2.ports.PatientRecords.

It reads ONLY through the application's own store functions (no SQL here, nothing duplicated, nothing written):
    backend.patient_store.get_patient_for_clinic      (clinic-scoped patient lookup)
    backend.patient_store.get_prescription_history    (confirmed prescriptions; structured data authoritative, legacy rows
                                                       fall back to the store's own reader)
    backend.encounter_store.list_encounters_for_patient
Derived from memory.snapshot.build_snapshot; the reads now share one read-only transaction (see build_snapshot). This is the only AI-2 module that imports backend
code other than the herb alias table (ai2.vocabulary), and it imports no web framework.
"""
from __future__ import annotations

from ai2.snapshot import PatientNotFound, PatientSnapshot, SnapshotUnavailable, snapshot_from_records

PAGE = 50  # the API's own maximum page size (PATIENT_HISTORY_MAX_LIMIT / ENCOUNTER_LIST_MAX_LIMIT)


def build_snapshot(clinic_id: str, patient_pk: int) -> PatientSnapshot:
    """One point-in-time snapshot: the patient lookup, every prescription page (with its items) and every encounter page are read on ONE
    connection inside ONE read-only transaction (backend.patient_store.read_snapshot), so a confirmation committed meanwhile is excluded
    from all of them or included in all of them -- never half. Any read failure, or a record that appears twice, raises SnapshotUnavailable."""
    from backend.encounter_store import list_encounters_for_patient
    from backend.patient_store import SnapshotReadError, get_patient_for_clinic, get_prescription_history, read_snapshot

    try:
        with read_snapshot() as conn:
            patient = get_patient_for_clinic(patient_pk, clinic_id, conn=conn)      # the FIRST statement: it fixes the read snapshot
            if patient is None:
                raise PatientNotFound(f"patient {patient_pk} not found")
            rx_records, enc_records, offset = [], [], 0
            while True:
                page, more = get_prescription_history(clinic_id, patient["patient_pk"], PAGE, offset, conn=conn)
                rx_records += page
                offset += PAGE
                if not more:
                    break
            offset = 0
            while True:
                page, more = list_encounters_for_patient(clinic_id, patient["patient_pk"], PAGE, offset, conn=conn)
                enc_records += page
                offset += PAGE
                if not more:
                    break
    except SnapshotReadError as exc:
        raise SnapshotUnavailable(str(exc)) from exc
    return snapshot_from_records(clinic_id, patient["patient_pk"], rx_records, enc_records)


class ClinicStoreRecords:
    """ai2.ports.PatientRecords over the existing clinic stores."""

    def load_snapshot(self, clinic_id: str, patient_pk: int) -> PatientSnapshot:
        return build_snapshot(clinic_id, patient_pk)
