# AyurCore — API Contract Addendum v1.1

**Date:** September 13, 2026, delivered alongside the Phase A / A1 (Persistent Preview State) milestone report.
**Status:** additive only. Nothing in `AyurCore_API_Contract_v1.md` is changed, removed, or reworded by this document — every request/response JSON shape, status code, and guarantee in v1 still holds exactly as written. This addendum documents two things that were added to the running code *after* v1 was locked (an AI-outage fallback endpoint and an error-code header) but were never written down, plus what Phase A's A1 milestone did and didn't change underneath v1's existing endpoints.

---

## A1. `POST /prescribe/manual-preview` (new endpoint)

**Why it exists:** when `/prescribe/preview` can't reach Gemini (quota, outage, timeout — see A2 below), the clinic needs to keep prescribing. This endpoint is the fallback: same dosha/herb fields, no AI call, no free-text `notes` field, nothing ever sent to an external service. Everything downstream of it — herb-name resolution, the preview it creates, the `/prescribe/confirm` call that finishes it — is the *exact same code path* `/prescribe/preview` uses. It is a second front door onto the same pending-preview machinery, not a parallel system.

**Request**

```json
{
  "patient_name": "string, required",
  "patient_phone": "string, optional",
  "patient_id": "string, optional",
  "dosha_imbalance": "string, required — doctor's own clinical judgment, entered directly",
  "prescribed_herbs": [
    { "name": "string", "grams": "integer, > 0, <= 10000" }
  ],
  "clinic_id": "string, optional, default \"default_clinic\""
}
```

**Response — 200**

Identical shape to `/prescribe/preview`'s response:

```json
{
  "preview_id": "string — opaque handle; pass this unchanged to /confirm, exactly like an AI-sourced preview_id",
  "patient_name": "string",
  "dosha_imbalance": "string — echoed back as entered",
  "prescribed_herbs": [
    { "name": "string", "grams": "integer" }
  ]
}
```

**Error responses**
- `400` — a manually-entered herb name isn't recognized (same `normalize_herb` resolver `/preview` uses; a doctor's typo gets the identical rejection an AI hallucination would)

**Guarantees**: everything v1 §1–§2 already guarantees about a `preview_id` — opaque, unguessable, 30-minute expiry, single confirmable use, session-bound the same way — applies identically to a `preview_id` from this endpoint. The one behavioral difference downstream: a manual preview is **never** written to the `corrections` table on confirm (see A3 below) — there is no AI proposal to compare it against, and logging one anyway would misrepresent doctor-entered data as an unchanged AI proposal, corrupting the one dataset (`corrections`) this project is depending on staying clean.

## A2. `X-Error-Code: AI_SERVICE_UNAVAILABLE` header (new, on an existing error path)

**Where:** `POST /prescribe/preview`, on the existing `502` response for an AI-side failure.

v1 already documents `502` as "the AI call itself failed, or its response couldn't be safely de-tokenized" — that wording still fully applies. What's new is a header that lets a caller tell those two cases apart *without string-matching the human-readable `detail` text*, specifically so a UI can decide when to offer the `/prescribe/manual-preview` fallback:

```
HTTP/1.1 502 Bad Gateway
X-Error-Code: AI_SERVICE_UNAVAILABLE
```

```json
{ "detail": "AI parsing failed" }
```

This header is set **only** when the underlying failure was Gemini being unreachable, rate-limited, or timing out. It is deliberately **not** set on the other `502` (a de-tokenization failure) or on the `422`/`500` branches of the same code path — those are not "the AI is down" problems that manual entry routes around the same way, and a caller that doesn't recognize the header can safely ignore it and treat the response as a plain `502` exactly as v1 already describes.

---

## A3. What Phase A's A1 milestone changed underneath v1 — and what it didn't

A1 (Persistent Preview State) replaced the in-memory preview store (`_PENDING_PREVIEWS`/`_CONSUMED_PREVIEWS` dicts, which did not survive a process restart and were not shared across worker processes) with a real database table (`prescription_previews`, migration 003). This is an internal robustness change, not a contract change:

- **No request or response JSON shape changed.** Every field in v1 §1 and §2 is unchanged.
- **No status code changed** and no new status code was introduced by A1 itself (§A1/§A2 above predate A1 and are documented here only because they'd never been written down).
- **What got strictly stronger, not different:** v1 §2 already promised inventory deduction is atomic and a preview is consumed only after a successful commit. Under the old in-memory store, that promise held for a single crash-free process, but a crash between the in-memory dict's mutation and the database commit could still leave the two out of sync. A1 folds the preview's PENDING→CONFIRMED transition into the *same* database transaction as the inventory deduction and prescription insert — they now commit or roll back together, closing that gap. A doctor-visible behavior is unchanged; what changed is that the guarantee now survives a process crash or restart, which it was always documented as providing but couldn't previously enforce.
- **`preview_id` generation**: v1 §1 already specified "opaque, unguessable, nothing encoded into it." A1 implements this with `secrets.token_hex(8)` (64 bits of CSPRNG entropy) as a real database primary key, so a collision now raises instead of silently overwriting another patient's preview (astronomically unlikely at this entropy, but no longer even theoretically possible to hit silently).

Nothing in A1 required a v1.2 addendum on its own — it's mentioned here because the user asked A1's report to fold in the standing v1.1 addendum this document already owed from the manual-preview/`X-Error-Code` additions.

---

## Not yet covered (upcoming Phase A milestones, not this addendum)

`Idempotency-Key` (A4), real session tokens replacing the current "whatever `Authorization` header was present, if any" placeholder (A2), and RBAC roles (A3) are all still to come and will get their own addendum entries when built — each is additive to what's here, per the same rule v1 states: additive-but-flagged, never a silent rewrite of a locked shape.
