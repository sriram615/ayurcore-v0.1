"""
agents/a0 -- the Agentic A0 treatment arm (A0 architecture report, section 9).

Public surface is deliberately exactly two names: `propose` (a second
implementation of agents/copilot.parse_clinical_notes's signature-and-
return-type contract) and `AgentContext` (the frozen, two-field object
that carries clinic_id/request_id into it). Nothing else here is meant to
be imported from outside this package.

This package imports nothing from backend/main.py, backend/rbac.py, or
backend/auth_service.py, and holds no reference to a request, a session,
or a token -- see agents/a0/context.py's docstring for why that boundary
is load-bearing, not incidental.
"""
from agents.a0.context import AgentContext
from agents.a0.orchestrator import propose

__all__ = ["propose", "AgentContext"]
