# AyurCore — Locked API & Data Contract (v1)

**Date locked:** September 13, 2026
**Amended:** September 13, 2026, after an architecture review caught a real bug (a preview was being consumed before knowing whether confirm would succeed, so a doctor hitting a 409 couldn't retry) and several places where the written contract's wording was stricter/vaguer than what the code actually does. The JSON shapes below are unchanged; six guarantees are reworded or added — see the callouts marked "(amended after review)" or "(new)".
**Why this exists:** you and your teammate are each building a full, separate MVP and merging them later rather than splitting the work by layer. That only avoids becoming a rewrite if both of you build against the exact same request/response shapes and schema from day one. Once this is sent, treat it as fixed — if either of you needs to change something here, say so and re-sync before building further on top of the old shape, don't just quietly diverge.

## What's locked here

The two clinical endpoints (`/prescribe/preview` and `/prescribe/confirm`), the session token shape, `clinic_id` as a field everywhere it belongs, the `corrections` table, and the WhatsApp hand-off shape. Everything else — how voice becomes text, what the UI actually looks like, how either of you deploys — is intentionally not locked. Build those however's fastest; they don't touch this contract.

---

## 1. `POST /prescribe/preview`

Runs the entire AI step. Makes exactly one call to Gemini. Writes nothing to the database — no inventory deduction, no prescription record. Safe to call as many times as needed while a doctor is reviewing or re-recording.

**Request**

```json
{
  "patient_name": "string, required",
  "patient_phone": "string, optional",
  "patient_id": "string, optional",
  "notes": "string, required — raw clinical notes, from voice transcript, typing, or read-aloud paper notes",
  "clinic_id": "string, optional, default \"default_clinic\""
}
```

**Response — 200**

```json
{
  "preview_id": "string — opaque handle; pass this unchanged to /confirm",
  "patient_name": "string — echoed back exactly as sent",
  "dosha_imbalance": "string",
  "prescribed_herbs": [
    { "name": "string", "grams": "integer, > 0" }
  ]
}
```

**Error responses**
- `400` — a prescribed herb isn't in the clinic's known herb list
- `500` — the internal PII safeguard aborted the request *before* any AI call was made (this is the safeguard working correctly, not a bug)
- `502` — the AI call itself failed, or its response couldn't be safely de-tokenized

**Guarantees**: the patient's real name, phone, and ID are tokenized locally, verified absent from the outgoing text by a pre-flight check, and restored locally after the AI responds — they are never sent to Gemini. `preview_id` expires after 30 minutes of not being confirmed.

**On `preview_id` (amended after review):** it is an opaque, unguessable lookup key only — nothing is encoded into the string itself. All the state it refers to (patient identity, `clinic_id`, the AI's exact proposal) lives server-side, in a store keyed by `preview_id`. `/preview` also records whichever `Authorization` header (if any) made the call, so `/confirm` can require the same one later — see the note on session binding below.

---

## 2. `POST /prescribe/confirm`

Makes **no AI call of any kind**. Everything past this point is deterministic code. This is the only endpoint that touches inventory or writes a real prescription record.

**Request**

```json
{
  "preview_id": "string, required — from a prior /preview call",
  "dosha_imbalance": "string, optional — doctor's correction; omit to accept the AI's proposal as-is",
  "prescribed_herbs": "array, optional — doctor's correction; omit to accept the AI's proposal as-is"
}
```

**Response — 200**

```json
{
  "patient_name": "string",
  "dosha_imbalance": "string — final, as confirmed",
  "prescribed_herbs": [
    { "name": "string", "grams": "integer" }
  ],
  "prescription_id": "integer",
  "created_at": "ISO 8601 timestamp"
}
```

**Error responses**
- `400` — a doctor's corrected herb name isn't recognized
- `403` — this `preview_id` was created under a different authenticated session (see session binding, below)
- `404` — `preview_id` not found or expired; the UI should send the doctor back to re-run `/preview`
- `409` — either insufficient stock for one or more herbs (nothing was deducted), *or* this `preview_id` was already successfully confirmed once before (see single-use, below) — the error message distinguishes the two

**Guarantees (amended after review):**
- Inventory deduction is atomic (a prescription either fully succeeds or leaves no partial trace).
- **Single-use**: a `preview_id` can only be successfully confirmed once. It is marked consumed only *after* the prescription commits — a failed attempt (400 unknown herb, 409 insufficient stock) leaves it untouched, so the doctor can correct and retry the *same* `preview_id` without a new `/preview` call (and a new AI call). A second confirm attempt on an already-*successful* `preview_id` gets `409`, never a second prescription — this matters once double-clicks or browser retries are possible.
- **Session binding**: if `/preview` was called with an `Authorization` header, `/confirm` must present the same one, or gets `403`. A `preview_id` created with no session recorded (true today, since there's no login system yet) accepts a confirm from any session — this activates automatically the day real auth exists, no code change needed.
- **Corrections logging is best-effort, not guaranteed**: every *successful* `/confirm` *attempts* exactly one write to `corrections`, after the prescription has already committed. A logging failure is caught and logged separately — it can never roll back or block the prescription. (Reworded from the original wording, which implied a stronger guarantee than two separate writes can actually make.)
- The **original AI proposal** (not a value reconstructed from what the frontend sends) is what `corrections.ai_dosha_imbalance` / `ai_prescribed_herbs_json` record — it's read back from the same server-side preview state, so a doctor's correction can never overwrite what the AI actually said.
- AI-proposed and doctor-corrected herb names resolve through the **exact same canonical resolver** (`normalize_herb`) — an AI's "Turmeric" and a doctor's "turmeric powder" both resolve identically, and a genuinely unknown herb still `400`s whether the AI or the doctor named it.
- Doctor corrections get the **same Pydantic validation** as AI output (`grams` > 0 and ≤ 10000, integer only) — `ConfirmRequest.prescribed_herbs` reuses the identical `PrescribedHerb` model `/preview`'s response does, deliberately, not a looser one.

---

## 3. Session / auth

Server-side opaque session token (not a JWT the frontend decodes itself). However you build your login screen, treat the token as an opaque string to store and replay — don't parse it. Header: `Authorization: Bearer <token>`. This matters at merge time only if one of your logins ends up wired to the other's backend.

## 3a. Session binding on previews (new)

Neither of you has built real login yet — that's still separate, planned work. But the *hook* for it is already wired into `/preview` and `/confirm`: whatever `Authorization` header a `/preview` call carries (if any) is what a later `/confirm` for that same `preview_id` must match. Build your login however you like; the day it exists and starts sending a real session token in that header, this starts being enforced with no backend change required. Until then, it's inert (no header sent = no check performed).

## 4. `clinic_id`

A plain string field, defaulting to `"default_clinic"`, present on `inventory`, `prescriptions`, and `corrections`. Not used for real multi-tenant routing yet — there's one clinic — but every write path threads it through now so activating a second clinic later doesn't require touching this layer again.

## 5. `corrections` table (the data flywheel)

Not exposed as its own endpoint yet — this is what `/confirm` writes internally on every call:

| column | type | notes |
|---|---|---|
| `id` | integer | autoincrement |
| `prescription_id` | integer | FK to `prescriptions.id` |
| `clinic_id` | text | |
| `ai_dosha_imbalance` | text | what the AI proposed |
| `ai_prescribed_herbs_json` | text | what the AI proposed |
| `confirmed_dosha_imbalance` | text | what actually got confirmed |
| `confirmed_prescribed_herbs_json` | text | what actually got confirmed |
| `was_changed` | integer (0/1) | whether the doctor changed anything |
| `created_at` | timestamp | |

Deliberately **no patient name, phone, or ID column** — the data flywheel only ever needs to know what was proposed vs. confirmed, never who. Keep it that way as this table grows; it's tempting but unnecessary to add patient identity here later "for context," and doing so would quietly widen the system's PII footprint for no real analytical benefit.

## 6. WhatsApp hand-off shape

Not an endpoint — this is the shape a `/confirm` result should be handed to, whoever wires the actual sender:

```json
{
  "to_phone": "string — patient_phone from the original /preview request",
  "patient_name": "string",
  "dosha_imbalance": "string",
  "prescribed_herbs": [ { "name": "string", "grams": "integer" } ],
  "clinic_id": "string"
}
```

---

## Not locked, build freely

How audio/typed/paper input becomes the `notes` string before it reaches `/preview`. The UI's actual screens and flow. Deploy target and hosting. None of it needs to match between your two builds for the merge to work — only the shapes above do.
