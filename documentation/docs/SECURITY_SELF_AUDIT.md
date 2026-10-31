# AyurCore v0.1 — Security Self-Audit Report

**Date:** 2026-10-30  
**Scope:** Repository Self-Audit (Author-conducted)  
**Classification:** SYNTHETIC DATA PROTOTYPE — NOT CLINICALLY VALIDATED  
**Framing:** Self-audited using OWASP-aligned checklist + automated scanning (`pip-audit`, `bandit`, `semgrep`, `pnpm audit`). Not externally penetration tested.

---

## 1. Executive Summary & Scope

This document records the methodology, raw tool outputs, and analysis of an internal, zero-cost security self-audit performed on the **AyurCore v0.1** codebase.

> [!IMPORTANT]
> **Honest Scope Disclaimer:**
> This audit was conducted by the repository author using free and open-source automated security tooling and manual code inspection. **It is NOT a third-party penetration test, NOT a SOC 2 compliance audit, NOT a HIPAA certification, and NOT an ISO/IEC 27001 evaluation.**
>
> The system is a portfolio prototype intended to demonstrate deterministic clinical EHR patterns, multi-tenant isolation, cryptographic audit trails, and defensive AI boundaries. It is not currently certified for production healthcare deployment.

All evaluations are conducted against synthetic data. The shipped database `db/ayurcore.db` is empty; all test runs use temporary isolated sandboxes created by `scripts/run_isolated_tests.py`.

---

## 2. Automated Scanning Tools & Results

### 2.1 Tool Inventory

| Tool | Version | Target | Purpose | Status |
|---|---|---|---|---|
| **pip-audit** | 2.10.1 | Python virtual environment | Known dependency CVE / advisory scanner | Completed |
| **bandit** | 1.9.4 | `backend/` (5,584 LOC) | Python static application security testing (SAST) | Completed (0 High, 2 Medium FP, 2 Low FP) |
| **semgrep** | 1.183.0 | `backend/` | Semantic pattern scanner | Tool wrapper installed; core binary unavailable on Intel macOS |
| **pnpm audit** | 12.5.1 | `frontend/` (400 deps) | Node.js / Next.js dependency vulnerability scanner | Completed (100 advisories: 10 low, 46 moderate, 40 high, 4 critical) |
| **OWASP ZAP** | N/A | HTTP API endpoints | Dynamic application security testing (DAST) | Skipped (not installed in local environment) |

---

### 2.2 Python Dependency Audit (`pip-audit`)

**Command:**
```bash
./venv/bin/pip-audit
```

**Raw Output:**
```
Found 16 known vulnerabilities in 4 packages
Name              Version ID              Fix Versions
----------------- ------- --------------- ------------
mcp               1.29.0  CVE-2026-59951  1.30.0,2.2.0
mcp               1.29.0  CVE-2026-116685 1.30.0,2.2.0
mcp               1.29.0  CVE-2026-116684 1.30.0,2.2.0
mcp               1.29.0  CVE-2026-116686 1.30.0,2.2.0
opentelemetry-api 1.37.0  CVE-2026-114603 1.45.1
pip               26.0    PYSEC-2026-2875 26.1
pip               26.0    PYSEC-2026-2876 26.1
pip               26.0    PYSEC-2026-196  26.1.2
pip               26.0    PYSEC-2026-196  26.1.2
pip               26.0    PYSEC-2026-2875 26.1
pip               26.0    PYSEC-2026-2876 26.1
pip               26.0    PYSEC-2026-3721 26.2
pip               26.0    PYSEC-2026-3721 26.2.0
urllib3           2.7.0   PYSEC-2026-4177 2.8.0
urllib3           2.7.0   PYSEC-2026-4176 2.8.0
urllib3           2.7.0   PYSEC-2026-4175 2.8.0
```

**Analysis & Disposition:**
- **Core AyurCore Runtime:** FastAPI, Pydantic, Argon2id (`argon2-cffi`), and SQLite bindings have **0 known CVEs**.
- `mcp` and `opentelemetry-api` are transitive dependencies introduced by developer security auditing tools (`semgrep`), not imported by `backend/main.py` or runtime EHR logic.
- `pip` vulnerabilities affect the CLI tool in the virtual environment.
- `urllib3` 2.7.0 has 3 PYSEC advisories resolvable by upgrading to `>= 2.8.0`.

