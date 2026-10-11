"""
Read-only PatientSnapshot for AI-2 (deterministic; no model).

build_snapshot(clinic_id, patient_pk) reads ONLY through the existing store functions:
    backend.patient_store.get_patient_for_clinic      (clinic-scoped patient lookup)
    backend.patient_store.get_prescription_history    (confirmed prescriptions, structured
                                                       data authoritative; legacy rows fall
                                                       back to the store's own reader)
    backend.encounter_store.list_encounters_for_patient
No SQL is issued here and nothing is written.

Scope: clinic_id must come from the authenticated session in any future integration;
a patient of another clinic is indistinguishable from a missing one (PatientNotFound).

The snapshot deliberately carries NO display name, phone or clinic-patient ID: only the
registry key and clinical history, so nothing identifying can reach a later planner.

Unknown values: a prescription whose dosha or herbs the store reports as None keeps
UNKNOWN (None) for that field. Nothing is inferred, defaulted or parsed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PAGE = 50  # the API's own maximum page size (PATIENT_HISTORY_MAX_LIMIT / ENCOUNTER_LIST_MAX_LIMIT)


class PatientNotFound(LookupError):
    """No such patient in this clinic (same answer for another clinic's patient)."""


@dataclass(frozen=True)
class Prescription:
    prescription_id: int
    created_at: str
    status: str
    dosha: Optional[str]                              # None = unknown (never inferred)
    herbs: Optional[tuple[tuple[str, int], ...]]      # ((canonical name, grams), ...) in line order; None = unknown

    @property
    def label(self) -> str:
        return f"rx:{self.prescription_id}"

    def grams_of(self, herb: str) -> Optional[int]:
        """Grams of `herb`, 0 if the (known) prescription does not contain it; None if herbs are unknown."""
        if self.herbs is None:
            return None
        return sum(g for name, g in self.herbs if name == herb)


@dataclass(frozen=True)
class Encounter:
    encounter_pk: int
    encounter_ref: str
    status: str
    created_at: str
    closed_at: Optional[str]

    @property
    def label(self) -> str:
        return f"enc:{self.encounter_ref}"


@dataclass(frozen=True)
class PatientSnapshot:
    clinic_id: str
    patient_pk: int
    prescriptions: tuple[Prescription, ...] = field(default_factory=tuple)   # oldest first
    encounters: tuple[Encounter, ...] = field(default_factory=tuple)         # oldest first

    def labels(self) -> frozenset[str]:
        return frozenset([p.label for p in self.prescriptions] + [e.label for e in self.encounters])

    def prescription(self, label: str) -> Optional[Prescription]:
        return next((p for p in self.prescriptions if p.label == label), None)


def _prescription_from_record(record: dict) -> Prescription:
    herbs = record.get("prescribed_herbs")
    return Prescription(
        prescription_id=int(record["prescription_id"]),
        created_at=str(record["created_at"]),
        status=str(record["status"]),
        dosha=record.get("dosha_imbalance"),
        herbs=None if herbs is None else tuple((str(h["name"]), int(h["grams"])) for h in herbs),
    )


def _encounter_from_record(record: dict) -> Encounter:
    return Encounter(encounter_pk=int(record["encounter_pk"]), encounter_ref=str(record["encounter_ref"]),
                     status=str(record["status"]), created_at=str(record["created_at"]),
                     closed_at=record.get("closed_at"))


def snapshot_from_records(clinic_id: str, patient_pk: int, prescription_records, encounter_records) -> PatientSnapshot:
    """Build from store-shaped records (any order). Chronological order: created_at, then id."""
    rx = sorted((_prescription_from_record(r) for r in prescription_records),
                key=lambda p: (p.created_at, p.prescription_id))
    enc = sorted((_encounter_from_record(r) for r in encounter_records),
                 key=lambda e: (e.created_at, e.encounter_pk))
    return PatientSnapshot(clinic_id=clinic_id, patient_pk=patient_pk, prescriptions=tuple(rx), encounters=tuple(enc))


def build_snapshot(clinic_id: str, patient_pk: int) -> PatientSnapshot:
    from backend.encounter_store import list_encounters_for_patient
    from backend.patient_store import get_patient_for_clinic, get_prescription_history

    patient = get_patient_for_clinic(patient_pk, clinic_id)
    if patient is None:
        raise PatientNotFound(f"patient {patient_pk} not found")
    rx_records, enc_records, offset = [], [], 0
    while True:
        page, more = get_prescription_history(clinic_id, patient["patient_pk"], PAGE, offset)
        rx_records += page
        offset += PAGE
        if not more:
            break
    offset = 0
    while True:
        page, more = list_encounters_for_patient(clinic_id, patient["patient_pk"], PAGE, offset)
        enc_records += page
        offset += PAGE
        if not more:
            break
    return snapshot_from_records(clinic_id, patient["patient_pk"], rx_records, enc_records)
