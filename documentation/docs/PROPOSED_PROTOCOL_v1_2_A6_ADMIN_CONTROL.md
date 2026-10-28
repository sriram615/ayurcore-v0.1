# PROPOSED (not registered, not frozen): longitudinal protocol v1.2 — A6 ADMIN control

Status: **DRAFT FOR OWNER APPROVAL.** Nothing in `eval/protocols/`, `documentation/docs/AI2_LONGITUDINAL_EVALUATION_PROTOCOL_v1*.md` or any pinned artifact is changed by this document. It has no force until the owner approves it and a person registers it.

## Why a new version is needed

Protocol v1.1 (and `actor_aware_evaluator_spec_v1.json`) defines gate A6 with a paired control `admin_a_own_patient`: "ADMIN clinic A, a clinic A patient: must NOT be denied (ADMIN holds the patients permission)".

The shipping `/patients/{pk}/ask` route is guarded by `PERMISSION_MATRIX["memory_ask"] = {DOCTOR}` (`backend/rbac.py`), provisional decision D-02: DOCTOR-only. An ADMIN gets 403. Evaluated as written, A6 would fail on its own control for a reason that is a policy choice, not a defect. A frozen protocol cannot be edited, so the change needs a new version.

## Proposed change (one control, nothing else)

| Item | v1.1 | Proposed v1.2 |
|---|---|---|
| `paired_controls.admin_a_own_patient` | ADMIN clinic A on a clinic A patient: must NOT be denied | ADMIN clinic A on a clinic A patient: must be **forbidden** (403, reason FORBIDDEN), identically for every wording, and `build_snapshot` must not be called |
| `paired_controls.doctor_a_own_patient` | must NOT be denied | unchanged |
| Denials: no_token, auditor, doctor_a_asks_clinic_b_patient | unchanged | unchanged |
| Gate A6 threshold | 1.0 | unchanged (1.0) |
| All other gates (A1–A5, A7, B*, C*), thresholds, case sets, frozen files | — | unchanged |

Counts: the ADMIN case moves from "control (allowed)" to "denial (forbidden)". The number of actor-aware cases does not change.

## Alternative (not recommended)

Widen `memory_ask` to `{DOCTOR, ADMIN}`: keeps protocol v1.1 as written but changes RBAC (a security-relevant change to a route that returns clinical history), requires matching docs, tests, and frontend role handling. The recommended path is the smaller one above, because ADMIN is an operations role and the doctor-only reading of clinical history is the more conservative default.

## What approval would authorise, and what it would not

Would: registering this text as v1.2 (new file, new hash) and building/qualifying the actor-aware evaluator against it on synthetic stand-in cases.
Would not: a fix freeze, any run on the acceptance set, any RBAC change, or any claim that A6 has passed. The evaluator, once built, is qualified on synthetic stand-ins and dev-side controls only.

## Decision requested

D-02 final: (a) DOCTOR-only final + register v1.2 as above (recommended), or (b) widen to ADMIN with RBAC change.
