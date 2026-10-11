"""
The one thing AI-2 needs from the application: a read-only PatientSnapshot for a patient of the caller's clinic.

The application (not AI-2) authenticates the caller, applies RBAC and chooses the clinic from the session; it then hands
AI-2 either a snapshot or a PatientRecords implementation bound to that decision. AI-2 never receives credentials, never
chooses a clinic, and has no write access: the Protocol has a single read method.
"""
from __future__ import annotations

from typing import Protocol

from ai2.snapshot import PatientSnapshot


class PatientRecords(Protocol):
    def load_snapshot(self, clinic_id: str, patient_pk: int) -> PatientSnapshot:
        """The snapshot of `patient_pk` inside `clinic_id`. Raises ai2.snapshot.PatientNotFound when there is no such patient
        in that clinic (a patient of another clinic is indistinguishable from a missing one)."""
        ...
