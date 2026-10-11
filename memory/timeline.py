"""
Deterministic chronological Timeline of a PatientSnapshot.

Events: ENCOUNTER_OPENED, PRESCRIPTION_CONFIRMED, ENCOUNTER_CLOSED, each with a stable
source label (rx:<prescription_id>, enc:<encounter_ref>). Order: timestamp, then a
fixed kind rank (opened < prescription < closed), then the record id.

Caveat, recorded on every event: timestamps have one-second resolution and prescriptions
and encounters live in separate tables, so events of DIFFERENT kinds in the same second
cannot be ordered against each other from the data. Those events are marked
`same_second=True`; within one kind the id order is exact.
"""
from __future__ import annotations

from dataclasses import dataclass

from memory.snapshot import PatientSnapshot

KIND_RANK = {"ENCOUNTER_OPENED": 0, "PRESCRIPTION_CONFIRMED": 1, "ENCOUNTER_CLOSED": 2}


@dataclass(frozen=True)
class TimelineEvent:
    at: str
    kind: str
    label: str
    record_id: int
    same_second: bool = False


def build_timeline(snapshot: PatientSnapshot) -> tuple[TimelineEvent, ...]:
    raw = [(e.created_at, "ENCOUNTER_OPENED", e.label, e.encounter_pk) for e in snapshot.encounters]
    raw += [(e.closed_at, "ENCOUNTER_CLOSED", e.label, e.encounter_pk) for e in snapshot.encounters if e.closed_at]
    raw += [(p.created_at, "PRESCRIPTION_CONFIRMED", p.label, p.prescription_id) for p in snapshot.prescriptions]
    raw.sort(key=lambda r: (r[0], KIND_RANK[r[1]], r[3]))
    kinds_at: dict[str, set] = {}
    for at, kind, _, _ in raw:
        kinds_at.setdefault(at, set()).add(kind)
    return tuple(TimelineEvent(at, kind, label, rid, same_second=len(kinds_at[at]) > 1)
                 for at, kind, label, rid in raw)
