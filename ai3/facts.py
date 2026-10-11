"""
AI-3 candidate facts: deterministic, verified statements about one patient's recorded history.

Every fact is built from an AI-2 closed-vocabulary query executed on the read-only PatientSnapshot (ai2.answer.render), so its
claim carries the executor's value, qualifier and cited records. A fact is KEPT only if
  (1) the AI-2 verifier re-executes the claim on the snapshot and finds it supported, and
  (2) every number, date and herb name in the fact's displayed sentence appears in the claim's own value or evidence.
Nothing is computed that the executor does not report (no day counts, no trends, no inference about causes or doses).
Unknown values stay unknown: a prescription whose herbs or dosha are not recorded is reported as such, never filled in.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ai2.answer import render
from ai2.executor import Query
from ai2.snapshot import PatientSnapshot
from ai2.verifier import verify_claim
from ai2.vocabulary import HERB_ALIASES

KINDS = ("last_prescription", "open_visit", "last_visit", "dosha_changed", "herbs_changed", "unknown_data", "no_change",
         "count_prescriptions", "count_visits", "first_prescription")
NUMBER = re.compile(r"\d+")
HERBS = frozenset(HERB_ALIASES.values())


@dataclass(frozen=True)
class Fact:
    kind: str
    text: str
    claim: dict
    labels: tuple          # the records the fact cites, e.g. ("rx:7", "enc:ab12…")
    exact: bool            # False when the claim is limited by unknown or partial records


def _day(ts: Optional[str]) -> str:
    return (ts or "")[:10]


def _rx_id(label: str) -> str:
    return "#" + label.split(":", 1)[1]


def _herbs_text(herbs) -> str:
    if herbs is None:
        return "herbs not recorded"
    return ", ".join(f"{n} {g} g" for n, g in herbs) if herbs else "no herbs"


def _plural(n: int, one: str) -> str:
    return f"{n} {one}" + ("" if n == 1 else "s")


def _claim(snapshot: PatientSnapshot, **query) -> Optional[dict]:
    claim = render(snapshot, Query(**query))
    return claim if claim["status"] != "not_recorded" and claim["sources"] else None


def _fact(kind: str, text: str, claim: dict) -> Fact:
    return Fact(kind=kind, text=text, claim=claim, labels=tuple(claim["sources"]), exact=claim["qualifier"] == "exact")


def candidate_facts(snapshot: PatientSnapshot) -> list[Fact]:
    """All facts that can be stated about this snapshot, before verification and before any selection."""
    facts: list[Fact] = []
    rx = snapshot.prescriptions

    last = _claim(snapshot, op="last", target="prescription")
    if last:
        v = last["value"]
        dosha = v["dosha"] if v["dosha"] is not None else "not recorded"
        facts.append(_fact("last_prescription", f"Latest prescription ({_rx_id(v['prescription'])}, {_day(v['created_at'])}): dosha {dosha}; {_herbs_text(v['herbs'])}.", last))

    if len(rx) >= 2:
        first = _claim(snapshot, op="first", target="prescription")
        if first:
            v = first["value"]
            facts.append(_fact("first_prescription", f"First recorded prescription: {_rx_id(v['prescription'])} on {_day(v['created_at'])}.", first))
        diff = _claim(snapshot, op="diff", target="prescription")
        if diff:
            v = diff["value"]
            since = f"since {_rx_id(v['a'])}"
            changes = []
            if v["added"]:
                changes.append("added " + ", ".join(f"{n} {g} g" for n, g in v["added"]))
            if v["removed"]:
                changes.append("removed " + ", ".join(f"{n} {g} g" for n, g in v["removed"]))
            if v["changed"]:
                changes.append("changed " + ", ".join(f"{n} {a} g to {b} g" for n, a, b in v["changed"]))
            if changes:
                facts.append(_fact("herbs_changed", f"Herbs {since}: " + "; ".join(changes) + ".", diff))
            if v["dosha_changed"] is True:
                facts.append(_fact("dosha_changed", f"Dosha changed from {v['dosha_a']} to {v['dosha_b']} {since}.", diff))
            if v["added"] == [] and v["removed"] == [] and v["changed"] == [] and v["dosha_changed"] is False:
                facts.append(_fact("no_change", f"No change in herbs or dosha {since} ({_rx_id(v['a'])} to {_rx_id(v['b'])}).", diff))

    count = _claim(snapshot, op="count", target="prescription")
    if count and count["value"] >= 1:
        facts.append(_fact("count_prescriptions", f"{_plural(count['value'], 'prescription')} on record.", count))

    enc_last = _claim(snapshot, op="last", target="encounter")
    if enc_last:
        v = enc_last["value"]
        if v["closed_at"] is None:
            facts.append(_fact("open_visit", f"A visit opened {_day(v['opened_at'])} is still open.", enc_last))
        else:
            facts.append(_fact("last_visit", f"Latest visit opened {_day(v['opened_at'])} and closed {_day(v['closed_at'])}.", enc_last))
    enc_count = _claim(snapshot, op="count", target="encounter")
    if enc_count and enc_count["value"] >= 1:
        facts.append(_fact("count_visits", f"{_plural(enc_count['value'], 'visit')} on record.", enc_count))

    unknown_herbs = [p for p in rx if p.herbs is None]
    unknown_dosha = [p for p in rx if p.dosha is None]
    for field_name, rows, target in (("Herbs", unknown_herbs, "herb"), ("Dosha", unknown_dosha, "dosha")):
        if rows:
            claim = _claim(snapshot, op="list", target=target)
            if claim:
                ids = ", ".join(_rx_id(p.label) for p in rows)
                facts.append(_fact("unknown_data", f"{field_name} are not recorded on: {ids}." if target == "herb" else f"Dosha is not recorded on: {ids}.", claim))
    return facts


def _support_text(claim: dict) -> str:
    return json.dumps([claim["value"], claim["sources"], claim["query"]], sort_keys=True, default=str)


def fact_supported(snapshot: PatientSnapshot, fact: Fact) -> tuple[bool, str]:
    """(True, '') when the fact may be shown. Re-executes the claim (AI-2 verifier) and checks the displayed sentence against it."""
    verdict = verify_claim(snapshot, fact.claim)
    if verdict["verdict"] != "verified":
        return False, "claim rejected by the verifier: " + ",".join(v["code"] for v in verdict["violations"])
    support = _support_text(fact.claim)
    # evidence the sentence may rest on: the claim's value and the cited records of THIS snapshot
    for label in fact.labels:
        p = snapshot.prescription(label)
        if p is not None:
            support += json.dumps([p.created_at, p.dosha, list(p.herbs) if p.herbs is not None else None, p.prescription_id])
        for e in snapshot.encounters:
            if e.label == label:
                support += json.dumps([e.created_at, e.closed_at])
    allowed = set(NUMBER.findall(support))
    for number in NUMBER.findall(fact.text):
        if number not in allowed:
            return False, f"number {number} is not in the claim's value or evidence"
    lowered = support.lower()
    for herb in HERBS:
        if re.search(rf"\b{re.escape(herb.lower())}\b", fact.text.lower()) and herb.lower() not in lowered:
            return False, f"herb {herb} is not in the claim's value or evidence"
    return True, ""


def days_between(later: str, earlier: Optional[str]) -> Optional[int]:
    """Whole days from `earlier` (a stored timestamp) to `later` (a date); None if earlier is missing. Used only as a ranking feature."""
    if not earlier:
        return None
    return (date.fromisoformat(later) - datetime.strptime(earlier[:10], "%Y-%m-%d").date()).days
