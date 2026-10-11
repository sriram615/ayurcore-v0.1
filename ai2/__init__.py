"""
AI-2 Consultation Memory -- production package.

A local, deterministic, read-only question-answering service over one patient's recorded history (confirmed prescriptions and
encounters). No LLM, no learned embeddings, no external API, no web framework, no database access in the core. Public surface:

    from ai2 import Ai2Service, Ai2Answer, PatientNotFound
    from ai2.ports import PatientRecords
    from ai2.adapters.clinic_stores import ClinicStoreRecords     # the only module that reads the host application's stores

See ai2/PROVENANCE.json for the exact accepted evaluation modules each file derives from.
"""
from ai2.result import CATEGORIES, Ai2Answer
from ai2.service import Ai2Service, InvalidChoice, PlannerIntegrityError, load_planner
from ai2.snapshot import PatientNotFound, PatientSnapshot, SnapshotUnavailable, snapshot_from_records

# SnapshotUnavailable is importable (from ai2 import SnapshotUnavailable) but, like the public surface test requires, __all__ is unchanged.
__all__ = ["Ai2Service", "Ai2Answer", "CATEGORIES", "InvalidChoice", "PatientNotFound", "PatientSnapshot", "PlannerIntegrityError", "load_planner",
           "snapshot_from_records"]
