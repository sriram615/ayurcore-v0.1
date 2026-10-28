# AyurCore v0.1 --- AI-First Portfolio Master Plan

**Status:** Master project contract\
**Purpose:** Resume-ready AI/ML portfolio project\
**Data:** Synthetic only\
**Primary objective:** Build and rigorously evaluate an AI/ML system,
not merely an SDE application with an LLM API.\
**Last updated:** 2026-10-06 --- AI-1 extractor path complete and
frozen (see §4A); the Gemini pilot path was abandoned.
2026-10-10 --- AI-2 working-tree status and Defect Fix 2 closure recorded
under Block 7 (§18).

**Remaining-work roadmap:** `documentation/docs/PORTFOLIO_READINESS_ROADMAP.md` is the
authoritative record of what remains until v0.1 is portfolio-ready, its
order, and how each item is verified. This plan stays the project
contract; where they disagree, the roadmap's §F.4 lists the discrepancy.

## 1. Non-negotiable framing

`<repo>` is a **resume/portfolio project**,
developed separately from the actual clinic work on the other branch.

It is not a clinic production system and must not use real patient data.

All benchmark results are **synthetic technical results, not clinical
validation**.

The SDE foundation remains important, but the primary portfolio story is
AI/ML.

Target positioning:

> An AI-first clinical workflow system that extracts structured
> prescriptions from multilingual/code-mixed consultation notes,
> supports grounded longitudinal patient reasoning, and evaluates AI
> behavior through reproducible synthetic benchmarks, evidence
> verification, adversarial testing, and human-in-the-loop workflows.

## 2. AI capabilities

### AI-1 --- Notes → Structured Prescription --- FLAGSHIP

Input: free-text English, Malayalam, Manglish/code-mixed consultation
notes.

Output: structured `ClinicalParse` containing dosha, prescribed herbs,
total grams, and mentioned-but-not-stocked herbs.

Phenomena: - aliases; - Malayalam script; - Manglish; - misspellings; -
negation; - self-correction; - substitutions/"instead of"; - shared
quantities; - units; - distractors; - unstocked herbs.

Compare: 1. deterministic baseline; 2. Control; 3. A0 bounded agent.

**Status (2026-10-06):** the Control/A0 (Gemini) comparison was
abandoned (§4A). The implemented and evaluated AI-1 path is the local
trained extractor: T1 learned tagger → T2 deterministic assembly → T3
deterministic verification with R1--R5 context safety checks.

### AI-2 --- Consultation Memory

Doctor asks questions about a selected patient's recorded history.

Architecture:

``` text
Question
→ typed query plan
→ deterministic executor
→ evidence
→ grounded claims
→ verifier
→ cited answer
```

Closed query vocabulary only: `first`, `last`, `count`, `sum`, `list`,
`diff`, `compare`, and explicitly justified text lookup.

No arbitrary SQL from the model.

### AI-3 --- Consultation Brief

Generate a concise pre-consultation context brief.

Deterministic software computes facts; AI selects salient information
and phrases it; verifier checks claims.

Never infer diagnosis, treatment rationale, outcomes, allergies, or
dosing details that are not recorded.

## 3. Deterministic vs AI boundary

### Deterministic

RBAC, clinic/patient scope, SQL, retrieval, formulary lookup, dates,
ordering, intervals, counts, sums, differences, timeline construction,
query execution, verification, prescription confirmation, inventory,
audit, idempotency.

### AI

Free-text extraction, multilingual interpretation, semantic query
planning, salience selection, grounded phrasing, and measured abstention
decisions.

**Rule: the model plans and phrases; software computes and verifies.**

## 4. S1 benchmark

Current S1: - 600 synthetic cases; - 360 train / 120 dev / 120 test; - 8
categories: clean, alias, malayalam_script, manglish, negation,
distractor, units, mixed_hard; - deterministic generation; - gold labels
known by construction; - fingerprint:
`b6ccaca65013a4b32ff3dc3827a8cdaf58ba29671072fd84c126edd4eee7dd19`.

All benchmark data is synthetic.

### Current deterministic reference

