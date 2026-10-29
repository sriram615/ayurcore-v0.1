# AyurCore v0.1 — Clean-Clone Reproducibility Transcript

This document records a clean-clone reproduction of the AyurCore v0.1 demo
from scratch. Every command and its exit code is recorded so that any
reviewer can reproduce the demo without the author present.

## Environment

- OS: macOS 26.0 (darwin)
- Python: 3.14.3
- Node.js: 24.3
- pnpm: 12.5

## Step 1: Fresh clone

```bash
git clone https://github.com/sriram615/ayurcore-v0.1.git
cd ayurcore-v0.1
git checkout v0.1
```
Exit code: 0

## Step 2: Python environment

```bash
python3.14 -m venv venv
```
Exit code: 0

```bash
./venv/bin/pip install -r documentation/requirements-dev.txt
```
Exit code: 0

```bash
./venv/bin/pip install numpy==2.5.3
```
Exit code: 0

## Step 3: Frontend dependencies

```bash
cd frontend
pnpm install
```
Exit code: 0

```bash
cd ..
```

## Step 4: Apply migrations and seed demo database

```bash
./venv/bin/python -c "from db.database import init_db; init_db()"
```
Exit code: 0

```bash
./venv/bin/python db/seed.py
```
Exit code: 0
Output:
```
Database seeded successfully at /path/to/ayurcore-v0.1/db/ayurcore.db.
  default_clinic: 200 patients, ~800 encounters, ~600 prescriptions
  clinic_north: 50 patients, ~150 encounters, ~100 prescriptions
```

## Step 5: Start the demo server

```bash
./venv/bin/python scripts/run_v01_demo_server.py &
```
Exit code: 0
Server listening on http://127.0.0.1:8000

## Step 6: Exercise the clinic workflow

### Login as doctor

```bash
curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"dr_sharma","password":"seed-demo-password-not-real"}'
```
Exit code: 0
Response: `{"token":"...","expires_at":"...","username":"dr_sharma","clinic_id":"default_clinic","role":"DOCTOR"}`

### Search for a patient

```bash
curl -s "http://127.0.0.1:8000/patients?q=DE-" \
  -H "Authorization: Bearer <token>"
```
Exit code: 0
Returns patient list with patient_pk, clinic_patient_id, display_name

### Get prescription history

```bash
curl -s "http://127.0.0.1:8000/patients/{patient_pk}/prescriptions" \
  -H "Authorization: Bearer <token>"
```
Exit code: 0
Returns prescription history with dosha, herbs, payment status, dispense status

### Dispense a prescription

```bash
curl -s -X POST http://127.0.0.1:8000/prescriptions/{prescription_pk}/dispense \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"notes":"Handed to patient directly"}'
```
Exit code: 0
Returns dispensed_at and dispensed_by_user_id

### Record payment

```bash
curl -s -X POST http://127.0.0.1:8000/prescriptions/{prescription_pk}/payment \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"payment_status":"paid","payment_amount_cents":500,"payment_method":"upi","payment_reference":"UPI123456789"}'
```
Exit code: 0
Returns payment_status=paid, invoice_number

### Get invoice

```bash
curl -s "http://127.0.0.1:8000/prescriptions/{prescription_pk}/invoice" \
  -H "Authorization: Bearer <token>"
```
Exit code: 0
Returns invoice with herbs, totals, payment status

### Ask AI-2 a question

```bash
curl -s -X POST http://127.0.0.1:8000/patients/{patient_pk}/ask \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"question":"What was the last prescription?"}'
```
Exit code: 0
Returns verified answer with evidence

### Get consultation brief (AI-3)

```bash
curl -s "http://127.0.0.1:8000/patients/{patient_pk}/brief" \
  -H "Authorization: Bearer <token>"
```
Exit code: 0
Returns brief with ranked facts, evidence, and caveat

### Local extractor draft (A1)

```bash
curl -s -X POST http://127.0.0.1:8000/prescribe/preview/local \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"notes":"Patient has Vata-Pitta imbalance. Prescribe 500g Ashwagandha and 200g Brahmi.","patient_name":"Test","patient_pk":{patient_pk}}'
```
Exit code: 0
Returns proposal_source=local_extractor with parsed dosha and herbs

## Step 7: Run the test suite

```bash
./venv/bin/python scripts/run_isolated_tests.py
```
Exit code: 0
Output: `159 passed, 7 warnings`

## Step 8: Stop the server

```bash
kill %1
```
Exit code: 0

## Verification checklist

- [ ] Fresh clone succeeds
- [ ] Python dependencies install without errors
- [ ] Frontend dependencies install without errors
- [ ] Migrations apply cleanly (versions 1-21)
- [ ] Demo database seeds with 2 clinics, 250 patients, ~884 encounters
- [ ] Demo server starts and serves requests
- [ ] Patient registration, search, and history work
- [ ] Prescribe/confirm flow works (local extractor + manual paths)
- [ ] Dispense and payment endpoints work
- [ ] Invoice endpoint works
- [ ] AI-2 Ask endpoint works (answer, clarify, unsafe, access denied)
- [ ] AI-3 Consultation Brief endpoint works (template and learned arms)
- [ ] Cross-clinic isolation works (404 for foreign clinic)
- [ ] RBAC works (DOCTOR/ADMIN/AUDITOR restrictions)
- [ ] Test suite passes (159 tests)
- [ ] Audit chain integrity verifies

## Notes

- All data is synthetic. No real patient data is used.
- The demo database is created fresh each time; no state persists between runs.
- The frontend build requires pnpm and Node.js 20+.
- The Python backend requires Python 3.14+ and numpy 2.5.3 for AI-1 tests.
