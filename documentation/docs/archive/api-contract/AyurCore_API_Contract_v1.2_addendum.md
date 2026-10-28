# AyurCore — API Contract Addendum v1.2

**Date:** September 14, 2026, delivered alongside the Phase A / A2 (Real Authentication) milestone report.
**Status:** additive, with one explicitly-permitted behavioral change. `AyurCore_API_Contract_v1.md` and `v1.1_addendum.md` are otherwise unchanged — every request/response JSON shape and status code documented there still holds exactly as written. The one change: `POST /prescribe/preview`, `POST /prescribe/manual-preview`, and `POST /prescribe/confirm` now **require** a valid `Authorization: Bearer <token>` header and return `401` without one. v1 already specified this header's *shape* ("a server-side opaque session token... don't parse it") and explicitly flagged that verifying it was "inert... until real auth exists, activating automatically the day it does" — this addendum is that day, and the resulting `401` is exactly the "documented auth error" v1's locked-endpoint clause anticipated.

---

## B1. `POST /auth/login` (new endpoint)

**Why it exists:** replaces the placeholder "authentication" — an `Authorization` header that was accepted as-is, with no real user behind it — with real, persistent, server-verified identity. See the A2 report's section A for the full before/after.

**Request**

```json
{
  "username": "string, required, 1-128 chars",
  "password": "string, required, 1-256 chars"
}
```

**Response — 200**

```json
{
  "token": "string — opaque, server-generated bearer token. NOT a JWT; the client must not attempt to decode it.",
  "expires_at": "datetime — fixed, absolute expiry from creation (12 hours); not extended by use",
  "username": "string",
  "clinic_id": "string — the account's own persisted clinic_id, never something the client can override at login time"
}
```

**Error responses**
- `401` — `{"detail": "Invalid username or password."}`. Returned for a nonexistent username, a wrong password, **and** a correct password on a deactivated account, all identically — by design. Nothing in the response (or in how long the request takes) reveals which of the three occurred; see the A2 report's section B for why.
- `422` — standard FastAPI validation error if `username`/`password` are missing or malformed.

**Guarantees**: a successful login creates a real, persistent row in the `sessions` table (migration 005) — the session survives an application restart, because there is no in-memory session state anywhere for a restart to lose. The returned `token` is shown to the caller exactly once, here; only a SHA-256 hash of it is ever persisted server-side.

## B2. `POST /auth/logout` (new endpoint)

**Request**: no body. Requires `Authorization: Bearer <token>` — this is itself a protected endpoint, not an open one.

**Response — 200**

```json
{ "status": "logged_out" }
```

**Error responses**
- `401` — no/malformed/invalid/already-revoked `Authorization` header. Logging out twice with the same token returns `401` the second time (the session is already revoked), not a special "already logged out" shape.

**Guarantees**: revocation is immediate and permanent. The instant this call succeeds, that exact token stops authenticating anything — every protected endpoint, checked on every request, not on a cache or a delay.

## B3. New `401` semantics on `POST /prescribe/preview`, `POST /prescribe/manual-preview`, `POST /prescribe/confirm`

**This is the one behavioral change in this addendum.** All three now require `Authorization: Bearer <token>` where a valid, non-expired, non-revoked session's token hash is found. Previously this header was read but never verified — any value, or no value at all, was accepted.

```
HTTP/1.1 401 Unauthorized
```

```json
{ "detail": "Missing or malformed Authorization header. Expected 'Bearer <token>'." }
```

or

```json
{ "detail": "Invalid, expired, or revoked session." }
```

Both `401` variants are deliberately generic and do not distinguish *why* (missing header vs. malformed header vs. unknown token vs. expired vs. revoked vs. deactivated user) — a caller should treat any `401` from these endpoints identically: re-authenticate via `/auth/login`.

**Everything else about these three endpoints is unchanged** — same request/response JSON shapes, same `400`/`404`/`409`/`422`/`500`/`502` semantics v1 and v1.1 already document, same preview TTL, same single-use guarantee.

## B4. `created_by_session` and preview/session binding — now real, not inert

v1 §3a documented preview/session binding as inert: "a preview created with no session accepts a confirm from any session... The day real auth exists, this activates with no code change needed here." That day is this addendum. `created_by_session` on a preview now records the real, server-resolved session identity that created it (not a raw client-supplied header string), and `/prescribe/confirm`'s existing check — "a preview created under one session cannot be confirmed by a different one" (`403`) — now compares against a real, unforgeable session identity instead of an arbitrary string. No request or response shape changed to make this real; only what the comparison trusts did.

## B5. `clinic_id` on preview creation — no longer client-trusted for authorization

`PreviewRequest.clinic_id` and `ManualPreviewRequest.clinic_id` **remain in the request schema, unchanged** — a client may still send them, and doing so is not an error. However, as of this addendum, the value actually used to create the preview comes from the **authenticated user's own persisted `clinic_id`** (set on their account, not sent at request time), never from this request field. A client that sends a `clinic_id` different from its own account's clinic_id is not rejected — the field is simply ignored for this purpose. Full clinic-isolation *policy* (e.g., rejecting or scoping cross-clinic access outright) is explicitly out of scope for this addendum and is deferred to Phase A's A3 (RBAC + clinic isolation) milestone; this addendum only stops trusting an unverified client-supplied value for what identity/authorization decisions there already are.

`/prescribe/confirm` is unaffected by this point — it already derived `clinic_id` from the preview row (`entry["clinic_id"]`), never from the confirm request body, both before and after this addendum.

---

## Not yet covered (upcoming Phase A milestones, not this addendum)

- **Rate limiting / brute-force protection on `/auth/login`**: explicitly not implemented in A2. The login flow is enumeration-safe (see B1) and does not leak account existence, but nothing yet throttles repeated attempts against a single account or from a single caller. Deferred to a security-hardening pass (A6 / the Phase A security review), not silently claimed as solved here.
- **`GET /audit/export`**: unchanged and still **unauthenticated** — this addendum does not touch it. Flagged as an open risk in the A2 report's section H, not silently carried forward as acceptable.
- Full clinic isolation / RBAC enforcement (A3), idempotency keys (A4), and tamper-evident audit logging (A5) are all still to come and will get their own addendum entries when built, per the same additive-but-flagged rule this document and v1.1 both follow.
