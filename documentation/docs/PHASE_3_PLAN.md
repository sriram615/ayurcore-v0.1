# AyurCore OS — Phase 3 Plan: Deterministic Guardrails

**Status:** **Phase 3A implemented (evaluation-only, engine `3A.3`, A1 hardening 2026-10-01; see §2.4–§2.6). Phase 3B PROPOSED — NOT IMPLEMENTED**, and needs explicit approval before any work starts.
**Date:** 2026-09-27. **Baseline:** `documentation/docs/PHASE_2_FREEZE.md`, `documentation/docs/ARCHITECTURE_BASELINE.md`.

## 0. Principles (fixed; derived from `documentation/CLAUDE.md` and the existing architecture)

1. **Guardrails are deterministic.**
   - No AI call evaluates a rule; confirm keeps **zero** AI calls.
   - The same input and the same rule-set version always give the same result.
2. **Guardrails never invent clinical content.**
   - Every clinical rule, threshold, interaction or contraindication comes from a named, clinician-approved source, recorded with the rule.
   - **This plan contains no clinical values.** Every rule in §3 is a *category* awaiting clinician input and explicit approval.
3. **Guardrails read structured data only.**
   - They use `prescription_items`, `prescriptions.dosha_imbalance` and other structured fields, never `description` text.
   - Legacy text-only prescriptions are treated as *unknown* by any rule that needs history (see R1). A rule must say whether "unknown" means pass, warn or block, and that choice needs approval.
4. **Existing behaviour is preserved until 3B is approved.**
   - 3A doesn't change any response, status code or write path.
   - Applied migrations are never edited; schema changes are new migrations (017+).
5. **Every decision is attributable.** The rule-set version and each rule's outcome for a confirm become part of that confirm's audit record (3B), in the same transaction.

## 1. Entry criteria (before 3A starts)

- **R5 closed:** the checkout is under version control, with the Phase 2 freeze committed and tagged. *Status: committed as `41a2d1f` and tagged `phase-2-freeze`; no remote yet.*
- **R4 closed:** the running server has been restarted from the frozen checkout. *Status: restarted 2026-09-27 18:00 IST; health, auth, RBAC and read-only calls verified.*
- **Approved:** this plan, and the decisions in §5 marked *before 3A*.
- **A named clinical owner** for rule content (R3). 3A can start without rules, but no clinical rule can be written without this person.

---

## 2. Phase 3A — Deterministic guardrail foundation (no enforcement)

**Goal:** the machinery to define, version, evaluate and record rules, running in **shadow mode**. Rules are evaluated and their results stored, but **no request is ever blocked or changed**.

### 2.1 Proposed scope
1. **Rule model.**
   - A rule has an identifier, version, category, description and *source reference* (who approved it, and from what document).
   - Its severity (`INFO` / `WARN` / `BLOCK`) is **proposed by the clinician, approved by the owner**.
   - Its parameters are data, not code.
2. **Rule-set versioning.**
   - Rule sets are immutable and versioned, each with a content hash.
   - Exactly one is *active* per clinic.
   - Storage is **open** (§5 D1): a table via a new migration, or a signed file in the repository.
3. **Evaluation engine.**
   - A pure function: `evaluate(candidate, context, rule_set) -> [RuleResult]`.
   - `candidate` is the canonical, merged herbs plus dosha, exactly what `deduct_and_prescribe` would write.
   - `context` holds structured patient and encounter facts, and **only those approved in §5 D3**.
   - No I/O inside rules.
4. **Shadow recording.**
   - Results are stored against the preview or prescription, in a new table (new migration).
   - This is outside the confirm transaction's success path: a recording failure never blocks a confirm. **Open (§5 D2):** best-effort, or in-transaction.
5. **Initial non-clinical rules** (engineering-owned, no clinical judgement):
   - *herb is in the clinic formulary* — already enforced by `normalize_herb` and stock; mirrored so the engine is exercised;
   - *quantity is a positive integer within the existing technical bound* — mirrors `backend/schemas.PrescribedHerb`;
   - *structured data present* — flags a legacy text-only record.

   These duplicate existing checks on purpose, to prove the engine end to end. They are **not** clinical rules.