Frozen test reference: - complete-case pass: **74.2% (89/120)** - herb
precision: **96.9%** - herb recall: **90.9%** - herb F1: **93.8%** -
exact quantity: **96.4%** - dosha exact: **100%** - edits: **47**

Difficult slices: - negation: **13.3%** - units: **80.0%** - mixed_hard:
**6.7%**

This baseline is a **reference point, not an unbiased final estimate**,
because baseline behavior and hard phenomena were inspected during S1
construction. Do not tune or redesign the baseline from this result.

## 4A. AI-1 local extractor --- final state (2026-10-06)

**All results in this section are synthetic technical evaluation, not
clinical validation.** Every evaluation set was written by the same AI
assistant that designed the extractor and its rules; per-category
samples are small (6--12 cases).

### Why the Gemini pilot was abandoned

The Control pilot failed on free-tier quota: 20 requests per day per
model were enforced for both `gemini-3.6-flash` and `gemini-3.8-flash`.
Paid APIs were not authorized. A 20-case three-arm pilot with repeated
runs was therefore infeasible, and the user stopped Gemini as the
primary path. Control and A0 remain in the codebase but have no
evaluation results.

### Architecture (frozen)

``` text
note
→ T1  averaged structured perceptron, BIO span tagger (numpy), trained on S1 train only
→ T2  deterministic assembly: units, herb/dosha normalisation, linking rules L1–L12
→ T3  deterministic verifier: V1–V9 against the tagger output and provenance,
      then context checks against T3's own reading of the note:
      R1 conflicting / foreign quantities, R2 alternatives, R3 historical context,
      R4 unresolved status / negation (hard rejections); R5 unaccounted dose (warning only)
→ verified ClinicalParse, or ABSTAIN (rejection = abstention; T3 never repairs)
```

- T1 weights sha256 `e0dfaaee…`.
- T3 v3: `verify.py` `f5096345…`, `verify_context.py` `6856be82…`.
- T3 imports nothing from T2's assembler.
- T2 rule L6 was written with a known S1-dev failure visible; this is
  disclosed.

### Results

| Set | Status | Result |
|---|---|---|
| S1 train/dev | development (in-distribution; dev inspected) | T2 output pass 360/360 and 120/120 with T1 labels; R1--R4 false rejections 0 (gold and T1 labels) |
| S1 test | **not evaluated with the extractor** | remains a valid holdout (Block 10) |
| S1-OOD v1 (62, fp `7415ffa2…`) | **spent** | T2 pass 44/62; 4/74 emitted facts confident errors; T3 (pre-R1--R5) rejected 0 |
| S1-OOD v2 (73, fp `48b7c2ff…`) | **spent** | T3 + R1--R4: confident errors 40 → 12 (28 prevented, 70%); 6/36 correct outputs falsely rejected (16.7%) |
| S1-OOD v3 (62, fp `0d8e9f9a…`) | **spent; final extractor cycle** | T3 v3: **24/29 confident errors prevented (83%)**; **4/35 correct outputs falsely rejected (11.4%)**; final confident errors 5/40 emitted facts (12.5%); clean controls rejected 0/10; correct abstentions 13/17; 11 correct facts withheld with rejected outputs |

- The v1/v2/v3 sets differ in content and difficulty, so comparisons
  between them are descriptive only.
- **Raw and graded outputs:** `artifacts/models/ood_eval`,
  `ood_v2_eval` and `ood_v3_eval`, with run metadata and hashes.

### Remaining failure modes (semantic, from v3)

