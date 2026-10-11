"""
A0's context-engineering contract (A0 architecture report, section 6).

AgentContext is deliberately a frozen dataclass with exactly two fields:
clinic_id and request_id. Two rules about it, both load-bearing (section 5):

  1. It is constructed in backend/main.py from the authenticated session --
     never inside agents/. agents/ has zero knowledge of FastAPI, sessions,
     or tokens today; this package keeps it that way. The day agents/
     imports auth_service, the PII/auth air-gap argument gets much harder
     to make.
  2. It is frozen, and carries no user_id, username, session_id,
     preview_id, and nothing derived from patient identity. It cannot grow
     new fields at a call site.

build_clinical_context(ctx) assembles everything section 6 says "may enter
the prompt" and is PII-free by construction: the clinic's formulary
(derived from HERB_ALIASES, not copied from agents/copilot.py's SEED_HERBS
-- see the report's section 3 finding on the formulary already living in
four places that must be hand-synced) and this clinic's current stock (one
bulk read, replacing the N check_stock round trips the control arm makes).

Because every field here is PII-free by construction, no new
assert_no_pii_leak boundary is needed around the assembled prompt --
that is the "preferred" branch of the two options section 6 lays out.
Nothing under agents/a0/ receives, stores, or forwards patient identity,
a PII token mapping, or untokenized note text; only backend/main.py
tokenizes and rehydrates, exactly as it does for the control arm.
"""
from dataclasses import dataclass

from backend.herb_resolver import HERB_ALIASES
from backend.inventory_read import list_stock


@dataclass(frozen=True)
class AgentContext:
    clinic_id: str
    request_id: str


@dataclass(frozen=True)
class ClinicalContext:
    """Everything build_clinical_context assembles for one request."""
    formulary: list[str]
    stock_by_herb: dict[str, int]


def build_clinical_context(ctx: AgentContext) -> ClinicalContext:
    """Assembles the PII-free clinical context for one request. Sorted and
    de-duplicated so the same clinic produces a stable prompt across
    calls, which matters for reading diffs during the A/B evaluation in
    the report's section 11."""
    formulary = sorted(set(HERB_ALIASES.values()))
    stock_by_herb = list_stock(ctx.clinic_id)
    return ClinicalContext(formulary=formulary, stock_by_herb=stock_by_herb)
