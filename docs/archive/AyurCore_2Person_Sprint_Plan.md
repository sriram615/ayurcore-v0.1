# AyurCore — 2-Person Sprint Plan (Sep 12 → Sep 20)

Scope check first, because 8 days is tight: this builds a **demo-ready prototype for the pitch**, not the hardened production system from the readiness plan. Real patient data, full RBAC, backups, and the deeper compliance work stay out of scope here on purpose — trying to do both in 8 days guarantees neither gets done. The demo runs on test/fake data throughout.

## Roles

**Person A (you) — AI & backend.** Owns everything that touches the model, the pipeline logic, and data correctness: the preview/confirm split, the agentic tool-calling loop, the Sarvam voice integration, the correction-logging capture, auth *logic* (login/session code, not the infra it runs on).

**Person B — DevOps.** Owns everything that touches where the code runs and how it's reached: cloud hosting, containerization, CI, secrets, HTTPS, monitoring, and — pairing naturally with deployment — the Streamlit UI itself, since they'll be the one standing it up and keeping it running. Also owns kicking off WhatsApp Business verification on day one, since Meta's approval turnaround is outside either of your control and needs the longest possible lead time.

## The integration contract — agree on this before writing feature code

Two people building in parallel only works if the seam between "the code" and "the infrastructure" is nailed down on day one, not discovered on day six.

- **Environment variables, decided now, changed never:** `GEMINI_API_KEY`, `SARVAM_API_KEY`, `SESSION_SECRET`, `DATABASE_PATH`, `WHATSAPP_TOKEN`. Person B provisions these on the hosting side without needing to keep syncing with Person A on what exists.
- **A Dockerfile is the actual handoff point.** Person A's job ends at "this container runs the app correctly on my machine." Person B's job starts at "deploy this container reliably." Containerize early — day 2, not day 7 — so this boundary is real, not aspirational.
- **A written API contract before either side builds against it.** Before Person B builds the Streamlit preview/confirm screens, Person A writes down (even in a shared doc, doesn't need to be fancy) the exact request/response JSON shape for `/prescribe/preview` and `/prescribe/confirm`. Build both sides against that written contract, not against "whatever the other person's code currently does" — that's what prevents day-5 integration surprises.
- **Git: `main` is always deployable.** Feature branches per piece, merge only when it runs. Person B sets up the simplest possible CI on day 2 (install deps, run the existing test scripts) so Person A gets fast feedback on every push instead of finding out something broke at integration time.
- **Daily 15-minute sync, same time every day.** With two parallel tracks over 8 days, silent drift is the biggest risk on this timeline, not any single hard technical problem.

## Day-by-day

**Day 1 — Sep 12, kickoff**
Joint: agree on the contract above, divide tasks, set the daily sync time.
Person A: start the `/prescribe/preview` + `/prescribe/confirm` split — this is the safety-critical piece from the readiness plan and the fastest thing to get right first, since everything else builds on top of it. Touch the inventory/prescriptions schema once while you're in there to add a `clinic_id` column — costs almost nothing today, expensive to retrofit later.
Person B: get a bare "hello world" FastAPI endpoint deployed to a real URL with HTTPS *today*, before any real feature exists — proves the deploy pipeline works before anything depends on it. Recommend a managed platform (Railway, Render, or Fly.io) over hand-rolling a VPS for this sprint — the full Caddy/systemd/firewall setup from the readiness plan is the right call for the real pilot later, but it's not worth the setup time against an 8-day deadline. Also today: start the WhatsApp Business verification and submit a template for approval — this has external lead time you don't control, so it needs to start now even though you won't use it until day 6 or 7.

**Day 2 — Sep 13**
Person A: finish preview/confirm + its tests. Start the agentic tool-calling loop (`check_herb_alias`, `check_stock`) using native `google-genai` function calling.
Person B: write the Dockerfile, wire the CI job, get secrets loaded on the hosting platform, confirm HTTPS and the domain are solid.

**Day 3 — Sep 14**
Person A: finish the tool-calling loop and its tests — mirror the style of the existing `test_pii_airgap.py`, mocking the Gemini boundary to verify the right tool gets called with the right arguments.
Person B: set up bare-bones monitoring (an uptime check plus Sentry's free tier), and start the Streamlit UI skeleton — page layout, a login stub, nothing wired to real endpoints yet.
Checkpoint: first deploy of real functionality (not just hello-world) to the shared environment.

**Day 4 — Sep 15**
Person A: wire Sarvam voice input into the existing tokenize pipeline. Add the corrections table and capture instrumentation at the confirm step (log what the AI proposed versus what the doctor actually confirmed) — cheap to add now, this is the data flywheel from earlier and it's wasted if you don't start capturing from day one of real usage.
Person B: build the real Streamlit flow — record/type input, the preview screen, the confirm button — calling Person A's actual endpoints per the written contract. Wire basic auth: Person A already has the login/session backend logic, Person B wires the login screen and stores the session token in `st.session_state`.

**Day 5 — Sep 16 — integration day, joint, no solo work**
This is the day two independently-built halves either click together or reveal where the contract broke down — budget the whole day for it, expect friction, don't schedule anything else. If you haven't already sat down with the doctor for the 15-minute sample-notes session from earlier in this process, this is also the last safe day to do it — you want real friction showing up today, in front of you, not for the first time during the actual demo.

**Day 6 — Sep 17 — buffer, always needed**
Fix whatever day 5 broke. Person B: if the WhatsApp template came back approved, wire the real send at the confirm step; if it hasn't, stub it — log what would have been sent and treat it as a known, disclosed gap rather than letting an external approval you don't control block everything else. Person A: one tenant-isolation smoke test now that `clinic_id` exists, proving scoped data doesn't leak — doesn't need to be exhaustive, just needs to exist.

**Day 7 — Sep 18**
Full run-throughs, both of you, looking specifically for rough edges and slow points rather than new features. Deploy the build you intend to demo and freeze feature work from this point.

**Day 8 — Sep 19**
Dress rehearsal — run the actual pitch at least twice, ideally in front of someone who isn't either of you, for a genuinely fresh reaction. Bug fixes only. The temptation to add "just one more thing" the night before is exactly what breaks demos; resist it.

**Day 9 — Sep 20 — demo.**

## What's explicitly out of scope for this sprint

Say this out loud to each other now, not when you're behind on day 6: the order-management/reorder agent, multi-batch FEFO inventory, and the full production-hardening list (Litestream backups, complete RBAC, fuzz testing, per-tenant SQLite files) all stay out. None of them change whether the demo lands. If the timeline slips, WhatsApp is the first thing to cut — stub it and say so honestly rather than let it threaten the parts that actually carry the pitch (voice input, the agent catching its own mistake against real stock, the preview screen proving nothing auto-commits).