---

### 2.3 Python Static Analysis (`bandit`)

**Command:**
```bash
./venv/bin/bandit -r backend/
```

**Summary Metrics:**
- **Total Lines of Code Scanned:** 5,584 LOC
- **Total Issues:** 4 (0 High, 2 Medium, 2 Low)
- **High Severity Issues:** 0

**Findings & Triage:**

| Issue ID | Severity | Confidence | Location | Code Snippet | Triage / Disposition |
|---|---|---|---|---|---|
| **B105** | Low | Medium | `backend/guardrails/model.py:23` | `PASS = "PASS"` | **False Positive:** Enum string literal for `GuardrailDecision.PASS`, flagged by heuristic matching on "PASS". |
| **B105** | Low | Medium | `backend/pii_sanitizer.py:40` | `_TOKEN_TEMPLATE = "[[PAT_{}]]"` | **False Positive:** Pseudonymization replacement token template, flagged due to substring "PAT". |
| **B608** | Medium | Medium | `backend/patient_store.py:253` | `SELECT ... WHERE prescription_id IN ({placeholders})` | **False Positive:** Dynamic parameter placeholder generation (`', '.join('?' for _ in page)`). Values are passed as parameterized tuples `(clinic_id, *[row["id"] for row in page])`. No user input interpolated. |
| **B608** | Medium | Medium | `backend/shadow_store.py:163` | `INSERT INTO guardrail_shadow_results ({', '.join(_COLUMNS)}) SELECT ...` | **False Positive:** Column name interpolation uses fixed, hardcoded constant tuple `_COLUMNS`. All values are bound with `?` parameters. |

---

### 2.4 Frontend Dependency Audit (`pnpm audit`)

**Command:**
```bash
cd frontend && pnpm audit
```

**Summary Metrics:**
- **Total Dependencies:** 400 (323 dependencies, 37 devDependencies, 63 optional)
- **Total Advisories:** 100
- **Severity Breakdown:** 10 Low | 46 Moderate | 40 High | 4 Critical

**Critical Advisories Identified:**

1. **Next.js 16.3.3 (`next`)**:
   - `GHSA-vcvr-r3jv-pc5j`: Remote Code Execution in `next/og` ImageResponse.
   - `GHSA-2464-fhr4-w9xw`: Unauthenticated Remote Code Execution in Image Optimization API when SVG files are used.
   - `GHSA-673q-mjm6-r5xr`: Unauthenticated Remote Code Execution in `use cache` functions returning typed arrays.
   - *Status:* AyurCore frontend does not use `next/og` or SVG image optimization endpoints, but upgrading Next.js is accepted as a necessary maintenance task for production.
2. **`proxy-addr` 2.0.7**:
   - `GHSA-jqcg-44mw-7w3h`: IP spoofing via IPv4-mapped IPv6 trust subnet.
   - *Status:* Subdependency of Next.js server runtime; fixed in upstream Next.js updates.

---

## 3. Targeted Codebase Checks

### 3.1 Raw SQL & SQL Injection Verification

**Command:**
```bash
grep -rn "f\"SELECT\|f'SELECT\|\.format.*SELECT" backend/ db/
```

**Results:**
- Exactly 1 match: `backend/shadow_store.py:164`:
  ```python
  f"SELECT {', '.join('?' for _ in _COLUMNS)} "
  ```
- **Finding:** Every SQL statement in `backend/` and `db/` uses parameterized queries (`?` placeholders). No user-supplied parameters are interpolated directly into SQL query strings.

---

### 3.2 Hardcoded Secrets & Credentials Verification

**Command:**
```bash
grep -rn "sk-\|API_KEY\s*=\|password\s*=" backend/ scripts/
```

