# AyurCore — API Contract Addendum v1.3

**Date:** September 14, 2026, delivered alongside the Phase A / A3 (RBAC + Clinic Isolation) milestone report.
**Status:** additive, with two explicitly-permitted behavioral changes, both new *rejection* modes on endpoints that previously accepted a request. `AyurCore_API_Contract_v1.md`, `v1.1_addendum.md`, and `v1.2_addendum.md` are otherwise unchanged — every request/response JSON shape v1 and its prior addenda document still holds exactly as written. The two changes:

1. `POST /prescribe/preview`, `POST /prescribe/manual-preview`, and `POST /prescribe/confirm` now require the caller's role to be `DOCTOR` — an authenticated ADMIN or AUDITOR is now rejected with `403`, where v1.2 would have let them through (any authenticated user, any role, could call these).
2. `GET /audit/export` — previously **not part of the locked v1 contract at all**, and unauthenticated — gets its **first formal contract entry** in this addendum: it now requires authentication, requires the caller's role to be `ADMIN` or `AUDITOR`, and its data is now scoped to the caller's own clinic rather than an arbitrary client-supplied one.

v1.2 §B5 explicitly deferred exactly this: *"Full clinic-isolation policy... is explicitly out of scope for this addendum and is deferred to Phase A's A3... milestone."* This is that milestone.

---

## C1. New `403` role semantics on `POST /prescribe/preview`, `POST /prescribe/manual-preview`, `POST /prescribe/confirm`

All three endpoints already required authentication as of v1.2 (`401` on missing/invalid/expired/revoked session). As of this addendum, a request that **is** authenticated but whose session's role is not `DOCTOR` is now rejected:

```
HTTP/1.1 403 Forbidden
```

```json
{ "detail": "Your role is not permitted to perform this action." }
```

