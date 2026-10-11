/**
 * REAL replies of POST /patients/{patient_pk}/ask for a patient whose prescriptions ALL have unknown herbs and dosha (legacy-style rows with no
 * structured data): the backend answers a list question with outcome "answer", category "incomplete", qualifier "partial" and an EMPTY list,
 * citing the unknown prescriptions. Captured from the backend (ai2 + backend/main.py) running in-process against a fresh disposable SQLite
 * database holding SYNTHETIC data only (three prescriptions with no recorded herbs or dosha); nothing here is hand-written. The values are
 * identifiers, enum words and fixed timestamps: no names, credentials, clinic identifiers or real patient data. The verifier's template text
 * is kept because the panel must never show it. patient_pk is a placeholder; tests re-key it with emptyReply(key, pk).
 * Regenerate by re-asking the questions against a fresh sandbox; do not edit by hand.
 */
export const REAL_EMPTY: Record<string, Record<string, unknown>> = {
 "allunknown: List all the herbs prescribed so far": {
  "patient_pk": 0,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: list herb: none.",
  "value": [],
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:101",
    "prescription_id": 101,
    "created_at": "2026-03-01 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:102",
    "prescription_id": 102,
    "created_at": "2026-03-02 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:103",
    "prescription_id": 103,
    "created_at": "2026-03-03 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_HERB",
  "plan_query": {
   "op": "list",
   "target": "herb",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "allunknown: Which herbs have been prescribed?": {
  "patient_pk": 0,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: list herb: none.",
  "value": [],
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:101",
    "prescription_id": 101,
    "created_at": "2026-03-01 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:102",
    "prescription_id": 102,
    "created_at": "2026-03-02 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:103",
    "prescription_id": 103,
    "created_at": "2026-03-03 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_HERB",
  "plan_query": {
   "op": "list",
   "target": "herb",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "allunknown: List the dosha of each prescription": {
  "patient_pk": 0,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: list dosha: none.",
  "value": [],
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:101",
    "prescription_id": 101,
    "created_at": "2026-03-01 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:102",
    "prescription_id": 102,
    "created_at": "2026-03-02 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:103",
    "prescription_id": 103,
    "created_at": "2026-03-03 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_DOSHA",
  "plan_query": {
   "op": "list",
   "target": "dosha",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "allunknown: What were the doshas recorded on each prescription?": {
  "patient_pk": 0,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: list dosha: none.",
  "value": [],
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:101",
    "prescription_id": 101,
    "created_at": "2026-03-01 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:102",
    "prescription_id": 102,
    "created_at": "2026-03-02 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:103",
    "prescription_id": 103,
    "created_at": "2026-03-03 09:00:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_DOSHA",
  "plan_query": {
   "op": "list",
   "target": "dosha",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 }
}

/** A deep copy of a captured reply, about patient `pk` (the only thing changed), with optional overrides. */
export function emptyReply(key: string, pk = 5, overrides: object = {}): Record<string, unknown> {
  const found = REAL_EMPTY[key]
  if (!found) throw new Error(`no captured reply for ${key}`)
  return { ...JSON.parse(JSON.stringify(found)), patient_pk: pk, ...overrides }
}
