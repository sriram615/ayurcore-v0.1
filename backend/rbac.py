"""
Role-based access control (Phase A, A3 -- RBAC + Clinic Isolation).

Before A3, "authorization" was really just "is this Authorization header a
real, unexpired, unrevoked session" (A2). Any authenticated user -- doctor,
admin, auditor, didn't matter -- could call every endpoint, and
`GET /audit/export` didn't even require that much: it took `clinic_id` as a
plain, unauthenticated query parameter and would hand back any clinic's
corrections data to anyone who asked. A3 closes both gaps: WHICH role may
call WHICH endpoint, and WHOSE clinic's data a call is allowed to touch.

This module is the single source of truth for both questions. It is
deliberately kept separate from backend/auth_service.py (identity and
sessions -- importable and testable with zero FastAPI dependency, so
tests/test_auth_service.py's session-layer tests keep running in
environments without FastAPI installed) and separate from backend/main.py
(the actual route handlers) -- this module owns "who is allowed to do what,"
main.py owns "what does doing it actually involve."

================================================================================
PERMISSION MATRIX -- written down BEFORE any of the code below enforces it,
per the explicit instruction that this must be designed, not improvised.
================================================================================

Roles come from migration 004's CHECK constraint on users.role: exactly
DOCTOR, ADMIN, AUDITOR. No other role value can exist in this database.

| Endpoint                          | DOCTOR | ADMIN | AUDITOR | Clinic scope                         |
|------------------------------------|:------:|:-----:|:-------:|----------------------------------------|
| POST /auth/login                   | public | public| public  | n/a -- no identity exists yet          |
| POST /auth/logout                  |  YES   |  YES  |  YES    | self only (revokes the caller's OWN session -- there is no "logout someone else," so no clinic question even arises) |
| POST /prescribe/preview            |  YES   |  NO   |  NO     | auth.clinic_id (server-derived)        |
| POST /prescribe/manual-preview     |  YES   |  NO   |  NO     | auth.clinic_id (server-derived)        |
| POST /prescribe/confirm            |  YES   |  NO   |  NO     | preview's own clinic_id, must equal auth.clinic_id |
| GET  /audit/export                 |  NO    |  YES  |  YES    | auth.clinic_id (server-derived)        |
| POST /patients                     |  YES   |  YES  |  NO     | auth.clinic_id (server-derived; client clinic_id rejected) |
| patient_pk on preview/manual-preview |  YES |  n/a  |  n/a    | patient must belong to auth.clinic_id, else 404 |
| GET  /patients?q=                  |  YES   |  YES  |  NO     | auth.clinic_id (server-derived)        |
| GET  /patients/{pk}/prescriptions  |  YES   |  YES  |  NO     | patient must belong to auth.clinic_id, else 404 |
| POST /patients/{pk}/encounters     |  YES   |  NO   |  NO     | patient must belong to auth.clinic_id, else 404; client clinic_id rejected |
| GET  /patients/{pk}/encounters     |  YES   |  YES  |  NO     | patient must belong to auth.clinic_id, else 404 |
| POST /patients/{pk}/ask            |  YES   |  YES  |  NO     | patient must belong to auth.clinic_id, else 404 (read-only; AI-2 question about the recorded history) |
| POST /prescriptions/{pk}/dispense  |  YES   |  NO   |  NO     | prescription must belong to auth.clinic_id, else 404 (marks a confirmed prescription as dispensed) |
| POST /prescriptions/{pk}/payment   |  YES   |  NO   |  NO     | prescription must belong to auth.clinic_id, else 404 (records payment against a prescription) |
| GET  /prescriptions/{pk}/invoice   |  YES   |  YES  |  NO     | prescription must belong to auth.clinic_id, else 404 (read-only receipt summary) |
| GET  /patients/{pk}/brief          |  YES   |  YES  |  NO     | patient must belong to auth.clinic_id, else 404 (read-only consultation brief summary) |
| GET  /encounters/{pk}              |  YES   |  YES  |  NO     | encounter must belong to auth.clinic_id, else 404 |
| POST /encounters/{pk}/close        |  YES   |  NO   |  NO     | encounter must belong to auth.clinic_id, else 404 |

Reasoning for every YES and every NO below -- per the rule that a permission
must be justified, never granted just because a test would be more
convenient with it:

- **Prescribing (`/prescribe/preview`, `/prescribe/manual-preview`,
  `/prescribe/confirm`) is DOCTOR-only.** Prescribing is the one genuinely
  safety-critical, licensed clinical act this whole system exists to gate
  correctly (see A1/A2's own docstrings on why /confirm's transaction and
  session binding exist at all). ADMIN and AUDITOR are administrative and
  oversight roles, not clinical ones -- granting either of them the ability
  to issue a prescription "for convenience" (an admin covering for a doctor,
  say) is exactly the kind of scope creep this instruction explicitly
  forbids without a documented reason, and no such reason has been given.
  If a real clinic later needs an admin who is ALSO a doctor, that's a
  second user account with role=DOCTOR, not a permission grant on ADMIN.
- **`/prescribe/confirm` is DOCTOR-only for the same reason as the other
  two** -- it is the step that actually deducts inventory and writes a
  real prescription record. It is not a "lesser" action than /preview that
  a non-clinical role could safely be trusted with; if anything it is the
  MORE safety-critical of the two.
- **`/audit/export` is ADMIN and AUDITOR, not DOCTOR.** Auditing the
  AI-proposal-vs-confirmation pipeline is precisely ADMIN's and AUDITOR's
  reason to exist (AUDITOR's name says so directly). Nothing in this
  project's spec asks a doctor to review the audit trail as part of their
  job, so DOCTOR is deliberately excluded rather than included "just in
  case it's useful" -- a real access need can be added later, additively,
  the same way this matrix itself can grow.
- **`/auth/logout` has no role restriction** because it isn't really an
  action ON a resource at all -- it revokes the token that authenticated
  the call itself. There is no version of "logout" that touches another
  user's session or another clinic's data, so a role/clinic check here
  would be enforcing a boundary that doesn't exist.

Clinic scoping is a SECOND, INDEPENDENT check from the role check above,
not a side effect of it. A role check answers "can this ROLE perform this
ACTION at all"; a clinic check separately answers "does this specific
resource belong to THIS user's clinic." Both must pass. Every clinic
check in this system is anchored to `AuthenticatedSession.clinic_id` --
which itself comes from a live database join in
backend/auth_service.py's resolve_session(), never from anything a client
sends in a request body, header, or query string. See
backend/main.py's /prescribe/confirm and /audit/export for where each
check is actually applied, and why cross-clinic previews are rejected
with 404 (treated as "does not exist" from a foreign clinic's point of
view -- consistent with not confirming a resource's existence to a tenant
who has no business knowing about it) while a same-clinic session
mismatch keeps the LOCKED contract's existing 403 (a same-clinic
colleague's preview is known to exist, just not confirmable by you).
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException

from backend.auth_service import AuthenticatedSession, resolve_session

# The exact three values migration 004's CHECK constraint allows on
# users.role. Defined once here so every role check and every test
# references the SAME strings, rather than each call site retyping
# "DOCTOR" / "ADMIN" / "AUDITOR" as separate opportunities to typo one.
ROLE_DOCTOR = "DOCTOR"
ROLE_ADMIN = "ADMIN"
ROLE_AUDITOR = "AUDITOR"
ALL_ROLES = frozenset({ROLE_DOCTOR, ROLE_ADMIN, ROLE_AUDITOR})

# Machine-checkable mirror of the matrix documented above, keyed by a
# short logical name (not a literal path, since /prescribe/preview and
# /prescribe/manual-preview share one entry -- they are the same
# clinical action with two different proposal sources). Tests import
# this directly to assert the CODE actually matches the DOCUMENTED
# matrix, rather than the two silently drifting apart over time.
PERMISSION_MATRIX = {
    "prescribe": frozenset({ROLE_DOCTOR}),
    "audit_export": frozenset({ROLE_ADMIN, ROLE_AUDITOR}),
    # Patient Registry Phase 1 (Addendum v1.5): registering/reading patients
    # is DOCTOR + ADMIN. AUDITOR is deliberately excluded -- the audit role's
    # data (/audit/export) is identity-free by design, and giving it the
    # registry would hand it exactly the patient identity that design keeps
    # out.
    "patients": frozenset({ROLE_DOCTOR, ROLE_ADMIN}),
    # Encounter foundation Phase 1A (migration 010). Opening/closing an
    # encounter is a clinical act, so DOCTOR only -- the same reasoning as
    # "prescribe" above. Reading follows the patient-access policy (DOCTOR +
    # ADMIN). AUDITOR gets neither: an encounter is patient-level data.
    "encounters_write": frozenset({ROLE_DOCTOR}),
    "encounters_read": frozenset({ROLE_DOCTOR, ROLE_ADMIN}),
    # AI-2 consultation memory (POST /patients/{pk}/ask). A clinical question about a patient's recorded history, asked while
    # consulting: DOCTOR and ADMIN may both ask about their own clinic's patients. Read-only; it writes nothing.
    "memory_ask": frozenset({ROLE_DOCTOR, ROLE_ADMIN}),
    # Prescription dispensing (POST /prescriptions/{pk}/dispense). A clinical act -- marking a confirmed
    # prescription as physically handed to the patient. DOCTOR only, like prescribing itself.
    "prescription_dispense": frozenset({ROLE_DOCTOR}),
    # Prescription payment recording (POST /prescriptions/{pk}/payment). Records money received against
    # a prescription. DOCTOR only (billing is a clinical-adjacent act in a single-doctor clinic).
    "prescription_payment": frozenset({ROLE_DOCTOR}),
    # Prescription invoice / receipt (GET /prescriptions/{pk}/invoice). Read-only receipt summary.
    # DOCTOR + ADMIN (an auditor may need to see invoices for reconciliation).
    "prescription_invoice": frozenset({ROLE_DOCTOR, ROLE_ADMIN}),
    # Consultation brief (GET /patients/{pk}/brief). A short, cited summary of one patient's recorded
    # history for the start of a consultation. Read-only: reads the snapshot, writes nothing.
    # DOCTOR + ADMIN (a clinician preparing for the visit, or an admin reviewing it).
    "patient_brief": frozenset({ROLE_DOCTOR, ROLE_ADMIN}),
}


def require_auth(authorization: Optional[str] = Header(default=None)) -> AuthenticatedSession:
    """FastAPI dependency: the ONE place an Authorization header is turned
    into a real, validated identity (Phase A, A2; moved here unchanged
    from backend/main.py in A3 so backend/rbac.py's require_role() below
    can build directly on it without a circular import between main.py
    and this module).

    Rejects, with 401 in both cases, an Authorization header that is
    missing/empty, malformed (not `Bearer <token>`), or that resolves
    (via backend.auth_service.resolve_session) to nothing -- which
    covers a forged token, an unknown token, an expired session, a
    revoked session, and a session whose user has since been
    deactivated, all identically.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header. Expected 'Bearer <token>'.",
        )

    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header. Expected 'Bearer <token>'.",
        )

    session = resolve_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid, expired, or revoked session.")

    return session


