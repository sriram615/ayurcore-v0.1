"""
Deterministic executor for the closed AI-2 query vocabulary (no model, no SQL).

    Query(op, target, herb=None, dosha=None, last_n=None, a="previous", b="last")

    op        target        meaning
    first     prescription  the earliest prescription
    last      prescription  the latest prescription
    first     herb (herb)   the earliest prescription containing `herb`, with its grams
    last      herb (herb)   the latest prescription containing `herb`, with its grams
    first     dosha         the dosha of the earliest prescription
    last      dosha         the dosha of the latest prescription
    first     encounter     the earliest encounter (opened at)
    last      encounter     the latest encounter
    count     prescription  number of prescriptions
    count     encounter     number of encounters
    count     herb (herb)   number of prescriptions containing `herb`
    count     dosha (dosha) number of prescriptions recorded with exactly `dosha`
    sum       herb (herb)   total grams of `herb`
    list      prescription  prescription labels, oldest first
    list      encounter     encounter labels, oldest first
    list      herb          distinct herbs, in order of first appearance
    list      dosha         recorded doshas, oldest first (one per prescription)
    diff      prescription  herbs added / removed / changed and dosha change, from a to b
    compare   herb (herb)   grams of `herb` in a and in b, and b - a
    compare   dosha         dosha in a and in b, and whether it changed

`last_n` restricts prescription-based queries to the latest n prescriptions.
Selectors a / b: "first", "last", "previous" (second latest) or a label "rx:<id>"; identical
selectors are malformed, and two selectors that resolve to the same record give not_recorded.

Every Result carries the evidence it was computed from (source label, field, value).
Status:
    ok            fully determined by recorded data
    not_recorded  nothing in the record answers it (e.g. no prescriptions; herb never
                  appears and no prescription has unknown herbs)
    incomplete    a lower bound / partial answer: some prescription in scope has unknown
                  herbs or dosha that could change it (value = what the KNOWN records give)
    unknown       the answer depends entirely on an unknown value
Unknown values are never inferred.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ai2.snapshot import PatientSnapshot, Prescription

OPS = ("first", "last", "count", "sum", "list", "diff", "compare")
TARGETS = ("prescription", "herb", "dosha", "encounter")
SUPPORTED = {
    ("first", "prescription"), ("last", "prescription"), ("first", "herb"), ("last", "herb"),
    ("first", "dosha"), ("last", "dosha"), ("first", "encounter"), ("last", "encounter"),
    ("count", "prescription"), ("count", "encounter"), ("count", "herb"), ("count", "dosha"),
    ("sum", "herb"),
    ("list", "prescription"), ("list", "encounter"), ("list", "herb"), ("list", "dosha"),
    ("diff", "prescription"), ("compare", "herb"), ("compare", "dosha"),
}
STATUSES = ("ok", "not_recorded", "incomplete", "unknown")


class QueryError(ValueError):
    """The query is outside the closed vocabulary or malformed (never answered by guessing)."""


@dataclass(frozen=True)
class Query:
    op: str
    target: str
    herb: Optional[str] = None
    dosha: Optional[str] = None
    last_n: Optional[int] = None
    a: str = "previous"
    b: str = "last"

    def to_dict(self) -> dict:
        return {"op": self.op, "target": self.target, "herb": self.herb, "dosha": self.dosha,
                "last_n": self.last_n, "a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d: dict) -> "Query":
        allowed = {"op", "target", "herb", "dosha", "last_n", "a", "b"}
        if not isinstance(d, dict) or set(d) - allowed:
            raise QueryError(f"query fields must be a subset of {sorted(allowed)}")
        return cls(**d)


@dataclass(frozen=True)
class Evidence:
    label: str
    field: str
    value: Any


@dataclass(frozen=True)
class Result:
    status: str
    value: Any
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)
    unknown_sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(e.label for e in self.evidence) | frozenset(self.unknown_sources)


def _validate(q: Query) -> None:
    if q.op not in OPS or q.target not in TARGETS or (q.op, q.target) not in SUPPORTED:
        raise QueryError(f"unsupported query {q.op!r} x {q.target!r}")
    needs_herb = q.target == "herb" and q.op in ("first", "last", "count", "sum", "compare")
    if needs_herb and not (isinstance(q.herb, str) and q.herb):
        raise QueryError(f"{q.op} {q.target} needs a herb")
    if not needs_herb and q.herb is not None:
        raise QueryError("herb is only allowed for herb queries that use it")
    if (q.op, q.target) == ("count", "dosha") and not (isinstance(q.dosha, str) and q.dosha):
        raise QueryError("count dosha needs a dosha")
    if (q.op, q.target) != ("count", "dosha") and q.dosha is not None:
        raise QueryError("dosha is only allowed for count dosha")
    if q.last_n is not None and (not isinstance(q.last_n, int) or isinstance(q.last_n, bool) or q.last_n < 1):
        raise QueryError("last_n must be a positive integer")
    if q.last_n is not None and q.target == "encounter":
        raise QueryError("last_n applies to prescription-based queries only")
    for sel in (q.a, q.b):
        if not (sel in ("first", "last", "previous") or (isinstance(sel, str) and sel.startswith("rx:"))):
            raise QueryError(f"bad selector {sel!r}")
    if q.op in ("diff", "compare") and q.a == q.b:
        raise QueryError("a and b must be different selectors")


def _scope(s: PatientSnapshot, q: Query) -> tuple[Prescription, ...]:
    return s.prescriptions[-q.last_n:] if q.last_n else s.prescriptions


def _select(s: PatientSnapshot, sel: str) -> Optional[Prescription]:
    rx = s.prescriptions
    if sel == "first":
        return rx[0] if rx else None
    if sel == "last":
        return rx[-1] if rx else None
    if sel == "previous":
        return rx[-2] if len(rx) >= 2 else None
    return s.prescription(sel)  # an rx label of THIS snapshot only; another patient's id is not found


def _rx_facts(p: Prescription) -> dict:
    return {"prescription": p.label, "created_at": p.created_at, "dosha": p.dosha,
            "herbs": None if p.herbs is None else [list(h) for h in p.herbs]}


def execute(s: PatientSnapshot, q: Query) -> Result:
    _validate(q)
    rx = _scope(s, q)
    op, t = q.op, q.target

    if t == "encounter":
        enc = s.encounters
        if op == "count":
            return Result("ok", len(enc), tuple(Evidence(e.label, "opened_at", e.created_at) for e in enc))
        if not enc:
            return Result("not_recorded", None)
        if op == "list":
            return Result("ok", [e.label for e in enc], tuple(Evidence(e.label, "opened_at", e.created_at) for e in enc))
        e = enc[0] if op == "first" else enc[-1]
        return Result("ok", {"encounter": e.label, "opened_at": e.created_at, "closed_at": e.closed_at},
                      (Evidence(e.label, "opened_at", e.created_at),))

    if t == "prescription" and op in ("first", "last", "count", "list"):
        if op == "count":
            return Result("ok", len(rx), tuple(Evidence(p.label, "created_at", p.created_at) for p in rx))
        if not rx:
            return Result("not_recorded", None)
        if op == "list":
            return Result("ok", [p.label for p in rx], tuple(Evidence(p.label, "created_at", p.created_at) for p in rx))
        p = rx[0] if op == "first" else rx[-1]
        status = "ok" if p.herbs is not None and p.dosha is not None else "incomplete"
        return Result(status, _rx_facts(p), (Evidence(p.label, "record", _rx_facts(p)),),
                      (p.label,) if status != "ok" else ())

    if t == "herb" and op in ("first", "last"):
        ordered = rx if op == "first" else tuple(reversed(rx))
        unknown_before = []
        for p in ordered:
            if p.herbs is None:
                unknown_before.append(p.label)
                continue
            grams = p.grams_of(q.herb)
            if grams:
                value = {"prescription": p.label, "created_at": p.created_at, "grams": grams}
                return Result("incomplete" if unknown_before else "ok", value,
                              (Evidence(p.label, f"herbs.{q.herb}.grams", grams),), tuple(unknown_before))
        if unknown_before:
            return Result("unknown", None, (), tuple(unknown_before))
        return Result("not_recorded", None)

    if t == "dosha" and op in ("first", "last"):
        if not rx:
            return Result("not_recorded", None)
        p = rx[0] if op == "first" else rx[-1]
        if p.dosha is None:
            return Result("unknown", None, (), (p.label,))
        return Result("ok", {"prescription": p.label, "dosha": p.dosha}, (Evidence(p.label, "dosha", p.dosha),))

    if (op, t) in (("count", "herb"), ("sum", "herb")):
        unknown = tuple(p.label for p in rx if p.herbs is None)
        hits = [(p, p.grams_of(q.herb)) for p in rx if p.herbs is not None and p.grams_of(q.herb)]
        value = len(hits) if op == "count" else sum(g for _, g in hits)
        evidence = tuple(Evidence(p.label, f"herbs.{q.herb}.grams", g) for p, g in hits)
        if not hits and not unknown:
            return Result("not_recorded" if op == "sum" else "ok", None if op == "sum" else 0, evidence)
        return Result("incomplete" if unknown else "ok", value, evidence, unknown)

    if (op, t) == ("count", "dosha"):
        unknown = tuple(p.label for p in rx if p.dosha is None)
        hits = [p for p in rx if p.dosha == q.dosha]
        return Result("incomplete" if unknown else "ok", len(hits),
                      tuple(Evidence(p.label, "dosha", p.dosha) for p in hits), unknown)

    if (op, t) == ("list", "herb"):
        if not rx:
            return Result("not_recorded", None)
        unknown = tuple(p.label for p in rx if p.herbs is None)
        seen, evidence = [], []
        for p in rx:
            for name, grams in (p.herbs or ()):
                if name not in seen:
                    seen.append(name)
                    evidence.append(Evidence(p.label, f"herbs.{name}.grams", grams))
        return Result("incomplete" if unknown else "ok", seen, tuple(evidence), unknown)

    if (op, t) == ("list", "dosha"):
        if not rx:
            return Result("not_recorded", None)
        unknown = tuple(p.label for p in rx if p.dosha is None)
        known = [p for p in rx if p.dosha is not None]
        return Result("incomplete" if unknown else "ok", [{"prescription": p.label, "dosha": p.dosha} for p in known],
                      tuple(Evidence(p.label, "dosha", p.dosha) for p in known), unknown)

    # diff / compare between two selected prescriptions
    pa, pb = _select(s, q.a), _select(s, q.b)
    if pa is None or pb is None:
        return Result("not_recorded", None)
    if pa.label == pb.label:  # e.g. first vs last with a single prescription: nothing to compare
        return Result("not_recorded", None)

    if (op, t) == ("compare", "dosha"):
        unknown = tuple(p.label for p in (pa, pb) if p.dosha is None)
        if unknown:
            return Result("unknown", None, (), unknown)
        return Result("ok", {"a": pa.label, "b": pb.label, "dosha_a": pa.dosha, "dosha_b": pb.dosha,
                             "changed": pa.dosha != pb.dosha},
                      (Evidence(pa.label, "dosha", pa.dosha), Evidence(pb.label, "dosha", pb.dosha)))

    if (op, t) == ("compare", "herb"):
        unknown = tuple(p.label for p in (pa, pb) if p.herbs is None)
        if unknown:
            return Result("unknown", None, (), unknown)
        ga, gb = pa.grams_of(q.herb), pb.grams_of(q.herb)
        if not ga and not gb:
            return Result("not_recorded", None)
        return Result("ok", {"a": pa.label, "b": pb.label, "grams_a": ga, "grams_b": gb, "delta": gb - ga},
                      (Evidence(pa.label, f"herbs.{q.herb}.grams", ga), Evidence(pb.label, f"herbs.{q.herb}.grams", gb)))

    # diff prescription
    unknown = tuple(p.label for p in (pa, pb) if p.herbs is None or p.dosha is None)
    value: dict = {"a": pa.label, "b": pb.label}
    if pa.herbs is not None and pb.herbs is not None:
        ha, hb = dict(), dict()
        for name, g in pa.herbs:
            ha[name] = ha.get(name, 0) + g
        for name, g in pb.herbs:
            hb[name] = hb.get(name, 0) + g
        value["added"] = sorted([[n, hb[n]] for n in hb if n not in ha])
        value["removed"] = sorted([[n, ha[n]] for n in ha if n not in hb])
        value["changed"] = sorted([[n, ha[n], hb[n]] for n in ha if n in hb and ha[n] != hb[n]])
    else:
        value["added"] = value["removed"] = value["changed"] = None
    value["dosha_a"], value["dosha_b"] = pa.dosha, pb.dosha
    value["dosha_changed"] = None if pa.dosha is None or pb.dosha is None else pa.dosha != pb.dosha
    evidence = (Evidence(pa.label, "record", _rx_facts(pa)), Evidence(pb.label, "record", _rx_facts(pb)))
    return Result("incomplete" if unknown else "ok", value, evidence, unknown)
