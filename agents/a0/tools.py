"""
A0's read-only tool surface (A0 architecture report, sections 4, 6, 9).

Exactly one live tool: check_herb_alias, backed by
backend.herb_resolver.normalize_herb -- a pure dict lookup, no DB access,
no PII. There is no check_stock tool in this arm. The report's context
contract (section 6) moves stock out of the tool-calling loop entirely:
current stock for the caller's clinic is read once in
agents/a0/context.py's build_clinical_context and embedded directly into
the prompt (see agents/a0/prompt.py), not fetched mid-conversation the way
agents/copilot.py's check_stock tool does it. One fewer live tool is also
one fewer thing agents/a0/orchestrator.py's state machine has to bound,
count, and reason about under a deadline.

Only one import from outside agents/a0/: backend.herb_resolver.
normalize_herb (and its exception type). Nothing here imports, or can
transitively reach, any of the report's forbidden write tools --
inventory_service.deduct_and_prescribe, preview_store.create_preview /
mark_confirmed / mark_stale_previews_expired, audit_store.
create_audit_event, corrections_service.log_correction,
idempotency_store.reserve_idempotent_operation /
complete_idempotent_reservation, any auth_service session function, or
db.database.get_connection used for anything but a read.
tests/test_a0_no_write_imports.py enforces this statically, by walking
every import under agents/a0/, so a future edit that adds a convenient
import of backend.inventory_service (which also holds
deduct_and_prescribe) fails a test rather than silently widening what this
package can reach.
"""
from backend.herb_resolver import normalize_herb, UnknownHerbError


def check_herb_alias(raw_name: str) -> dict:
    """Check whether a herb name matches one this clinic actually stocks.

    Args:
        raw_name: the herb name as it appeared in the clinical notes (any
            local name, spelling, or botanical name).

    Returns:
        A dict with `known` (bool) and, if known, `canonical_name` (the
        exact name to use in the final answer).
    """
    try:
        canonical = normalize_herb(raw_name)
        return {"known": True, "canonical_name": canonical}
    except UnknownHerbError:
        return {"known": False, "canonical_name": None}