6. **Tests:**
   - determinism (same input and version → same output);
   - rule-set immutability and hash;
   - shadow mode never changes any existing response or write — assert the complete existing suite still passes unchanged;
   - an engine failure is isolated;
   - legacy "unknown" handling;
   - clinic scoping.

### 2.2 Out of scope for 3A
- Any clinical rule content.
- Any response change.
- Any frontend change.
- Any block.

### 2.3 Exit criteria
- The engine and versioned rule sets exist, with tests passing, and the **complete existing suite passes unchanged**.
- Shadow results are recorded for every preview and confirm in a test clinic.
- The contract impact is documented, but not yet in effect.

### 2.4 Implementation record — Phase 3A (2026-09-27)

**What exists** (`backend/guardrails/`; first released as engine `3A.1`, now `3A.2` — see §2.5 for what changed):
- `candidate.py`: `build_candidate(dosha, herbs)`. Structural validation only, raising `InvalidCandidateError`. No patient identity, no context. Stable SHA-256 fingerprint.
- `model.py`: outcomes **`PASS` / `REVIEW` / `REJECT`**, ordered by severity; stable `ReasonCode` values; immutable `RuleResult` and `EvaluationResult` with `to_dict()`.
- `rules.py`: a `Rule` (id, integer version, category, description, source, params as read-only data) and a registry.
  - A **`CLINICAL` rule cannot register without `approved_by` and `source`**, which raises `UnapprovedRuleError`.
  - Four **engineering** rules mirror existing checks:
    - `ENG-STRUCTURED` → `REVIEW` when dosha or herbs are missing (a data-quality flag, **not** decision D7);
    - `ENG-FORMULARY` → `REJECT` for a non-canonical herb;
    - `ENG-DUPLICATE` → `REJECT` for an unmerged duplicate line;
    - `ENG-QUANTITY` → `REJECT` for a non-integer grams value or one outside 1..10,000 g. The bound is read from `PrescribedHerb` and is **not** a clinical limit.
- `engine.py`: `build_ruleset(id, version, rule_ids)` gives an immutable `RuleSet`. Its content hash covers the order, ids, versions, categories, params and each rule's code fingerprint.
  - It raises `UnknownRuleError` or `InvalidRuleSetError` for unknown, repeated or empty rule sets.
  - `evaluate(candidate, ruleset)` refuses unregistered or tampered rule sets.
  - It runs rules in order and gives every rule at least one result.
  - A raising or malformed rule becomes `REVIEW` with `RULE_ERROR` / `RULE_INVALID_RESULT`. (Originally the exception type was recorded; since 3A.2, nothing from the exception is recorded — §2.5.)
  - The single rule set is `ENGINEERING-BASELINE` v1.

**Differences from §2.1, by instruction:**
- Outcome names are `PASS`/`REVIEW`/`REJECT`, not `INFO`/`WARN`/`BLOCK`.
- **No shadow recording, no new table or migration, and no wiring** into preview, confirm or manual fallback. Storage (D1) and recording mode (D2) remain open.
- No `context` (D3 open) and no per-clinic active rule set (needs D1).
- The §2.3 exit criterion "shadow results recorded for every preview and confirm" is therefore **not met**; it moves to the prerequisites below.

**Tests:** `tests/test_guardrails.py` (12 tests, 89 checks) covers:
- determinism, within a process and across processes;
- rule ordering;
- boundaries;
- invalid inputs;
- unknown rules and tampered rule sets;
- registration, including the clinical-approval gate;
- error isolation;
- import independence (no Gemini, database, inventory, audit or app module is loaded);
- the fact that no production module imports the package;
- Phase 2 consistency (a confirm-shaped candidate passes).

Complete suite **458/458** on an isolated copy (2026-09-27).