`401` still always takes priority over this `403` — an unauthenticated caller is rejected before their role (which doesn't exist yet, for them) is ever considered. A `403` from these endpoints therefore always means "this session is real, but not a DOCTOR," never "no session at all."

**Everything else about these three endpoints is unchanged** — same request/response JSON shapes, same `400`/`404`/`409`/`422`/`500`/`502` semantics prior addenda document, same preview TTL, same single-use guarantee.

## C2. New `404` semantics on `POST /prescribe/confirm` for a cross-clinic preview

v1.2 §B4 documented the existing same-clinic, different-session rejection: a preview created under one authenticated session cannot be confirmed by a *different* session, `403`. That is completely unchanged by this addendum.

New in this addendum: a preview belonging to a **different clinic** than the confirming caller's own (`auth.clinic_id`, server-derived) is rejected with the SAME `404` this endpoint already returns for an unknown or expired `preview_id`:

```
HTTP/1.1 404 Not Found
```

```json
{ "detail": "Preview not found or expired. Please re-run /prescribe/preview." }
```

This is deliberate: a cross-clinic caller receives no signal distinguishing "this preview never existed" from "this preview exists, but in a clinic that isn't yours" — consistent with not confirming a foreign tenant's resource's existence to a caller who has no business knowing about it. This check runs **before** the existing same-clinic session-binding check, so the full precedence for `/prescribe/confirm` failures is now: `401` (not authenticated) → `403` (wrong role) → `404` (preview doesn't exist, or exists in a different clinic) → `403` (exists in your own clinic, but a different session created it) → `409` (already confirmed) → other validation/business-logic errors.

## C3. `GET /audit/export` — first formal contract entry

This endpoint was never part of the locked v1 contract, and v1.2 §"Not yet covered" explicitly flagged it as still unauthenticated and an open risk. This is its first appearance in any versioned contract document.

**Request**

```
GET /audit/export?clinic_id=<optional string>
Authorization: Bearer <token>   (required)
```

- Requires a valid, authenticated session (`401` otherwise — same generic messages as every other protected endpoint; see v1.2 §B3).
- Requires the session's role to be `ADMIN` or `AUDITOR` (`403` otherwise — a `DOCTOR`'s valid session is rejected here, the mirror image of C1 above).
- `clinic_id` is **optional**. If omitted, the response is scoped to the caller's own `auth.clinic_id` (server-derived, from the session — never a client default). If supplied, it **must** equal `auth.clinic_id`; a supplied value that does not match is rejected outright:

```
HTTP/1.1 403 Forbidden
```

```json
{ "detail": "Cannot access another clinic's audit data." }
```

This is a deliberately different posture than C2's `404` for `/prescribe/confirm`: `clinic_id` here is not a resource lookup key whose existence should be hidden from a foreign tenant — it is an assertion by the caller about whose data they expect back, and a false assertion is a plain, undisguised `403`, exactly like a role mismatch. The data actually returned is never substituted or inferred from the parameter; it always comes from `auth.clinic_id`.

**Response — 200** (shape unchanged from `AuditExportResponse` — this addendum only formalizes it, having never been documented before):

```json
{
  "clinic_id": "string — always the CALLER's own clinic_id, never a client-supplied value",
  "generated_at": "datetime",
  "record_count": "integer",
  "changed_count": "integer",
  "records": [
    {
      "prescription_id": "integer",
      "clinic_id": "string",
      "created_at": "datetime",
      "ai_dosha_imbalance": "string",
      "ai_prescribed_herbs": [{"name": "string", "grams": "integer"}],
      "confirmed_dosha_imbalance": "string",
      "confirmed_prescribed_herbs": [{"name": "string", "grams": "integer"}],
      "was_changed": "boolean"
    }
  ],
  "integrity_notes": ["string", "..."]
}
```

Contains no patient identity (name/phone/ID) anywhere, by design — unchanged from how this endpoint has always behaved; only its access control is new.

## C4. New `role` field on `LoginResponse`

```json
{
  "token": "string",
  "expires_at": "datetime",
  "username": "string",
  "clinic_id": "string",
  "role": "string — one of DOCTOR, ADMIN, AUDITOR. New in this addendum."
}
```

Purely informational for the client (e.g. so a UI can hide a button a `DOCTOR` has no access to). It grants nothing by itself: every protected endpoint re-derives the caller's role live from the server-side session on every request (see `backend/rbac.py`), never from this field or anything else the client sends back.

## C5. `clinic_id` on preview creation — explicitly proven ignored, not just unused

v1.2 §B5 already stated that `PreviewRequest.clinic_id` / `ManualPreviewRequest.clinic_id` are not trusted for the preview's actual clinic. This addendum does not change that behavior, but does formally confirm it now has explicit adversarial test coverage (`tests/test_rbac.py::test_forged_clinic_id_in_preview_request_body_is_ignored`): a client-supplied `clinic_id` differing from the caller's own account clinic has zero effect on the preview actually created — the request still succeeds (the field is not rejected), but the persisted preview always belongs to `auth.clinic_id`.

---

## Not yet covered (upcoming Phase A milestones, not this addendum)

- **Rate limiting / brute-force protection on `/auth/login`**: still not implemented (carried forward from v1.2 — deferred to the Phase A security review).
- **`ui/app.py`**: still sends no `Authorization` header at all on any request (a pre-existing, A2-era gap, unchanged by A3 — see the A2 report's own note on this). Every prescribe/audit call the current UI makes will receive `401` until it gains a login flow. Not fixed in A3, which is a backend-only milestone per the user's explicit scope.
- **`scripts/verify_live_integration.py`, `scripts/verify_malayalam_manglish*.py`**: standalone, Mac-only, billed-Gemini-call manual verification scripts that also send no `Authorization` header — already broken by A2's auth requirement, unchanged (not newly broken) by A3. Flagged here as a known pre-existing gap, not silently carried forward as acceptable.
- Idempotency keys (A4) and tamper-evident audit logging (A5) are still to come and will get their own addendum entries when built, per the same additive-but-flagged rule this document and its predecessors all follow.