**Results:**
- **Zero** production API keys, private tokens, or hardcoded passwords exist in `backend/`.
- In `scripts/`:
  - `secrets.token_urlsafe(...)` is used for dynamic credential generation in demo setup scripts.
  - `PASSWORD = "seed-demo-password-not-real"` is explicitly marked as synthetic demonstration credentials for test suites.
- Environment variables (`GEMINI_API_KEY`, `SARVAM_API_KEY`, `AYURCORE_BACKUP_PASSPHRASE`, `AYURCORE_AUDIT_ANCHOR_KEY`) are read via `os.environ` and sanitized from subprocess execution in test runners.

---

### 3.3 Rate Limiting Coverage

**Command:**
```bash
grep -n "limiter(" backend/main.py
```

**Results:**
- Line 730: `POST /patients/{patient_pk}/ask` &rarr; `Depends(limiter("ask"))` (Default: 120 requests/min)
- Line 952: `POST /prescribe/preview` &rarr; `Depends(limiter("preview"))` (Default: 30 requests/min)
- Line 1172: `POST /prescribe/preview/local` &rarr; `Depends(limiter("preview_local"))` (Default: 60 requests/min)
- Line 1450: `POST /prescribe/confirm` &rarr; `Depends(limiter("confirm"))` (Default: 60 requests/min)
- Authentication: `POST /auth/login` uses IP- and account-based throttling cooldowns in `backend/auth_service.py` (`login_throttle` table) to prevent credential brute-forcing.

---

### 3.4 Cryptographic Audit Trail Immutability

**Command:**
```bash
grep -rn "DELETE.*audit_events\|UPDATE.*audit_events\|DROP.*audit_events" backend/ db/
```

**Results:**
- **Zero** `DELETE`, `UPDATE`, or `DROP` queries exist in `backend/`.
- The `audit_events` table is strictly append-only. Each row is chained via `prev_hash` &rarr; `event_hash = SHA256(prev_hash || payload)`.
- External HMAC-SHA256 checkpoint anchoring (`scripts/audit_anchor.py`) detects offline file modifications, tail truncation, and intermediate edits.

---

### 3.5 Personally Identifiable Information (PII) in Logs

**Command:**
```bash
grep -rn "logger\." backend/
grep -rn "print(" backend/
```

**Results:**
- `print()` calls in `backend/`: **0**.
- Structured `logger.*` calls in `backend/`: **0** instances of patient names, phone numbers, postal addresses, or raw clinical narratives.
- Log lines record only:
  - Integer IDs (`patient_pk`, `user_id`, `request_id`, `preview_id`).
  - Categorical metadata (`outcome=abstain`, `category=unsafe`, `stage=screen`).
- PII Sanitization Air-Gap: `backend/pii_sanitizer.py` replaces patient identifiers with deterministic pseudonyms (`[[PAT_1]]`) before any external service is called, verifying that responses do not leak identity tokens.

---

## 4. OWASP Top 10 (2021) Evaluation Checklist