**Limitations:**
- Nothing calls the engine, so it has no effect on any request.
- Its rules duplicate existing checks.
- The registry is process-wide and in memory.
- Rule versions are declared by hand (the code fingerprint in the hash catches unversioned code changes, but only at rule-set build time).
- The candidate holds dosha as free text.

### 2.5 Phase 3A.2 hardening (2026-09-27)

Fixes from the Phase 3A review, still **evaluation-only** (no production module imports `backend.guardrails`):
- **Privacy-safe results.** `Finding` has no free-text field, so a rule can report only a line number and a reason code. The engine sets every result's `detail` from a fixed per-reason-code text (`REASON_DESCRIPTIONS`). Herb names, dosha, quantities and exception messages or types never reach a serialized result.
- **Privacy-safe errors.** `InvalidCandidateError` carries only a fixed, value-free message. `build_confirm_candidate` raises it with **no chained exception** (`__cause__` and `__context__` are both `None`), so a logged traceback cannot show the raw input that pydantic or the herb resolver held.
- **Outcome/reason consistency.** `ALLOWED_OUTCOMES` fixes which outcomes each reason code may carry. A mismatch becomes `REVIEW` / `RULE_INVALID_RESULT`.
- **Parameter validation.** `register_rule` accepts only plain JSON data (string keys, finite numbers, strings, booleans, null, lists and objects) and raises `RuleRegistrationError` otherwise.
- **Confirm-equivalent candidates.** `build_confirm_candidate()` validates lines with `PrescribedHerb` and merges them with the shared `herb_resolver.merge_herb_lines()`, the same function confirmation, inventory and audit canonicalization use.
- **Candidate limits aligned with confirm.** There is no dosha-length or line-count cap, because the stored AI proposal has neither; the herb-name bound is read from `PrescribedHerb`.

**Tests:**
- `tests/test_guardrails.py`: 18 tests, 157 checks, including privacy of results, errors, tracebacks and logs.
- `tests/test_herb_merge.py`: 5 tests.
- Complete suite: **473/473** on an isolated checkout with a fresh, migrated and seeded database (2026-09-27).

**Still open (non-blocking while evaluation-only; must be settled before 3B):**
- approval details (`approved_by`, `source`, `description`) are not part of the rule-set hash;
- the code fingerprint covers only a rule's own `check` function, not helpers it calls;
- no execution time limit on rules (inside confirm, a slow rule would hold the database write lock);
- params are frozen one level deep;
- the candidate fingerprint hashes short free text, which would be guessable if stored.

**Phase 3B prerequisites (in addition to §4):**
- decisions D1 (storage), D2 (recording), D4 (overrides), D5 (blocked-confirm response) and D6 (activation);
- D3 before any rule reads patient or encounter facts, and D7 per rule;
- a named clinical owner and approved content for any `CLINICAL` rule (§3);
- shadow recording for a trial period before enforcement;
- a contract addendum (v1.7);
- separately authorized frontend work.

### 2.6 Phase 3A.3 — A1 engine hardening (2026-10-01)

Six reviewed blocks (B1–B6) from the adversarial review of the §2.5 open items, each designed, approved, implemented and committed separately on `backend/sriram`. Still **evaluation-only**: no production module imports `backend.guardrails` (re-verified for B5), and no prescription flow, API, database, migration, audit or clinical logic changed. Engine version `3A.3`.

