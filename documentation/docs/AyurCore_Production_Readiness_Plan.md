# AyurCore OS — Production Readiness Plan

*Prepared September 2026. Scope: cloud-hosted deployment for one clinic, with the goal of getting this specific deployment safe, stable, and legally sound enough to run a real clinic's patient records and be worth paying for.*

## Where things actually stand today

The clinical-safety core is genuinely solid: Pydantic boundaries reject malformed AI output, `herb_resolver.py` refuses to act on a herb it doesn't recognize, and the atomic `BEGIN IMMEDIATE` inventory deduction means two doctors can't oversell the same stock. Phase 1 closed the PII air-gap — patient identity never reaches Gemini. That's real engineering, and it's more than a lot of "AI wrapper" products bother with.

But "the core logic is sound" and "production ready" are different bars. Three things changed the moment you chose *cloud-hosted by you*: there is no longer a locked door and a receptionist controlling who touches the machine, the data now lives on infrastructure you are personally responsible for securing and backing up, and every network hop between a browser and your server is now a place data can leak if you don't put HTTPS and auth in front of it. None of the roadmap below is optional busywork — it's what "local hardened MVP" becoming "internet-facing product handling real health records" actually requires.

One more framing note: your own project docs describe AyurCore as a *"local operating system"* running *on the clinic's premises*. Moving it to a server you host is a meaningful change from what was likely pitched to the clinic — flag this to them explicitly and get their sign-off before go-live (see the Legal section). They may care, and if something ever goes wrong, "we told them and they agreed" versus "we quietly changed the architecture" is the difference between a bad day and a lawsuit.

## The one thing to fix before anything else

Right now, `POST /prescribe` is a single atomic pipeline: tokenize → ask Gemini → rehydrate → deduct inventory → write the prescription to the database, with no human checkpoint in between. If Gemini misreads "20g" as "200g", or mishears a dosha, or gets confused by Manglish phrasing, the current code commits that mistake to the clinical record and physically deducts the wrong amount of herb from inventory — no doctor ever sees the parsed result before it's final.

Your Pydantic/resolver/transaction hardening protects against the AI inventing a herb that doesn't exist or a quantity outside sane bounds. It does **not** protect against the AI being confidently, plausibly wrong about a real herb and a real quantity — and that failure mode is the one that actually matters in a clinical setting, because nothing downstream catches it.

Before anything else on this list, split the endpoint in two:

1. **`POST /prescribe/preview`** — runs tokenize → Gemini → rehydrate exactly as today, but stops there. Returns the parsed dosha + herb list to the doctor. No inventory touched, nothing written to the database.
2. **`POST /prescribe/confirm`** — takes the (possibly doctor-edited) structured result and *only then* runs herb resolution + the atomic inventory deduction + the DB write. No AI call happens here at all.

This is a genuinely small code change — you already have every piece (`normalize_herb`, `deduct_and_prescribe`, the sanitizer) — but it's the difference between "AI-assisted" and "AI-autopilot," and it's the first thing a clinic's lawyer or a curious regulator will ask about if you sell this as a real clinical tool. Do this before you touch cloud infrastructure.

## Phase 2 — Authentication & access control

This was already on your roadmap as Phase 3; hosting in the cloud makes it Phase 2 and non-negotiable, because there is no longer a physical perimeter around who can reach the API.

- A `users` table: `id`, `username`, `password_hash`, `role` (`doctor` / `receptionist`), `is_active`. Hash passwords with `argon2-cffi` (preferred over bcrypt in 2026 — no 72-byte truncation footgun, tunable memory cost).
- A login endpoint that verifies the password and issues a server-side session (a random opaque token stored in a small `sessions` table with an expiry, not a JWT — with a Streamlit frontend there's no clean place to store and refresh a JWT client-side, and you don't need JWT's statelessness for one clinic's traffic). Streamlit keeps the token in `st.session_state` and sends it as a bearer token on every API call.
- Role checks on every mutating endpoint: a receptionist can look up inventory and patient history but should not be able to call `/prescribe/confirm` — that's a doctor action. Enforce this server-side (a `require_role("doctor")` FastAPI dependency), never just by hiding a button in the UI.
- Rate-limit the login endpoint specifically (5 attempts per username per 15 minutes is a reasonable start) — it's the one endpoint an outsider can reach without already having a valid session.
- An **audit log**: every prescription write and every inventory adjustment should record who did it and when, not just what happened. This is both good clinical governance and something DPDP's security-safeguard obligations effectively expect (see Legal section — system/access logs retained for at least a year).

## Phase 3 — Cloud infrastructure

Given it's one clinic, keep this boring on purpose. You do not need Kubernetes, multi-region failover, or a managed database cluster for one clinic's prescription volume — you need one small, well-secured box that's backed up properly.

