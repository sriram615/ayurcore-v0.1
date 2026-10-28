"""
The input a guardrail evaluates: a prescription candidate.

A candidate is the clinical content only -- dosha plus herb lines -- in the
form /prescribe/confirm would commit it (canonical herb names, merged
totals). It carries NO patient identity (name, phone, ID) and no clinic
context: which patient or encounter facts rules may read is an open decision
(documentation/docs/PHASE_3_PLAN.md §5 D3).

Validation here is STRUCTURAL only (types, shape, finiteness), so malformed
input fails loudly with InvalidCandidateError -- never a PASS. Whether a
well-formed value is acceptable -- e.g. 0 g, 2.5 g, an unknown herb -- is for
the rules to judge, so those values are accepted here and reported as rule
results.

No limit here may be stricter than what /prescribe/confirm accepts. The
stored AI proposal it commits by default (backend/schemas.ClinicalParse) has
no bound on dosha length or on the number of herb lines, so neither is
bounded here; a herb name is bounded exactly as PrescribedHerb bounds it.

Size on the confirm path is nonetheless bounded where it matters for rules:
build_confirm_candidate merges lines by canonical herb, so a confirm
candidate never has more lines than the formulary has herbs (20 today),
however many lines were submitted. Only the dosha text is unbounded (AI
proposals).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from pydantic import ValidationError

from backend.herb_resolver import UnknownHerbError, merge_herb_lines
from backend.schemas import PrescribedHerb

# PrescribedHerb.name's max_length, the bound every confirm path applies.
MAX_HERB_NAME_LENGTH = next(m.max_length for m in PrescribedHerb.model_fields["name"].metadata
                            if hasattr(m, "max_length"))


class InvalidCandidateError(ValueError):
    """The candidate is structurally malformed and cannot be evaluated."""


@dataclass(frozen=True)
class HerbLine:
    name: str
    grams: int | float


@dataclass(frozen=True)
class Candidate:
    dosha_imbalance: str | None
    herbs: tuple[HerbLine, ...]

    def canonical_json(self) -> str:
        """Whole-number grams are written as integers, so 5 and 5.0 (or 0 and
        -0.0) -- which compare equal -- give one fingerprint. Every other value
        is written as given; HerbLine keeps the original for the rules."""
        return json.dumps(
            {"dosha_imbalance": self.dosha_imbalance,
             "herbs": [[h.name, _canonical_grams(h.grams)] for h in self.herbs]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _canonical_grams(grams: int | float) -> int | float:
    # Finite by construction (_grams rejects inf and nan), so int() is safe.
    return int(grams) if isinstance(grams, float) and grams.is_integer() else grams


def _text(value, field: str, allow_none: bool, max_length: int | None = None) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise InvalidCandidateError(f"{field} must be a string{' or null' if allow_none else ''}")
    if max_length is not None and len(value) > max_length:
        raise InvalidCandidateError(f"{field} exceeds {max_length} characters")
    return value


def _grams(value, index: int) -> int | float:
    # bool is an int subclass; True must not be read as 1 g.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidCandidateError(f"prescribed_herbs[{index}].grams must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidCandidateError(f"prescribed_herbs[{index}].grams must be finite")
    return value


def build_candidate(dosha_imbalance, prescribed_herbs) -> Candidate:
    """Builds a Candidate from the same shapes the API uses:
    `prescribed_herbs` is a list of {"name": str, "grams": number}.
    Raises InvalidCandidateError for anything structurally malformed."""
    dosha = _text(dosha_imbalance, "dosha_imbalance", allow_none=True)
    if prescribed_herbs is None:
        prescribed_herbs = []
    if not isinstance(prescribed_herbs, (list, tuple)):
        raise InvalidCandidateError("prescribed_herbs must be a list")
    lines = []
    for i, herb in enumerate(prescribed_herbs):
        if not isinstance(herb, dict) or set(herb) != {"name", "grams"}:
            raise InvalidCandidateError(f"prescribed_herbs[{i}] must be an object with exactly 'name' and 'grams'")
        lines.append(HerbLine(name=_text(herb["name"], f"prescribed_herbs[{i}].name", allow_none=False,
                                         max_length=MAX_HERB_NAME_LENGTH),
                              grams=_grams(herb["grams"], i)))
    return Candidate(dosha_imbalance=dosha, herbs=tuple(lines))


def build_confirm_candidate(dosha_imbalance, prescribed_herbs) -> Candidate:
    """The candidate exactly as /prescribe/confirm would commit it: each line
    validated as confirm validates it (PrescribedHerb), then resolved and
    merged by the same shared step deduct_and_prescribe uses
    (herb_resolver.merge_herb_lines). A line confirm would refuse, or a herb
    that doesn't resolve (confirm's 400), cannot become a committed
    prescription, so it raises InvalidCandidateError -- never a PASS.
    Merged totals are left for the rules to judge, as confirm's own
    merged-total check does."""
    dosha = _text(dosha_imbalance, "dosha_imbalance", allow_none=True)
    if prescribed_herbs is None:
        prescribed_herbs = []
    if not isinstance(prescribed_herbs, (list, tuple)):
        raise InvalidCandidateError("prescribed_herbs must be a list")
    # Privacy: pydantic's ValidationError and UnknownHerbError both carry the
    # raw input (herb name, grams text). Only a fixed, value-free summary is
    # kept, and InvalidCandidateError is raised AFTER the except block, so it
    # has no __cause__ or __context__ -- a logged traceback cannot show them.
    problem = None
    try:
        lines = [PrescribedHerb.model_validate(h).model_dump() for h in prescribed_herbs]
        merged = merge_herb_lines(lines)
    except ValidationError as exc:
        problem = f"a herb line is not accepted by confirmation ({exc.error_count()} error(s))"
    except UnknownHerbError:
        problem = "a herb does not resolve to the formulary"
    if problem is not None:
        raise InvalidCandidateError(problem)
    return build_candidate(dosha, merged)