| Block | Commit | What changed |
|---|---|---|
| B1 | `e1eb4ea` | **Rule metadata in the rule-set hash.** `description`, `source` and `approved_by` are hashed exactly as given (`""` and `None` are distinct). A `CLINICAL` rule whose approver or source is blank, whitespace-only or not text is refused as unapproved (previously any non-empty value, including whitespace, was accepted). Metadata must be text (`approved_by` may be `None`); empty text on an `ENGINEERING` rule is still accepted. |
| B2 | `a060bc2` | **Helper coverage; fail closed.** A rule's code fingerprint covers its `check` function and every helper it reaches, transitively (global names and closures, mutual recursion included), in the rule's own module or exactly `backend.guardrails` (not a look-alike such as `backend.guardrails_x`). A rule whose source cannot be read (generated function, callable object, no `.py` files) is no longer fingerprinted by its name: `build_ruleset` raises `InvalidRuleSetError`. |
| B3 | `0b7cfa4` | **Deep-frozen params.** Registration stores a deep, read-only copy (objects → read-only mappings, lists → tuples): caller-held references and the rule itself can no longer change a registered rule. A hashing hook keeps the serialisation identical, so rule-set hashes did not change. |
| B4 | `adf94f7` | **Evaluation cost: documented and tested, no runtime limit.** A cap would be stricter than `/prescribe/confirm` (§2.5). A confirm candidate never has more lines than the formulary has herbs (20 today); only the dosha text is unbounded (AI proposals). Rules must run in time linear in the candidate's size. Timing smoke tests (medians, 10–40× headroom over measurements): worst case < 100 ms, typical < 10 ms, building from 5,000 lines < 300 ms. There is no per-rule timeout (it cannot be enforced in-process without making results timing-dependent). |
| B5 | `e531f2e` | **Engine fingerprint in every result.** `EvaluationResult` (and `to_dict()`) gains `engine_fingerprint`: the read-only engine tables (`_SEVERITY`, `ALLOWED_OUTCOMES`, `REASON_DESCRIPTIONS`) plus the source of the code that interprets findings, found from `evaluate()` through functions and package-class methods, properties, staticmethods and classmethods (17 functions after B6; the rules' own checks are left to the rule-set hash). Sets and sources are sorted, so it is identical across processes and hash seeds. `evaluate()` raises `EngineChangedError` if a covered table, function, method or descriptor was replaced, or a function's `__code__` swapped in place, after import. Kept separate from the rule-set hash. |
| B6 | `5abf3e1` | **Whole-number grams.** The candidate fingerprint writes a whole-number float as an integer (`5.0` → `5`, `-0.0` → `0`), matching how candidates already compare and evaluate. Fractional values are unchanged, and rules still see the original value. Integer candidates (every confirm-path candidate) keep their fingerprints. |

**Status of the §2.5 open items:**
- approval details not hashed: **resolved** (B1);
- helpers not in the code fingerprint: **resolved** (B2), within the limits below;
- no rule time limit: **not added, by decision** (B4). Replaced by the formulary line bound, the linear-time rule requirement and timing smoke tests. Where evaluation runs relative to confirm's database lock is a 3B decision;
- params frozen one level deep: **resolved** (B3);
- guessable candidate fingerprint: **deferred** to shadow recording (A3), where it matters only if fingerprints are stored. A non-secret per-record salt is the likely answer; a secret key was rejected (key management, and it blocks independent reproduction).

**Fingerprints at `3A.3`:**
- rule set `ENGINEERING_BASELINE`: `37c3fe7f19c8a28bf1998326bc9ce1cd48279720cfb2d3e6033de43302792baf`. Changed by B1 and B2, unchanged since;
- engine: `d7828082f176a34bc4faefce01d21b5c2667b0fda182f9f4de4a5a2e852090b7`. It changes whenever covered engine code changes; the version bump alone does not change it;
- sample integer candidate (Brahmi 5 g, Triphala 30 g, dosha `Vata`): `e79da498fb6d3235adf5002d166c72a7dcf9aaca506c58b7de557999ee426a91`.

**Verification:**
- `tests/test_guardrails.py`: 26 tests, 268 checks. `tests/test_herb_merge.py`: 5 tests. Run only through `scripts/run_isolated_tests.py`: all passed on every block (24 tests after B1, rising to 31 after B6) and again after the version bump (31 passed).
- Each block's new test was shown to fail against the code it fixes (revert checks): for example, the old `engine.py` (B1 metadata), the old fingerprint function (B2 helpers), the old `rules.py` (B3 caller edits leaking in), unsorted sets, mutable tables and removed rebinding, code or descriptor checks (B5), and the old `candidate.py` or a removed `is_integer()` guard (B6). B4 adds no code, so its tests were shown to fail by breaking their own expectations.
- The complete suite was **not** re-run for A1; the last complete run is 473/473 (§2.5). It must be run before these commits are merged.

**Known limitations (carried into A3 and 3B):**
- **In-process guard only.** The engine guard detects accidental or casual replacement. It cannot protect `evaluate()`, `_engine_intact()` or its own records against deliberate in-process tampering.
- **Coverage gaps.** Not covered:
  - rule code: functions in other packages, module-attribute calls, module constants and data captured in closures (data must come in through `params`);
  - engine scan: closures, and descriptor types other than property getters, staticmethod and classmethod.
- **Over-inclusion.** Name-based matching includes some unneeded code (attribute names, same-named methods). Deterministic, but more sensitive than strictly necessary.
- **Source read from disk.** Source is read from disk when first fingerprinted: edits made after import are fingerprinted as on disk, and deployments must ship `.py` files (importing `backend.guardrails` fails without them).
- **Rules have no runtime guard.** A rule's helper replaced after its rule set is built is not detected; B5's guard covers the engine only.
- **Exact-string metadata.** Metadata is hashed exactly: no trimming or Unicode normalisation. A `CLINICAL` rule's description may still be blank.
- **Building cost grows with input.** Candidate building grows with the number of submitted lines (about 6.5 µs per line, the same merge confirm performs). Bounding it would need a limit on `ClinicalParse`, a production schema change.
- **Timing tests.** They are smoke alarms, not guarantees, and could flake on a heavily loaded machine.
- **Pinned values in tests.** Tests pin the baseline rule-set, sample-candidate and pre-B6 engine fingerprints, so an intended change to any of them requires updating those tests.
- **Not public API.** `EngineChangedError` and `ENGINE_FINGERPRINT` are not exported from `backend/guardrails/__init__.py`.

**Phase 3B prerequisites, updated:** as above, except that the contract change is now an update of `documentation/AyurCore_API_Contract.md` to v1.7 (the separate addenda were merged into it on 2026-09-28).

---

## 3. Candidate clinical rule categories — ALL REQUIRE EXPLICIT APPROVAL

Listed only so the clinician and owner can decide what to specify. **No thresholds, doses, interactions or contraindications are proposed here, and none may be inferred by engineering or by an AI model.** Each category needs:
- a clinician-supplied parameter table with sources;
- a severity decision;
- a decision on how legacy "unknown" data is treated;
- written approval.

| Category | What a rule would check | Data it needs | Exists today? | Status |
|---|---|---|---|---|
| C1 Per-herb total quantity | Confirmed total grams for a herb against a clinician-specified range | Items (yes); **clinician-supplied values per herb** | Items yes; values **no** | REQUIRES APPROVAL + CLINICAL INPUT |
| C2 Herb–herb combinations | A listed pair or set that must not, or should rarely, be co-prescribed | Items; **clinician-supplied list** | List **no** | REQUIRES APPROVAL + CLINICAL INPUT |
| C3 Patient-condition contraindications | A herb against a documented patient condition | Structured patient conditions — **no such data model exists** (the registry holds identity only) | **No** | REQUIRES APPROVAL + SCHEMA DESIGN + CLINICAL INPUT |
| C4 Repeat or duplicate therapy | The same herb prescribed again within an open encounter or a window | Items plus encounter linkage; **window and severity from the clinician** | Data yes; window **no** | REQUIRES APPROVAL + CLINICAL INPUT |
| C5 Dosha consistency | Prescribed herbs against the stated dosha | **Clinician-supplied mapping**; dosha is free text today | **No** | REQUIRES APPROVAL; likely needs structured dosha values first |

Frequency, duration and dosage form are **not** represented in the current schema (`ClinicalParse` holds only dosha and total grams). No rule may assume them.

---

## 4. Phase 3B — Enforcement integration (after 3A exits and rules are approved)

**Goal:** approved rules take effect at the clinical boundary, deterministically, with the doctor always informed and every outcome audited.

### 4.1 Proposed scope
1. **Preview (advisory).**
   - `/prescribe/preview` and `/prescribe/manual-preview` return rule results with the proposal, so the doctor sees them before confirming.
   - This is an **additive response change**, and needs a contract addendum (v1.7) plus frontend work (separately authorized).
2. **Confirm (enforcing).** Inside the existing `BEGIN IMMEDIATE` transaction, after herb normalisation and the merged-total check, and **before** `deduct_and_prescribe`:
   - evaluate the active rule set on the final candidate;
   - a `BLOCK` result without an approved override gives a new, documented error (status code **open**, §5 D5) and writes nothing, like every other confirm rejection;
   - `WARN` results are allowed and recorded.
3. **Override (if approved, §5 D4).**
   - The doctor supplies an explicit override with a reason.
   - Who may override, and which severities can be overridden, is decided by the clinician and owner.
4. **Audit.**
   - The `PRESCRIPTION_CONFIRMED` event data gains the rule-set version and hash, each rule's outcome, and any override and reason, in the same transaction.
   - This changes the event shape validated by `backend/audit_store.py`, so it needs:
     - approval, since audit logic is protected;
     - a design that keeps every existing event verifiable;
     - chain-compatibility tests.
5. **Idempotency.**
   - An override is part of the request, so it must enter `compute_request_fingerprint`.
   - The design must keep existing keys' replays byte-identical.
6. **Rule-set activation.**
   - Changing the active set is an ADMIN action (permission **open**, §5 D6), audited as its own event type (new, needs approval).
   - It never affects a confirm already in progress.

### 4.2 Tests (minimum)
- **Determinism:** block, warn and pass behave identically across replays.
- **Blocked confirms write nothing:** no stock, prescription, items, audit or idempotency row.
- **Override path:** audited, with its reason stored.
- **Existing behaviour preserved:**
  - confirms with **no active rule set** behave exactly as today (the complete existing suite passes unchanged);
  - the audit chain still verifies across old and new event shapes;
  - replays of pre-3B idempotency records are unchanged.
- **Concurrency:** confirms racing a rule-set activation serialize consistently.
- **Legacy data:** "unknown" history is handled per the approved decision.

### 4.3 Exit criteria
- Approved rules are enforced in a test clinic, with every outcome audited.
- The contract addendum is published.
- The frontend shows results (separately authorized).
- The complete suite passes.
- **Clinical sign-off** on rule behaviour against clinician-reviewed cases (R3).

---

## 5. Decisions required (none made here)

| ID | Decision | Needed before | Decider |
|---|---|---|---|
| D1 | Rule-set storage: new table (migration) or signed repository file | 3A | Engineering + owner |
| D2 | Shadow result recording: best-effort or in-transaction | 3A | Owner |
| D3 | Which patient or encounter facts rules may read (and whether new structured patient data is collected at all) | 3A (design), 3B (use) | Clinician + owner (privacy impact) |
| D4 | Whether overrides exist, who may override, which severities | 3B | Clinician + owner |
| D5 | Status code and error shape for a blocked confirm | 3B | Engineering + owner |
| D6 | Who may activate a rule set; audit event for activation | 3B | Owner |
| D7 | Treatment of legacy text-only history in rules (pass / warn / block / exclude) | 3B, per rule | Clinician |
| D8 | Every clinical rule's content, parameters, severity and source (C1–C5) | Before that rule is enabled | **Clinician**, approved by owner |

## 6. Explicitly not planned

- AI-generated or AI-evaluated rules.
- Thresholds taken from the internet or model knowledge.
- Inferring frequency or duration.
- Moving any clinical write out of `/prescribe/confirm`.
- Editing migrations 001–016.
- Changing A0.
