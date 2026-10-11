"""
AI-3 synthetic patients and the reference salience policy (evaluation and training only; the service never imports this module).

SYNTHETIC DATA ONLY. NOT CLINICAL DATA, NOT CLINICAL VALIDATION. The "gold" salience labels come from REFERENCE_POLICY below, a written
rule set authored by the same developer who built the ranker, on 2026-10-10, before any model was trained or evaluated. It is not a
clinician's judgement of what a doctor needs: results measure how well a learned ranker reproduces and generalises that policy, and how
a fixed template compares with it. Nothing here supports a claim about clinical usefulness.
"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import date, datetime, timedelta

from ai2.snapshot import Encounter, PatientSnapshot, Prescription
from ai2.vocabulary import HERB_ALIASES
from ai3.brief import verified_facts
from ai3.facts import KINDS, Fact
from ai3.salience import context, features

HERB_POOL = tuple(sorted(set(HERB_ALIASES.values())))
DOSHAS = ("Vata", "Pitta", "Kapha", "Vata-Pitta", "Pitta-Kapha", "Vata-Kapha")
CRITICAL = ("dosha_changed", "open_visit", "unknown_data_last")      # facts a brief must not omit when present (see critical_present)


def reference_policy(kind: str, ctx: dict[str, float]) -> bool:
    """REFERENCE_POLICY v1: would a brief for this patient be expected to include a fact of this kind? Thresholds are in whole units
    (prescriptions, days) recovered from the scaled context."""
    n_rx = round(ctx["n_rx"] * 10)
    days_rx = round(ctx["days_since_rx"] * 365)
    days_visit = round(ctx["days_since_visit"] * 365)
    if kind in ("last_prescription", "dosha_changed", "open_visit"):
        return True
    if kind == "unknown_data":
        return ctx["last_unknown"] == 1.0 or (ctx["any_unknown"] == 1.0 and n_rx <= 3)
    if kind == "herbs_changed":
        return days_rx <= 120
    if kind == "no_change":
        return n_rx >= 3 and days_rx <= 120
    if kind == "last_visit":
        return days_visit <= 60
    if kind == "count_prescriptions":
        return n_rx >= 6
    return False                                                    # first_prescription, count_visits


def critical_present(facts: list[Fact], ctx: dict[str, float]) -> set[str]:
    """Which critical facts exist for this patient: a dosha change, an open visit, unknown data on the latest prescription."""
    out = set()
    kinds = {f.kind for f in facts}
    if "dosha_changed" in kinds:
        out.add("dosha_changed")
    if "open_visit" in kinds:
        out.add("open_visit")
    if "unknown_data" in kinds and ctx["last_unknown"] == 1.0:
        out.add("unknown_data_last")
    return out


def critical_selected(selected_kinds: set[str], present: set[str]) -> set[str]:
    return {c for c in present if (c == "unknown_data_last" and "unknown_data" in selected_kinds) or c in selected_kinds}


def _ts(d: date, hour: int = 9) -> str:
    return datetime(d.year, d.month, d.day, hour).strftime("%Y-%m-%d %H:%M:%S")


def generate_case(rng: random.Random, index: int) -> tuple[PatientSnapshot, str]:
    n_rx = rng.choices([0, 1, 2, 3, 4, 5, 6, 8], weights=[5, 15, 20, 20, 15, 10, 8, 7])[0]
    start = date(2024, 1, 1) + timedelta(days=rng.randint(0, 800))
    rx, herbs, dosha, day = [], None, rng.choice(DOSHAS), start
    for i in range(n_rx):
        if i > 0:
            day = day + timedelta(days=rng.randint(14, 200))
        if herbs is None or rng.random() > 0.55:
            herbs = {h: rng.choice(range(5, 65, 5)) for h in rng.sample(HERB_POOL, rng.randint(1, 4))}
        else:
            herbs = dict(herbs)
            roll = rng.random()
            if roll < 0.4 and len(herbs) < 6:
                herbs[rng.choice([h for h in HERB_POOL if h not in herbs])] = rng.choice(range(5, 65, 5))
            elif roll < 0.7 and len(herbs) > 1:
                herbs.pop(rng.choice(sorted(herbs)))
            else:
                herbs[rng.choice(sorted(herbs))] = rng.choice(range(5, 65, 5))
        if rng.random() > 0.75:
            dosha = rng.choice(DOSHAS)
        shown_herbs = None if rng.random() < 0.08 else tuple(sorted(herbs.items()))
        shown_dosha = None if rng.random() < 0.08 else dosha
        rx.append(Prescription(prescription_id=1000 * (index + 1) + i + 1, created_at=_ts(day), status="CONFIRMED", dosha=shown_dosha, herbs=shown_herbs))
    last_event = day if n_rx else start
    enc = []
    n_enc = rng.choice([0, 1, 2, 3, 4, 6])
    for j in range(n_enc):
        d = start + timedelta(days=rng.randint(0, max((last_event - start).days, 1) + 30))
        enc.append((d, j))
    enc.sort()
    encounters = []
    for k, (d, j) in enumerate(enc):
        is_last_open = k == len(enc) - 1 and rng.random() < 0.15
        closed = None if is_last_open else _ts(d, 11)
        encounters.append(Encounter(encounter_pk=1000 * (index + 1) + k + 1, encounter_ref=hashlib.sha256(f"{index}:{k}".encode()).hexdigest()[:16],
                                    status="OPEN" if is_last_open else "CLOSED", created_at=_ts(d, 9), closed_at=closed))
    newest = max([last_event] + [e[0] for e in enc])
    as_of = newest + timedelta(days=rng.randint(0, 300))
    snap = PatientSnapshot(clinic_id="synthetic", patient_pk=index + 1, prescriptions=tuple(rx), encounters=tuple(encounters))
    return snap, as_of.isoformat()


def generate_cases(seed: int, n: int) -> list[tuple[PatientSnapshot, str]]:
    rng = random.Random(seed)
    return [generate_case(rng, i) for i in range(n)]


def case_summary(case: tuple[PatientSnapshot, str]) -> dict:
    snap, as_of = case
    return {"as_of": as_of, "rx": [[p.prescription_id, p.created_at, p.dosha, None if p.herbs is None else [list(h) for h in p.herbs]] for p in snap.prescriptions],
            "enc": [[e.encounter_pk, e.created_at, e.closed_at] for e in snap.encounters]}


def fingerprint(cases) -> str:
    return hashlib.sha256(json.dumps([case_summary(c) for c in cases], sort_keys=True).encode()).hexdigest()


def labelled_rows(cases, noise: float = 0.0, seed: int = 0) -> list[tuple[dict, int]]:
    """Training rows: (features, reference label) for every verified fact of every case. `noise` flips each label with that
    probability (seeded), modelling annotator inconsistency; evaluation always uses the clean reference policy."""
    rng = random.Random(seed)
    rows = []
    for snap, as_of in cases:
        facts, _ = verified_facts(snap)
        ctx = context(snap, as_of, facts)
        for f in facts:
            label = int(reference_policy(f.kind, ctx))
            rows.append((features(f, ctx), 1 - label if noise and rng.random() < noise else label))
    return rows