- **Host:** a single small VPS (Hetzner CX22 or a DigitalOcean/Linode equivalent — roughly 2 vCPU / 4GB RAM is overkill for this workload but gives headroom and costs about $6–12/month).
- **Topology:** bind FastAPI to `127.0.0.1` only — it should never be reachable directly from the internet. Streamlit is the only public-facing process, sitting behind **Caddy** as a reverse proxy, which gets you automatic Let's Encrypt HTTPS with near-zero config. Streamlit talks to FastAPI over localhost. This alone removes an entire class of "someone found the raw API and started poking it" risk.
- **Process management:** systemd units for the FastAPI and Streamlit processes (`Restart=on-failure`), not a bare `python main.py` in a tmux session. If the process crashes at 2am, it should come back on its own.
- **Firewall:** only 22 (SSH, ideally key-only, ideally from your IP or a bastion), 80, and 443 open. Nothing else.
- **Secrets:** `GEMINI_API_KEY`, the session-signing secret, etc. live in an environment file with `600` permissions, never in git. Given you're a single operator, a plain `.env` loaded by systemd's `EnvironmentFile=` is genuinely fine here — don't over-build a secrets manager for one server.
- **Production FastAPI hygiene:** disable `/docs` and `/redoc` in production (`FastAPI(docs_url=None, redoc_url=None)`), and lock down CORS to exactly your Streamlit origin.

## Phase 4 — Disaster recovery & backups

This was already Phase 2 on your roadmap; the recommendation below is specific to the fact that you're staying on SQLite (a reasonable choice — don't migrate to Postgres just because it "feels more production," your write pattern is low-volume and SQLite in WAL mode handles it fine).

- **Litestream** is the right tool for exactly this situation: it tails SQLite's WAL and streams changes continuously to S3-compatible object storage (Backblaze B2 or Cloudflare R2 are both cheap — a clinic's database is tiny, this will cost cents a month). You get point-in-time recovery, not just nightly snapshots, and restoring is a one-command operation.
- Encrypt the backups at rest (both B2 and R2 support this natively) — this is also a DPDP security-safeguard expectation.
- **Actually test a restore.** A backup you've never restored from is a hope, not a plan. Do a dry run onto a throwaway VM before you trust it, and repeat that drill periodically (quarterly is reasonable for this scale).
- Write down your RTO/RPO in plain language even informally: "if the server dies, we can have a new one running from the latest backup within roughly N hours, and we'll lose at most the last few seconds of writes." That sentence is also useful to have ready if the clinic ever asks what happens if your server goes down mid-day.
- Decide what the clinic does if AyurCore is unreachable during a consultation (a paper fallback, most likely) and tell them that's the plan — don't let "what if the internet is down" be a surprise on day one.

## Phase 5 — Observability

You added PII-safe structured logging in Phase 1 (request IDs, patient tokens, latency) — extend that into actual operational visibility instead of logs nobody reads until something breaks:

- **Error tracking:** Sentry's free tier is enough for one app at this scale, and it turns "the clinic called saying it's broken" into "I got an alert three hours ago and already know the stack trace."
- **Uptime monitoring:** something external (UptimeRobot's free tier, or similar) hitting a `/health` endpoint every few minutes, alerting you by SMS/email/push if it goes down. You want to know before the clinic does.
- **Gemini-specific monitoring:** track AI call latency and failure rate (you already measure latency in `main.py`) and alert if the failure rate spikes — that's your leading indicator of a Gemini outage or a quota problem before it becomes ten failed prescriptions in a row.
- **A `/health` endpoint** that checks DB connectivity, not just "the process is running."

## Phase 6 — Feature completeness

"Production ready" isn't only infrastructure hardening — the actual clinic workflow needs to exist. Right now `ui/` is empty and the API only supports write-once prescribing. Before this replaces the clinic's current process, it needs:

- The **Streamlit dashboard itself** — login screen, the preview/confirm prescribing flow from above, inventory view, and prescription history.
- **Patient lookup / history** — a returning patient's past prescriptions, not just a one-shot write. Right now there's no way to search "what has this patient been prescribed before," which a real clinic will want on day one.
- **Inventory replenishment** — you only have deduction today. Someone needs to be able to add stock when a new batch arrives, ideally logged with who/when/batch code, mirroring the rigor you already put into deduction.
- **Low-stock visibility** — a simple dashboard indicator, so the clinic isn't discovering they're out of Ashwagandha mid-consultation.
- **Editing/voiding a prescription** — mistakes happen even with the confirm step; there should be an audited way to correct or void a record rather than editing SQLite by hand.

## Phase 7 — Testing & CI

The golden-test pattern you're using (plain scripts, print PASS/FAIL) has worked well for an MVP, but for something you're going to deploy and iterate on remotely, get a real CI loop before you need one under pressure:

- Move to `pytest` properly (your venv already has the dependencies for FastAPI's `TestClient`, which is what `test_pii_airgap.py` already uses) — `pytest` gives you fixtures for the tokenize/AI-mock/DB-reset pattern you're currently hand-rolling in every test file, and lets you run `pytest -k pii` to target just one area.
- A GitHub Actions workflow that runs the full suite on every push, including a fresh SQLite DB per run so tests never depend on leftover state.
- Add the `/prescribe/preview` + `/prescribe/confirm` split from the top of this document to your golden-test suite once it exists — that boundary deserves the same rigor as the atomic-rollback test.
- A basic dependency vulnerability scan (`pip-audit` in CI) — cheap, and worth having before you're handling real patient data.

## Phase 8 — Legal, compliance & business setup

I'm not a lawyer, and this section is the factual grounding you need to go get real legal advice, not a substitute for it — especially since you're now commercially hosting another business's patient health data.

**DPDP Act 2023 / DPDP Rules 2025, what I could confirm:** the Rules were notified in November 2025. The Data Protection Board was established November 14, 2025; Consent Manager registration opens November 14, 2026; full substantive compliance obligations commence May 14, 2027. Practically relevant obligations include: consent must be explicit, in plain standalone language, and easily withdrawable (no pre-ticked boxes, no bundled generic policies); "reasonable security safeguards" are expected, explicitly including encryption, access controls, and **system/access logs retained for at least one year** — which is another reason the audit log in Phase 2 isn't optional; and a data breach must be reported to the Data Protection Board within **72 hours** of discovery, with affected individuals notified as well. I could not get a reliable, current answer on whether health data specifically carries extra obligations under the final Act (earlier draft bills had a distinct "sensitive personal data" category; I'm not confident that survived into the final Act in the same form) — **this is exactly the kind of question to put to a lawyer rather than infer**, given you're dealing with patient health records commercially.

**Things to have in place before real patient data flows through this, regardless of exact legal thresholds:**
- A written data processing agreement with the clinic: who owns the data, what you're allowed to do with it, what happens to it if the clinic stops using AyurCore, your obligations if there's a breach.
- A consent notice the clinic shows patients (or a process for one) — plain language, explains that clinical notes are processed by an AI service (Gemini) via anonymized tokens, hosted on cloud infrastructure.
- A basic incident response plan — who you call, what you tell the clinic, and how fast, if something goes wrong. Given the 72-hour Board notification window, "figure it out when it happens" isn't good enough.
- A retention/deletion policy — how long records are kept and what "delete this patient's data" actually does in your system (right now: nothing does that).
- Consider cyber/professional liability insurance before you're commercially responsible for real health records — it's inexpensive relative to the exposure if a breach happens.

**Business-side, since this is your first business:**
- A simple contract or terms of service with the clinic covering price, what "support" means, what your uptime commitment is (don't promise 99.99% — promise something honest you can actually meet), and a liability cap. Handshake deals go fine right up until the first incident.
- Gemini API cost is a non-issue at this scale: at roughly $0.30/1M input tokens and $2.50/1M output tokens for Gemini 2.5 Flash, a typical prescription request (a few hundred tokens in, under two hundred out) costs well under a tenth of a cent. Even a busy clinic doing 50 prescriptions a day comes in under a dollar a month in Gemini spend. Your real costs are the ~$10-15/month VPS, a few dollars/year of domain + backup storage, and your own time — price accordingly, and don't let AI-cost anxiety drive any technical decisions here.
- Decide your pricing model (flat monthly fee is simplest for a single clinic) with enough margin to cover your time for support and incidents, not just server costs.

## Rough sequencing

This is a solo effort, so sequence for risk reduction, not calendar-optimal parallelism:

1. Preview/confirm split (small, safety-critical, do it first — likely 1–2 days).
2. Auth + audit log (blocks everything else being safe to put on the internet — 3–5 days).
3. Infra: VPS, Caddy, systemd, firewall, secrets (1–2 days once you've picked a provider).
4. Litestream backups, tested restore (half a day setup, but budget time to actually rehearse a restore).
5. Streamlit UI covering the real workflow, including patient history and inventory replenishment (this is likely the biggest single chunk of work — a couple of weeks depending on how far you take it).
6. Observability (Sentry + uptime monitor + `/health` — an afternoon, do it before go-live not after the first incident).
7. Legal/contract/consent paperwork with the clinic — start this in parallel with step 5, not after; lawyers take longer than code.
8. CI + expanded tests — worth doing whenever a natural pause appears; not a blocker for go-live if steps 1–2 are solid, but don't let it slide indefinitely.

## Go-live checklist

- [ ] Preview/confirm split shipped and covered by a golden test
- [ ] Every mutating endpoint requires auth; role checks enforced server-side
- [ ] Audit log records who did what, when
- [ ] FastAPI bound to localhost only; only 22/80/443 open on the firewall
- [ ] HTTPS working end-to-end via Caddy; `/docs` disabled in production
- [ ] Litestream running; a restore has actually been rehearsed once
- [ ] Sentry + uptime monitoring wired up
- [ ] Clinic has explicitly agreed their data is now cloud-hosted, not local
- [ ] A consent notice for patients exists in some form
- [ ] A written agreement with the clinic covers price, support, uptime expectations, and liability
- [ ] You know what "figure out fast" looks like if there's ever a breach

Sources:
- [Gemini 2.5 Flash API Pricing](https://devtk.ai/en/models/gemini-2-5-flash/)
- [DPDP Rules 2025: A practical guide with implementation checklist](https://www.scrut.io/post/dpdp-rules)