def require_role(*allowed_roles: str):
    """Dependency FACTORY (Phase A, A3): returns a FastAPI dependency that
    first requires real authentication (via require_auth above -- so a
    401 always wins over a 403; an unauthenticated caller never learns
    anything about what roles an endpoint accepts), then requires the
    resolved session's role to be one of `allowed_roles`.

    `allowed_roles` is always passed as one of the frozensets in
    PERMISSION_MATRIX above (or individual ROLE_* constants), never a
    bare string typed inline at the call site -- this keeps the matrix
    documented in exactly one place. role is read live from
    auth.role, which itself came from resolve_session()'s live JOIN
    against the users table on THIS request -- not cached in the
    session token or anywhere else. A role change (or a clinic change,
    or deactivation) made directly in the database therefore takes
    effect on the very next request a user's existing, still-valid
    session makes; there is no stale-privilege window to wait out.

    Raises 403 (never 404 -- unlike the clinic checks in backend/main.py,
    there is nothing to hide about an endpoint's existence from an
    authenticated-but-wrong-role caller) with a generic message that
    does not enumerate which roles WOULD have been allowed.
    """
    def _check(auth: AuthenticatedSession = Depends(require_auth)) -> AuthenticatedSession:
        if auth.role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail="Your role is not permitted to perform this action.",
            )
        return auth

    return _check
