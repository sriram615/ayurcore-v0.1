# AyurCore — API Contract Addendum v1.4

**Date:** September 14, 2026, delivered alongside the Phase B / A4 (Idempotent Confirmation) milestone.
**Status:** additive only. `AyurCore_API_Contract_v1.md` and addenda v1.1–v1.3 are completely unchanged — every request/response JSON shape, every status code, and every existing rejection mode they document still holds exactly as written, for every caller that does not send the new header this addendum introduces.

This addendum adds exactly one thing: an optional `Idempotency-Key` request header on `POST /prescribe/confirm`, and the response semantics that follow from sending it.

---

## D1. `Idempotency-Key` header on `POST /prescribe/confirm`

**Request**

```
POST /prescribe/confirm
Authorization: Bearer <token>        (required, unchanged since v1.2)
Idempotency-Key: <opaque string>     (OPTIONAL, new in this addendum)
```

- **Optional.** A caller that omits this header gets exactly the behavior every prior contract version documents — no deduplication guarantee, the request is always executed as a fresh attempt. This preserves every existing integration untouched.
- When present, it is treated purely as a caller-supplied deduplication hint, scoped to `(the caller's own authenticated user_id, "prescribe_confirm", this key value)`. It is never used for authentication or authorization — those remain entirely derived from the `Authorization` header's resolved session, exactly as A2/A3 established. See D3 for why this key can never become a cross-user or cross-clinic information channel.
- Recommended for any caller that may retry a request whose outcome is unknown (a timeout, a dropped connection) — a retry with the identical key AND an identical request body is guaranteed safe (D2). A caller is free to choose any non-empty opaque string; this project's own doctor-facing web UI generates one via `crypto.randomUUID()` per confirmation attempt.

**Response — 200, on a replay**

Identical, byte-for-byte (as parsed JSON), to the original successful response for that key. No new business logic runs; no second prescription is created; no second inventory deduction occurs; no second correction is logged.

**Response — 409, new rejection modes**

Two new cases, both `409 Conflict`, both only reachable when `Idempotency-Key` is supplied:

```
HTTP/1.1 409 Conflict
```

```json
{ "detail": "Idempotency key '<key>' was already used with a different request." }
```

Reused key, but the request body (`preview_id`, `dosha_imbalance`, or `prescribed_herbs`) differs from the original request that key was first used for. The original committed result is completely unaffected.

```
HTTP/1.1 409 Conflict
```

```json
{ "detail": "Idempotency key '<key>' is no longer valid for the current authorization context." }
```

Reused key, same request body, but the caller's live clinic affiliation no longer matches what was recorded when the key was first reserved. Not reachable via any endpoint in the current system (nothing reassigns a user's clinic), included as a documented, fail-closed defense for a capability that doesn't exist yet.

**Everything else about `POST /prescribe/confirm` is unchanged.** Full precedence for this endpoint's failures, with the two new cases inserted:

`401` (not authenticated) → `403` (wrong role) → `409` (idempotency key reused with a different request or an inconsistent authorization context — new in this addendum) → `404` (preview doesn't exist, or exists in a different clinic) → `403` (exists in your own clinic, but a different session created it) → `409` (already confirmed — v1.3, unchanged) → `400`/`409` (herb/stock validation — unchanged) → `200`.

## D2. What "identical request" means for a replay

A retry is recognized as a replay of the same logical operation — and therefore safely short-circuited to the original result — when it presents the same `Idempotency-Key` **and** the same values for `preview_id`, `dosha_imbalance`, and `prescribed_herbs` as the original request that key was first used for. `dosha_imbalance`/`prescribed_herbs` being entirely absent (omitted, meaning "use the AI's own proposal") on both the original and the retry counts as identical. Any other field of the request, or any difference in either of those two fields' actual values, is a *different* request and gets the 409 described in D1, never a silent replay of the wrong result.

## D3. This header introduces no new information a caller could not already learn

`Idempotency-Key` never participates in any authorization decision, and reusing another user's key string (deliberately or by coincidence) has no observable effect: the internal scoping is `(auth.user_id, operation, key)`, where `auth.user_id` is always the caller's own server-derived identity from the resolved session, exactly like `auth.clinic_id` everywhere else in this contract. A caller who presents a key another user has used simply gets treated as if the key had never been used at all — there is no status code, timing difference, or response shape that distinguishes "nobody has used this key string" from "a different user used this exact string." This is the same non-disclosure principle v1.3/C2 established for cross-clinic preview access, applied here at the key-lookup layer.
