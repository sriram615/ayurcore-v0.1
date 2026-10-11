# apps/web — new AyurCore frontend

**Owner:** Member 2. This directory holds the new frontend, built **from scratch**.

- **Integration boundary: the backend HTTP API only.**
  - Read `docs/FRONTEND_HANDOFF.md` first: endpoints, auth, errors, UI states, acceptance checklist.
  - The authoritative contract is `AyurCore_API_Contract.md` at the repository root.
- **Do not copy, import or extend** the legacy `frontend/` (Next.js) or `ui/` (Streamlit). They are legacy material, to be retired separately.
- **Framework and tooling are the owner's choice.** Keep all frontend dependencies and build output inside `apps/web/`; the root `.gitignore` already ignores `node_modules/`, `.next/`, `dist/`, `build/`, `coverage/` and similar.
- **Configuration:**
  - the API base URL should be the only setting;
  - commit a `.env.example` with placeholder values only;
  - never commit `.env*` files with real values;
  - **never put AI, transcription or backend secrets in the browser**.
- **Development origin:** the backend's CORS allows only `http://localhost:3000` and `http://127.0.0.1:3000`. Serve the app from one of those, or request a backend change through the contract process in `CONTRIBUTING.md`.
- **Backend for development:** a separate, non-production copy with its own fresh database (`docs/FRONTEND_HANDOFF.md` §12). Never the clinic's database or its running API.
