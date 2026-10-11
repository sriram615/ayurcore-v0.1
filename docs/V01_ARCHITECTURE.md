# AyurCore v0.1 — architecture and the AI / deterministic boundary

Scope: what the v0.1 demo contains and why it is built this way. For the original backend design (confirm flow, idempotency, audit chain, migrations, backups) see `docs/ARCHITECTURE_BASELINE.md`; for the HTTP contract see `AyurCore_API_Contract.md` (v1.7).

## 1. System view

```
Browser (Next.js, frontend/)  ──HTTP, Bearer session token──▶  FastAPI (backend/main.py)  ──▶  SQLite (WAL)
   patient search · history                                      require_role(PERMISSION_MATRIX[...])          clinic-scoped tables
   Ask panel · Dashboard                                         clinic comes from the session, never          append-only audit chain
                                                                 from the request
                                                                      │
                                      POST /patients/{pk}/ask         ▼
                                      (DOCTOR only, read-only)   ai2.adapters.clinic_stores  ── one read transaction ──▶ PatientSnapshot (immutable)
                                                                      │
                                                                      ▼
                                                                 ai2.Ai2Service  ── no database, no network, no LLM
```

## 2. The boundary: what is AI, what is deterministic

| Layer | Deterministic code | Learned / statistical part |
|---|---|---|
| Auth, RBAC, clinic isolation, audit chain, confirm, stock | everything | none |
| AI-2 question handling | normalisation, scope and safety screen, closed vocabulary (herbs, doshas, dates, relative terms), reference resolution, query execution, verification, wording templates | a small **planner** (pinned by SHA-256) that ranks readings of an ambiguous question; it can only choose among readings the rules already produced |
| AI-3 brief | fact extraction, verification, sentence templates, the "not advice" caveat | a **logistic-regression salience ranker** (pinned by SHA-256) that selects and orders already-verified facts; it cannot create or reword a fact |
| AI-1 extractor | assembly and verification | tagger (evaluated, not wired in) |
| `/prescribe/preview` | validation, PII tokenising | Gemini drafts a prescription; the doctor must review and confirm |

Design rules that follow from this:

1. **The AI cannot write.** AI-2 and AI-3 read an immutable snapshot; the only clinical write in the product remains `POST /prescribe/confirm`, which makes no AI call.
2. **Every claim is re-verified against the records.** An answer or brief fact is shown only if an independent verifier can reproduce it from the cited records (AI-3 additionally re-checks numbers and herb names in the sentence it displays). A fact that fails is dropped, not softened.
3. **Unknown stays unknown.** A prescription whose herbs or dosha were not recorded is reported as incomplete; nothing is inferred or filled in.
4. **Refuse rather than guess.** Dose or treatment advice, requests about other patients, off-vocabulary questions and over-long questions get a categorised refusal; ambiguous questions get a clarification with explicit readings, and the chosen reading is validated server-side against what AI-2 itself offered (a tampered choice is a 422).
5. **Access is decided before data is read.** The role check (DOCTOR for `/ask`) and clinic scoping come first; a patient of another clinic returns the same 404 as a missing one. The A6 evaluator (`scripts/eval_actor_aware.py`) checks this against the shipping route.
6. **No external calls.** AI-2, AI-3 and the demo make none; local pinned files are the only inputs.

## 3. AI-2 request path

`normalize → scope/safety screen → interpret (closed vocabulary) → [clarify if several readings] → plan → execute on the snapshot → verify → answer or categorised refusal`

- Result categories: `answer` (verified, exact), `incomplete` (verified but limited by unknown or partial records), `clarify` (1–3 readings to choose from, never an answer), `missing` (the history holds nothing for it), `unsupported`, `unsafe` (advice, treatment, diagnosis, instruction injection), `access_denied` (other patients or the whole clinic).
- **Snapshot consistency:** the adapter reads prescriptions and encounters in one deferred read transaction (`PRAGMA query_only`), rejects duplicate records, and any failure is a 503 `MEMORY_SERVICE_UNAVAILABLE` — never a partial answer.
- **Provenance:** `ai2/PROVENANCE.json` ties each production file to the evaluated module it derives from; `memory/` holds those evaluation modules and a test enforces the link.
- **Logging:** who asked and the outcome class only — no question text, patient identity or record content.

## 4. AI-3 brief path

`snapshot → candidate facts (10 kinds, e.g. last prescription, open visit, dosha changed) → verification → salience ranking → top 5 → fixed sentences + caveat`

Two arms are built from the same verified facts: a fixed **template order** (the baseline, fixed before any model existed) and the **learned** order. The ranker is trained on synthetic patients with labels from a written reference policy (`ai3/synthetic.py`), so it demonstrates the method and the safety properties, not clinical judgement. The brief is a library and demo-script feature in v0.1; it has no HTTP endpoint (adding one is a separate, approval-gated change).

## 5. Frontend

`frontend/` (Next.js 16) is the demo surface. The Ask panel validates every reply against the contract before drawing it (`lib/ask-view.ts`; a reply that breaks the contract is refused, never repaired) and words it in `lib/ask-present.ts`. Explicit nulls render as "not recorded", evidence is shown as the cited records, and refusals show a category only — never a backend detail. Mocked specs (`e2e/`) cover the states; `e2e-real/` drives the built UI against the real demo backend.

## 6. Key decisions and trade-offs

| Decision | Reason | Cost |
|---|---|---|
| Rules + tiny pinned models instead of an LLM | Reproducible, auditable, no PHI leaving the machine, verifiable answers | Closed vocabulary; unusual phrasing is refused or clarified |
| `/ask` is DOCTOR-only (provisional, D-02) | Conservative for clinical history | Differs from the registered A6 ADMIN control; a protocol v1.2 is proposed |
| Disposable demo DB inside the checkout with path guards | The demo can never open the application database | Slightly more setup |
| Tests run only in a scratch sandbox built from tracked files | The suite once wrote into a real database; the restricted acceptance file must stay unopened | Not a plain `pytest` |
| Evaluation artifacts pinned by hash; protocol files immutable | Prevents silent re-tuning | Changes need a new protocol version |

## 7. Known limits

Single-clinic-process demo; SQLite; no rate limiting anywhere in the backend; planner and ranker trained on synthetic data; question length capped at 300 characters / 60 words (API contract §6A); Malayalam and Manglish coverage is limited to the closed vocabulary and has had no independent native-speaker validation; the AI-1 extractor is not part of any request path.
