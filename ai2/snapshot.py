"""
Read-only PatientSnapshot for AI-2 (deterministic; no model, no database access).

The snapshot is the ONLY patient data the pipeline sees. It is built from store-shaped records by
snapshot_from_records(); fetching those records is the application's job (see ai2.ports.PatientRecords and
ai2.adapters.clinic_stores, which reads through the existing store functions and is the only module that does).

Scope: clinic_id must come from the authenticated session of the application; a patient of another clinic is
indistinguishable from a missing one (PatientNotFound).

The snapshot deliberately carries NO display name, phone or clinic-patient ID: only the registry key and clinical
history, so nothing identifying can reach a later stage.

Unknown values: a prescription whose dosha or herbs the store reports as None keeps UNKNOWN (None) for that field.
Nothing is inferred, defaulted or parsed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

class PatientNotFound(LookupError):
    """No such patient in this clinic (same answer for another clinic's patient)."""


class SnapshotUnavailable(RuntimeError):
    """No consistent snapshot of the patient's record could be produced (the read failed, or the records contradict themselves, for example a
    prescription listed twice). Nothing partial is ever returned; the caller must treat the question as unanswerable right now."""


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
    require_unique_records(rx, enc)   # never silently deduplicated: a repeated record means the read was inconsistent
    return PatientSnapshot(clinic_id=clinic_id, patient_pk=patient_pk, prescriptions=tuple(rx), encounters=tuple(enc))


def require_unique_records(prescriptions, encounters) -> None:
    """Raises SnapshotUnavailable when a prescription id, encounter key or encounter reference appears more than once."""
    if len({p.prescription_id for p in prescriptions}) != len(prescriptions) or \
            len({e.encounter_pk for e in encounters}) != len(encounters) or len({e.encounter_ref for e in encounters}) != len(encounters):
        raise SnapshotUnavailable("the patient record contains a repeated prescription or encounter")