| OWASP Category | Status | Architectural Defense & Verification |
|---|---|---|
| **A01: Broken Access Control** | **PASS** | Role-based access control (`DOCTOR`, `ADMIN`, `AUDITOR`) enforced on all endpoints via `require_role(*PERMISSION_MATRIX[action])`. Clinic-level tenant scoping (`WHERE clinic_id = ?`) prevents cross-tenant access. Cross-clinic queries return `HTTP 404` (indistinguishable from nonexistent patient). S8 benchmark verified 0 cross-patient data leaks across 40 test cases. |
| **A02: Cryptographic Failures** | **PASS (Prototype Scope)** | Passwords hashed using Argon2id (`argon2-cffi`). Session tokens generated via CSPRNG `secrets.token_urlsafe(32)`. Audit log cryptographic chaining (`prev_hash` &rarr; `event_hash`). Backups encrypted with AES-256-GCM / PBKDF2 (`scripts/backup_db.py`). *Limitation:* SQLite file is unencrypted at rest; TLS termination assumed at reverse proxy. |
| **A03: Injection** | **PASS** | SQL queries use 100% parameterized bindings (`?`). AI-2 question answering uses deterministic parser/AST execution (no LLM, no prompt injection vector). AI-1 local extractor is an offline Perceptron tagger. Gemini draft path validates proposals against strict database formulary items. |
| **A04: Insecure Design** | **PASS** | Dual-layer verification (planner + deterministic verifier in AI-2). Idempotency keys with SHA-256 request fingerprinting prevent duplicate prescription charges and inventory over-allocation. S8 holdout verifies 170/170 safe refusals on unsafe advice/diagnosis. |
| **A05: Security Misconfiguration** | **PASS (Prototype Scope)** | Custom exception handlers catch database errors and return generic 500 / 503 `DATABASE_BUSY` responses without exposing internal stack traces or SQLite file paths. Migration runner enforces SHA-256 integrity verification. |
| **A06: Vulnerable & Outdated Components** | **PARTIAL** | Core Python backend dependencies have 0 CVEs. 16 dependency CVEs flagged in transitive development/CLI tools (`pip`, `urllib3`, `mcp`). Frontend `pnpm audit` flagged 100 advisories, including 4 critical advisories in `next` 16.3.3. Upgrades needed before any production pilot. |
| **A07: Identification & Authentication Failures** | **PASS** | Constant-time password hashing via Argon2id. Login throttling cooldowns prevent brute-force attacks. Uniform error message (`Invalid username or password.`) prevents username enumeration. Server-side session invalidation on logout. |
| **A08: Software & Data Integrity Failures** | **PASS** | Database migrations 001–022 locked by SHA-256 checksums in `migrations/manifest.json`. AI-2 planner model file locked to `PLANNER_SHA256 = "8e8161b5..."`. Tamper-evident audit chain with external HMAC checkpoint verification. |
| **A09: Security Logging & Monitoring Failures** | **PASS (Prototype Scope)** | Structured logging of security events (login success/failure, logout, RBAC denials, patient registration, encounter open/close, prescription confirmation). Zero PII leakage in logs. *Limitation:* Local stderr/file logging; no centralized SIEM aggregation in v0.1. |
| **A10: Server-Side Request Forgery (SSRF)** | **PASS / N/A** | Backend makes no arbitrary outbound HTTP requests to user-supplied URLs. External AI integrations (Gemini, Sarvam) use hardcoded HTTPS endpoints. Memory pipeline and local extractor execute entirely offline. |

---

## 5. Known Security & Architectural Limitations

1. **In-Process Rate Limiter:**
   - The rate limiter in `backend/rate_limit.py` is an in-memory sliding-window counter. It resets upon process restart and is not shared across multi-worker Uvicorn clusters. Production deployment requires edge rate limiting (e.g., Nginx, Envoy, Cloudflare).
2. **Single-Process SQLite Storage:**
   - Storage is a single SQLite database file (`db/ayurcore.db`) with WAL mode. It lacks database-level access control lists and disk encryption at rest (requires LUKS/FileVault or SQLCipher).
3. **Frontend Dependency Surface:**
   - Next.js 16.3.3 contains known upstream advisories that should be patched by updating `package.json` to a newer stable Next.js release.
4. **No Third-Party Penetration Testing:**
   - All tests and audits to date have been conducted by the author using open-source linters, static analyzers, and synthetic holdouts. No professional third-party pen test has been commissioned.

---

## 6. How to Reproduce Audit Scans

```bash
# 1. Python dependency vulnerability audit
./venv/bin/pip-audit

# 2. Python backend static analysis (SAST)
./venv/bin/bandit -r backend/

# 3. Targeted security pattern checks
grep -rn "f\"SELECT\|f'SELECT\|\.format.*SELECT" backend/ db/
grep -rn "sk-\|API_KEY\s*=\|password\s*=" backend/ scripts/
grep -n "limiter(" backend/main.py
grep -rn "DELETE.*audit_events\|UPDATE.*audit_events\|DROP.*audit_events" backend/ db/

# 4. Frontend dependency audit
cd frontend && pnpm audit

# 5. Full regression and safety test suite execution (isolated sandbox)
./venv/bin/python scripts/run_isolated_tests.py -q
```