- **Disagreeing sources with no correction cue** ("40 g per the letter,
  60 g per the card"). R1's "later amount wins" accepts the second value.
- **Substitution or cancellation verbs outside the closed word lists**
  ("swap to …", "drop Haridra").
- **Third-party medication** ("her mother takes …") read as a conflicting
  amount.
- **Instruction-only negation** ("not to be mixed with …") read as "do not
  dispense".
- **A later historical amount** that T1 does not label as unrelated.
- **Provenance pointing at a historical mention.**
- **T1 recognises rare labels poorly** on out-of-distribution phrasing:
  past, stopped, replaced, corrected and unstocked items.

### Decision

- **Rule-based iteration on the extractor is stopped.**
  - The remaining failures need understanding of meaning, not more
    closed word lists.
  - Further word patches would be tuning to spent holdouts.
- **S1-OOD v1--v3 must not be re-run, re-scored or used for tuning.**
- **Any future AI-1 work must be a learned improvement or an
  escalation/reasoning layer** (T1 rare-label generalisation, for
  example). It must be measured on a new, preferably independently
  written, holdout.

## 5. Evaluation protocol

Mandatory sequence:

``` text
Fresh baseline
→ small Gemini pilot
→ pilot error analysis
→ freeze AI configuration
→ full dev evaluation
→ ablations/error analysis
→ freeze final configuration
→ one final test evaluation
→ final report
```

### Test-set rule

`s1_test.json` is a frozen holdout.

After the current disclosed construction caveat, do not use it to: -
tune prompts; - tune model settings; - modify rules; - modify the
generator; - select models; - repeatedly iterate.

If a final configuration changes after test evaluation, the old test
result is no longer a valid final result.

**AI-1 as executed (2026-10):**
- The Gemini steps were replaced by: local T1 training on S1 train →
  development on S1 dev → three one-shot out-of-distribution holdouts
  (S1-OOD v1, v2, v3).
- Each holdout was evaluated exactly once with its own evaluator lock.
- `s1_test.json` has not been used to evaluate the extractor.

## 6. Immediate next experiment

**ABANDONED (2026-10-06):** the Gemini pilot below could not run within
free-tier quota (§4A). It is kept for the record. Choosing the next
block needs the user's decision. AI-2/AI-3 as specified assume an LLM,
so their model source must be settled first.

Run a **20-case dev pilot** before the full evaluation.

Select difficult/representative cases emphasizing: - negation; -
self-correction; - substitutions; - shared quantities; - mixed-hard; -
Manglish; - units.

Run: 1. baseline; 2. Control; 3. A0.

Record: - pass; - herb P/R/F1; - quantity accuracy; - dosha; -
wrong/hallucinated herbs; - edits; - latency; - provider failures; -
per-case outputs.

Do not tune from the pilot before reviewing failures.

If A0's stock/tool context can alter extraction, stop and isolate that
confound before running the experiment.

## 7. Metrics

### Extraction

Pass rate, herb precision/recall/F1, exact quantity, absolute gram
error, dosha accuracy, hallucinated/wrong herb rate, distractor
acceptance, correction burden.

### Planning/retrieval

Plan exact match, field accuracy, gold-record recall, Recall@k/MRR when
applicable.

### Grounding

Fact-support rate, hallucination rate, unsupported claims,
verifier-dropped claims.

### Citations

Citation precision, citation recall, invalid-label rate.

### Abstention/safety

Abstention precision/recall, out-of-scope refusal, treatment-advice
refusal, prompt-injection success, cross-patient leakage.

### Operations

p50/p95 latency, tokens, cost, provider failure rate.

Use repeated runs and confidence intervals for nondeterministic AI.

## 8. Grounding and verification

Model output must be structured, conceptually:

``` text
claims[]
  text
  facts[]
  sources[]
```

Verifier rules: - citations must belong to the current request; -
structured facts must match deterministic results; -
dates/quantities/herbs must match; - quoted note text must match the
cited note; - unsupported claims are rejected/dropped; - no evidence
means abstention.

The model must never invent source labels or obtain arbitrary database
access.

## 9. Consultation Memory

Use:

``` text
Doctor-selected patient
→ authorization
→ read-only PatientSnapshot
→ deterministic Timeline
→ question
→ typed planner
→ deterministic executor
→ evidence
→ grounded claims
→ verifier
→ cited answer
```

Patient and clinic scope always come from authenticated server context.

## 10. Consultation Brief

Use:

``` text
PatientSnapshot
→ deterministic timeline/facts
→ AI salience selection + phrasing
→ verifier
→ brief
```

Maintain a deterministic template brief as the baseline.

Compare template vs AI brief.

## 11. Agent/framework policy

### Direct `google-genai`

Preferred initially. Use explicit orchestration and structured Pydantic
outputs.

### LangChain

Do not add for appearance. Only introduce if a concrete architectural
need cannot reasonably be handled explicitly.

### LangGraph

Do not add yet.

Consider it only if the system develops genuinely stateful,
checkpointed, resumable, human-paused workflows such as:

``` text
draft → doctor review → revise → re-retrieve → re-verify → approve → resume later
```

Framework choice must follow a real AI/workflow requirement.

Do not add: - generic chatbot; - unnecessary RAG/vector DB; -
fine-tuning on insufficient data; - multi-agent systems; - autonomous
clinical recommendations; - autonomous prescription confirmation.

## 12. Human-in-the-loop

Existing flow:

``` text
AI proposal → doctor correction → confirmed prescription
```

Use correction data for evaluation such as acceptance rate, correction
burden, and correction types.

Do not automatically treat historical doctor corrections as clinical
ground truth for research claims.

Synthetic gold labels are the primary S1 benchmark ground truth.

## 13. Dataset and reproducibility rules

Every dataset needs: - version; - seed; - fingerprint; - frozen split; -
generation method; - gold definition; - limitations.

Every AI result must record: - dataset fingerprint; - model; - prompt
version/hash; - code version; - configuration; - timestamp; -
provider/version when available; - cost/latency.

Keep raw model outputs separate from graded results.

## 14. Required AI evaluation datasets

### S1

Notes → prescription benchmark.

### S1-OOD v1 / v2 / v3

Hand-written out-of-distribution and safety holdouts for AI-1. Each has
a manifest, a fingerprint, a README and integrity tests. All three are
frozen and **spent**: v1 (62 cases), v2 (73), v3 (62, final). See §4A.

### S2

Synthetic longitudinal patient histories, generated through the real
API.

### S3

Synthetic Memory questions with gold answers and source labels computed
by deterministic execution.

### S4

Adversarial/abstention set: - not-recorded questions; - treatment
advice; - diagnosis requests; - prompt injection; - cross-patient
attempts; - malformed outputs.

### S5

Small author-annotated sample, explicitly labelled non-clinical.

## 15. AI experiments

**Status (2026-10-06):**
- Experiments A and B (Control/A0) were not run: Gemini was abandoned.
- For AI-1, Experiment C was realised as "T2 output before
  verification vs final output after T3", on S1-OOD v2 and v3 (§4A).

### Experiment A

Baseline vs Control vs A0.

Question: does AI beat deterministic rules, and does bounded agentic
behavior beat a simpler AI?

### Experiment B

Agentic value.

Measure whether A0 specifically improves difficult cases over Control.

### Experiment C

Verification.

Compare generation with and without verifier for unsupported claims,
hallucination, citation quality and coverage.

### Experiment D

Ablations where justified: - planner vs no planner; - verifier on/off; -
Control vs A0; - model variants; - template vs AI brief.

## 16. Adversarial AI testing

Test: - prompt injection inside notes; - instructions embedded in
clinical text; - treatment advice; - diagnosis; - unsupported causal
reasoning; - hallucinated history; - fabricated citations; - unknown
source labels; - cross-patient attempts; - malformed structured
output; - missing records; - ambiguous questions; - multilingual
injection.

Target for cross-patient leakage and injection success: **0**.

## 17. Portfolio/resume evidence standard

Never claim: - clinical validation; - clinical efficacy; - clinical
safety validation; - real patient deployment.

Valid claims must be traceable to artifacts.

Examples:

> Built and evaluated an LLM-based clinical information extraction
> system on a frozen synthetic benchmark.

> Improved herb-level F1 from X to Y over a deterministic baseline on
> code-mixed notes.

> Implemented grounded longitudinal patient QA with typed query planning
> and automated citation verification.

> Reduced unsupported generated claims from X% to Y% using deterministic
> post-generation verification.

Traceable AI-1 claim (synthetic, author-written holdout; artifacts in
`artifacts/models/ood_v3_eval`):

> On a frozen 62-case synthetic out-of-distribution safety benchmark, a
> deterministic verifier with context safety checks prevented 24 of 29
> confident extraction errors (83%) by abstaining, while falsely
> rejecting 4 of 35 correct outputs (11.4%).

## 18. Implementation roadmap

### Block 1 --- S1 benchmark

**DONE**

600 cases, baseline, fingerprints, reproducibility.

### Block 2 --- evaluation isolation

**DONE**

v0.1 protected from clinic checkout/database contamination.

### Block 3 --- fresh baseline

**DONE**

74.2% pass, 93.8% herb F1, 96.4% quantity exact.

### Block 4 --- Gemini pilot

**ABANDONED** (free-tier quota; no paid APIs authorized; see §4A)

20 difficult dev cases, baseline vs Control vs A0.

### Block 4A --- AI-1 local extractor

**DONE (frozen 2026-10-06)**

- T1 trained span tagger.
- T2 deterministic assembly.
- T3 verifier V1--V9.
- R1--R5 context safety checks, integrated as T3 v3.
- S1-OOD v1, v2 and v3 one-shot evaluations; all three spent.
- Rule-based iteration stopped. See §4A.

### Block 5 --- full development evaluation

After pilot validity: - complete dev; - repeated runs; - metrics; -
confidence intervals; - cost/latency; - error taxonomy.

For AI-1, superseded by Block 4A: S1 dev development plus out-of-
distribution error taxonomies. T1 is deterministic at inference, so
repeated runs do not apply.

### Block 6 --- AI improvement/ablations

Only based on observed failures; never tune against test.

For AI-1, done within Block 4A: R1--R5 were designed from the v1 and v2
failure analyses, and each change was measured on a fresh holdout.

### Block 7 --- Consultation Memory

Snapshot, timeline, typed planner, executor, claims, verifier,
abstention, S2/S3/S4 evaluation.

**Status (2026-10-10): NOT marked done.** The following exists in the
v0.1 working tree, uncommitted; the evaluation protocol is not finished.

- AI-2 is a local, deterministic, read-only service (package `ai2/`);
  it uses no LLM.
- `POST /patients/{patient_pk}/ask` is DOCTOR-only. This is provisional:
  whether ADMIN may ask is an unresolved product decision (the registered
  longitudinal protocol's actor-aware control assumes ADMIN is not denied).
- A Patient History "Ask about this patient" panel exists in the frontend.
- Longitudinal protocol v1.1: the latest development run is dev-run-09
  (A gates held). No `fix_freeze` entry, no validation run and no accept
  run exist; the actor-aware evaluator is not built. The accept set is
  unopened.

**Defect Fix 2 --- Implementation complete; targeted browser verification
passed; full-suite and real-backend verification outstanding.**

*Defect.* For a patient whose prescriptions all have unknown herb or
dosha data, the backend correctly answers a list question with an
incomplete answer and an empty list; the frontend presenter refused it
("That answer couldn't be shown").

*Implementation (synthetic data only).*

- Block A: the presenter accepts an empty incomplete herb or dosha list
  only when every cited record is a unique prescription whose relevant
  field is unknown and the qualifier is `partial`. It shows "Not known",
  an Incomplete badge and a caveat stating that the empty list is not a
  record that nothing was prescribed. Files: `frontend/lib/ask-present.ts`
  plus a new spec and fixture.
- Block B: evidence field-presence hardening. `explicitNull` is derived
  from the raw reply (`raw.herbs === null`, `raw.dosha === null`), so an
  absent or undefined field is no longer treated as unknown. Files:
  `frontend/lib/ask-view.ts`, `frontend/lib/ask-present.ts` and a new
  spec.
- Browser verification added one file only:
  `frontend/e2e/patient-ask-empty-partial.spec.ts`. The earlier
  three-file implementation is unchanged.

*Evidence (two kinds, kept apart).*

- Shim results: 319 pure-function tests passed through an isolated
  scratch runner that re-implements the `test`/`expect` matchers used.
  It is not Playwright. A negative control against the unedited code
  failed the tests that describe the defect.
- Independent TypeScript check (`tsc --noEmit`, strict): passed.
- Real Playwright results: 7 targeted tests passed in the second run,
  including a 390 px layout test (Chrome, one worker, no retries, fresh
  production build in scratch). The first run failed all 7 at its final
  unexpected-request guard because the mock omitted `GET /inventory`;
  the mock was corrected without weakening any assertion or timeout, and
  the subsequent run passed.
- Screenshots confirmed the expected Incomplete badge, caveat and
  evidence cards for both empty-list answers. Mixed-history and exact
  non-empty list scenarios passed.
- Isolation: synthetic fixtures and mocked API responses only. No real
  backend, clinic database or acceptance-set material was used. Network
  was confined to loopback (server under a sandbox profile; browser
  host-resolution restricted and every non-loopback request aborted and
  counted).

*Outstanding (not claimed).* Full frontend-suite verification;
real-backend end-to-end verification; other browsers; accessibility
testing; the four earlier-timeout specs (`patient-ask-clarify.spec.ts`)
were not re-run. This is not production-ready, not fully verified and
not clinically validated.

### Block 8 --- Consultation Brief

Template baseline, AI salience/phrasing, verifier, comparative
evaluation.

### Block 9 --- Robustness

Injection, multilingual adversarial testing, unsupported inference,
malformed outputs, cross-patient attacks.

### Block 10 --- final frozen evaluation

Freeze model, prompt, code and configuration; run final test once.

For AI-1, the extractor is frozen and `s1_test.json` has not been used
with it; the single final S1 test run remains available.

### Block 11 --- portfolio packaging

Final evaluation report, architecture diagram, dataset/model cards,
README, demo, limitations, reproducibility instructions, resume bullets.

## 19. Definition of done

v0.1 is portfolio-ready when: 1. AI problems are clearly defined. 2.
Deterministic baselines exist. 3. AI systems are compared against them.
4. Agentic behavior is justified by measured value. 5. Memory is
grounded and evaluated. 6. Brief is compared with a deterministic
baseline. 7. Generated claims are verified. 8. Abstention is measured.
9. Adversarial tests exist. 10. Dataset/model/prompt versions are
recorded. 11. Results are reproducible. 12. The final test remains a
valid holdout. 13. Limitations are explicit. 14. No clinical claims are
made from synthetic data. 15. The system can be defended as an AI/ML
project without relying on framework names.

## 20. Claude Code operating rules

**Before every new v0.1 implementation block, Claude MUST read this
document.**

Claude must: - work only in `<repo>`; - work
only on the current block; - report evidence, not assumptions; - use
isolated tests; - preserve the clinic branch; - avoid real patient
data; - avoid live clinic databases; - avoid unapproved migrations; -
avoid unnecessary dependencies; - avoid framework-driven architecture; -
never silently expand scope.

Claude must not: - implement future blocks early; - modify the clinic
branch; - optimize against the test set; - install packages without
explicit authorization; - commit/push without explicit authorization; -
claim clinical validation; - add LangChain/LangGraph solely for resume
value.

If a request conflicts with this document, **stop and report the
conflict instead of improvising**.

Claude must not:
- reopen rule-based iteration on the AI-1 extractor;
- re-run, re-score or tune on S1-OOD v1, v2 or v3.

Doing either needs explicit user authorization and a new holdout.

## 21. ChatGPT review standard

Every completed block is reviewed adversarially.

Ask: 1. Did it solve the intended AI problem? 2. Is the AI genuinely
doing meaningful work? 3. Could deterministic software do the same
thing? 4. Is the evaluation valid? 5. Is there leakage? 6. Is the
benchmark biased? 7. Are metrics correctly defined? 8. Is it
reproducible? 9. Are claims stronger than evidence? 10. Does it
strengthen the AI portfolio story? 11. Is complexity justified? 12. Did
it remain inside v0.1? 13. Did anything touch the clinic branch? 14. Is
the next block still the highest-value AI work?

Passing tests alone is never sufficient for approval.

## 22. North star

The project should demonstrate:

``` text
Defined AI problem
→ synthetic benchmark
→ deterministic baseline
→ LLM extraction
→ bounded agent
→ controlled comparison
→ error analysis
→ grounded longitudinal reasoning
→ verification
→ adversarial evaluation
→ reproducible AI system
```

For AI-1, "LLM extraction → bounded agent → controlled comparison" was
realised as "learned local extractor → deterministic verification →
controlled before/after-verification comparison on fresh holdouts".
The reason is in §4A.

The objective is not to prove that AI is present.

The objective is to demonstrate that **AI provides measurable value on a
clearly defined problem, its failure modes are understood, and the
system is engineered to prevent unsupported behavior.**
