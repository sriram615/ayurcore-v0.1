/**
 * REAL replies of POST /patients/{patient_pk}/ask, captured from the backend (ai2 + backend/main.py at the time of Frontend Block 8) running
 * against a fresh disposable SQLite database holding SYNTHETIC patients only. Nothing here is hand-written: the values, qualifiers, query
 * plans and cited records are exactly what the backend returned, including the verifier's template `text` that the panel must never show.
 * The patient_pk is the sandbox's; tests re-key it with `reply(key, pk)`.
 *
 * Patients: "anjali" (4 complete prescriptions, 1 visit), "empty" (none), "malayalam" (Malayalam name, 1 prescription),
 * "legacy" (2 structured prescriptions and a legacy one with no recorded dosha or herbs, dated last).
 * Regenerate by re-running the questions against a fresh sandbox; do not edit by hand.
 */
export const REAL: Record<string, Record<string, unknown>> = {
 "anjali: What was the first prescription?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first prescription: prescription rx:1; created_at 2026-10-10 05:40:36; dosha Vata; herbs Ashwagandha, 30, Neem, 10.",
  "value": {
   "prescription": "rx:1",
   "created_at": "2026-10-10 05:40:36",
   "dosha": "Vata",
   "herbs": [
    [
     "Ashwagandha",
     30
    ],
    [
     "Neem",
     10
    ]
   ]
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_RX",
  "plan_query": {
   "op": "first",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: What is the latest prescription?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "last prescription: prescription rx:4; created_at 2026-10-10 05:40:36; dosha Kapha; herbs Guggulu, 12, Neem, 10.",
  "value": {
   "prescription": "rx:4",
   "created_at": "2026-10-10 05:40:36",
   "dosha": "Kapha",
   "herbs": [
    [
     "Guggulu",
     12
    ],
    [
     "Neem",
     10
    ]
   ]
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LAST_RX",
  "plan_query": {
   "op": "last",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: When was Neem first prescribed?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first herb Neem: prescription rx:1; created_at 2026-10-10 05:40:36; grams 10.",
  "value": {
   "prescription": "rx:1",
   "created_at": "2026-10-10 05:40:36",
   "grams": 10
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_HERB",
  "plan_query": {
   "op": "first",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: When was Neem last prescribed?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "last herb Neem: prescription rx:4; created_at 2026-10-10 05:40:36; grams 10.",
  "value": {
   "prescription": "rx:4",
   "created_at": "2026-10-10 05:40:36",
   "grams": 10
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LAST_HERB",
  "plan_query": {
   "op": "last",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Which dosha is on the first prescription?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first dosha: prescription rx:1; dosha Vata.",
  "value": {
   "prescription": "rx:1",
   "dosha": "Vata"
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_DOSHA",
  "plan_query": {
   "op": "first",
   "target": "dosha",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Her dosha?": {
  "patient_pk": 1,
  "outcome": "clarify",
  "category": "clarify",
  "stage": "clarify",
  "reason": "UNDERSPECIFIED",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [
   {
    "frame": "LIST_DOSHA",
    "text": "The dosha of each prescription, oldest first.",
    "query": {
     "op": "list",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "LAST_DOSHA",
    "text": "The dosha recorded on the latest prescription.",
    "query": {
     "op": "last",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "FIRST_DOSHA",
    "text": "The dosha recorded on the first prescription.",
    "query": {
     "op": "first",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   }
  ],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "anjali: When did her most recent visit end?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "last encounter: encounter enc:49f2ecc2fb8fb055; opened_at 2026-10-10 05:40:36; closed_at unknown.",
  "value": {
   "encounter": "enc:49f2ecc2fb8fb055",
   "opened_at": "2026-10-10 05:40:36",
   "closed_at": null
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "encounter",
    "label": "enc:49f2ecc2fb8fb055",
    "prescription_id": null,
    "created_at": "2026-10-10 05:40:36",
    "status": "OPEN",
    "dosha": null,
    "herbs": null,
    "encounter_ref": "49f2ecc2fb8fb055",
    "closed_at": null
   }
  ],
  "plan_frame": "LAST_ENC",
  "plan_query": {
   "op": "last",
   "target": "encounter",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Is her latest visit still open?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "last encounter: encounter enc:49f2ecc2fb8fb055; opened_at 2026-10-10 05:40:36; closed_at unknown.",
  "value": {
   "encounter": "enc:49f2ecc2fb8fb055",
   "opened_at": "2026-10-10 05:40:36",
   "closed_at": null
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "encounter",
    "label": "enc:49f2ecc2fb8fb055",
    "prescription_id": null,
    "created_at": "2026-10-10 05:40:36",
    "status": "OPEN",
    "dosha": null,
    "herbs": null,
    "encounter_ref": "49f2ecc2fb8fb055",
    "closed_at": null
   }
  ],
  "plan_frame": "LAST_ENC",
  "plan_query": {
   "op": "last",
   "target": "encounter",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: When was her first visit?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first encounter: encounter enc:49f2ecc2fb8fb055; opened_at 2026-10-10 05:40:36; closed_at unknown.",
  "value": {
   "encounter": "enc:49f2ecc2fb8fb055",
   "opened_at": "2026-10-10 05:40:36",
   "closed_at": null
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "encounter",
    "label": "enc:49f2ecc2fb8fb055",
    "prescription_id": null,
    "created_at": "2026-10-10 05:40:36",
    "status": "OPEN",
    "dosha": null,
    "herbs": null,
    "encounter_ref": "49f2ecc2fb8fb055",
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_ENC",
  "plan_query": {
   "op": "first",
   "target": "encounter",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: How many prescriptions has she had?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "count prescription: 4.",
  "value": 4,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:2",
    "prescription_id": 2,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Pitta",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Brahmi",
      "grams": 5
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_RX",
  "plan_query": {
   "op": "count",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: How many visits has she had?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "count encounter: 1.",
  "value": 1,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "encounter",
    "label": "enc:49f2ecc2fb8fb055",
    "prescription_id": null,
    "created_at": "2026-10-10 05:40:36",
    "status": "OPEN",
    "dosha": null,
    "herbs": null,
    "encounter_ref": "49f2ecc2fb8fb055",
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_ENC",
  "plan_query": {
   "op": "count",
   "target": "encounter",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: How many times was Neem prescribed?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "count herb Neem: 3.",
  "value": 3,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_HERB",
  "plan_query": {
   "op": "count",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: How many prescriptions were recorded as Vata only?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "count dosha Vata: 2.",
  "value": 2,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_DOSHA",
  "plan_query": {
   "op": "count",
   "target": "dosha",
   "herb": null,
   "dosha": "Vata",
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: How much Neem in total?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "sum herb Neem: 40.",
  "value": 40,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "SUM_HERB",
  "plan_query": {
   "op": "sum",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: List all her prescriptions.": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "list prescription: rx:1, rx:2, rx:3, rx:4.",
  "value": [
   "rx:1",
   "rx:2",
   "rx:3",
   "rx:4"
  ],
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:2",
    "prescription_id": 2,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Pitta",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Brahmi",
      "grams": 5
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_RX",
  "plan_query": {
   "op": "list",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: List all her visits.": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "list encounter: enc:49f2ecc2fb8fb055.",
  "value": [
   "enc:49f2ecc2fb8fb055"
  ],
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "encounter",
    "label": "enc:49f2ecc2fb8fb055",
    "prescription_id": null,
    "created_at": "2026-10-10 05:40:36",
    "status": "OPEN",
    "dosha": null,
    "herbs": null,
    "encounter_ref": "49f2ecc2fb8fb055",
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_ENC",
  "plan_query": {
   "op": "list",
   "target": "encounter",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Which herbs has she been given?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "list herb: Ashwagandha, Neem, Brahmi, Punarnava, Guggulu.",
  "value": [
   "Ashwagandha",
   "Neem",
   "Brahmi",
   "Punarnava",
   "Guggulu"
  ],
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:2",
    "prescription_id": 2,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Pitta",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Brahmi",
      "grams": 5
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
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
 "anjali: List the dosha of each prescription.": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "list dosha: prescription rx:1; dosha Vata, prescription rx:2; dosha Pitta, prescription rx:3; dosha Vata, prescription rx:4; dosha Kapha.",
  "value": [
   {
    "prescription": "rx:1",
    "dosha": "Vata"
   },
   {
    "prescription": "rx:2",
    "dosha": "Pitta"
   },
   {
    "prescription": "rx:3",
    "dosha": "Vata"
   },
   {
    "prescription": "rx:4",
    "dosha": "Kapha"
   }
  ],
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:2",
    "prescription_id": 2,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Pitta",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Brahmi",
      "grams": 5
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
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
 "anjali: What changed from the previous prescription to the latest?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "diff prescription: a rx:3; b rx:4; added Guggulu, 12; removed Ashwagandha, 40, Punarnava, 8; changed Neem, 20, 10; dosha_a Vata; dosha_b Kapha; dosha_changed True.",
  "value": {
   "a": "rx:3",
   "b": "rx:4",
   "added": [
    [
     "Guggulu",
     12
    ]
   ],
   "removed": [
    [
     "Ashwagandha",
     40
    ],
    [
     "Punarnava",
     8
    ]
   ],
   "changed": [
    [
     "Neem",
     20,
     10
    ]
   ],
   "dosha_a": "Vata",
   "dosha_b": "Kapha",
   "dosha_changed": true
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "DIFF_PREV",
  "plan_query": {
   "op": "diff",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Did the Neem dose change since the previous prescription?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "compare herb Neem: a rx:3; b rx:4; grams_a 20; grams_b 10; delta -10.",
  "value": {
   "a": "rx:3",
   "b": "rx:4",
   "grams_a": 20,
   "grams_b": 10,
   "delta": -10
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "CMP_HERB_PREV",
  "plan_query": {
   "op": "compare",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Compare Ashwagandha with the first prescription.": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "compare herb Ashwagandha: a rx:1; b rx:4; grams_a 30; grams_b 0; delta -30.",
  "value": {
   "a": "rx:1",
   "b": "rx:4",
   "grams_a": 30,
   "grams_b": 0,
   "delta": -30
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "CMP_HERB_FIRST",
  "plan_query": {
   "op": "compare",
   "target": "herb",
   "herb": "Ashwagandha",
   "dosha": null,
   "last_n": null,
   "a": "first",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Did the dosha change?": {
  "patient_pk": 1,
  "outcome": "clarify",
  "category": "clarify",
  "stage": "clarify",
  "reason": "UNDERSPECIFIED",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [
   {
    "frame": "CMP_DOSHA_PREV",
    "text": "The dosha on the previous prescription compared with the latest one.",
    "query": {
     "op": "compare",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "CMP_DOSHA_FIRST",
    "text": "The dosha on the first prescription compared with the latest one.",
    "query": {
     "op": "compare",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "first",
     "b": "last"
    }
   }
  ],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "anjali: What herbs appear across her three most recent prescriptions?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "list herb: Ashwagandha, Brahmi, Neem, Punarnava, Guggulu.",
  "value": [
   "Ashwagandha",
   "Brahmi",
   "Neem",
   "Punarnava",
   "Guggulu"
  ],
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:2",
    "prescription_id": 2,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Pitta",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Brahmi",
      "grams": 5
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_HERB_N",
  "plan_query": {
   "op": "list",
   "target": "herb",
   "herb": null,
   "dosha": null,
   "last_n": 3,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: How much Neem in her last 2 prescriptions?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "sum herb Neem: 30.",
  "value": 30,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "SUM_HERB_N",
  "plan_query": {
   "op": "sum",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": 2,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: When was Guggulu first prescribed?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first herb Guggulu: prescription rx:4; created_at 2026-10-10 05:40:36; grams 12.",
  "value": {
   "prescription": "rx:4",
   "created_at": "2026-10-10 05:40:36",
   "grams": 12
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_HERB",
  "plan_query": {
   "op": "first",
   "target": "herb",
   "herb": "Guggulu",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: How much Kutki in total?": {
  "patient_pk": 1,
  "outcome": "abstain",
  "category": "missing",
  "stage": "executor",
  "reason": "NOT_RECORDED",
  "text": "Not recorded: sum herb Kutki.",
  "value": null,
  "qualifier": "not_recorded",
  "verified": true,
  "options": [],
  "evidence": [],
  "plan_frame": "SUM_HERB",
  "plan_query": {
   "op": "sum",
   "target": "herb",
   "herb": "Kutki",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Neem count?": {
  "patient_pk": 1,
  "outcome": "clarify",
  "category": "clarify",
  "stage": "clarify",
  "reason": "UNDERSPECIFIED",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [
   {
    "frame": "COUNT_HERB",
    "text": "The number of prescriptions containing Neem.",
    "query": {
     "op": "count",
     "target": "herb",
     "herb": "Neem",
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "SUM_HERB",
    "text": "The total grams of Neem across all prescriptions.",
    "query": {
     "op": "sum",
     "target": "herb",
     "herb": "Neem",
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   }
  ],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "legacy: What is the latest prescription?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: last prescription: prescription rx:9; created_at 2099-01-01 05:30:00; dosha unknown; herbs unknown.",
  "value": {
   "prescription": "rx:9",
   "created_at": "2099-01-01 05:30:00",
   "dosha": null,
   "herbs": null
  },
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LAST_RX",
  "plan_query": {
   "op": "last",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: What was the first prescription?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first prescription: prescription rx:6; created_at 2026-10-10 05:40:36; dosha Vata; herbs Neem, 10, Brahmi, 4.",
  "value": {
   "prescription": "rx:6",
   "created_at": "2026-10-10 05:40:36",
   "dosha": "Vata",
   "herbs": [
    [
     "Neem",
     10
    ],
    [
     "Brahmi",
     4
    ]
   ]
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_RX",
  "plan_query": {
   "op": "first",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: How much Neem in total?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "At least sum herb Neem: 16.",
  "value": 16,
  "qualifier": "at_least",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:7",
    "prescription_id": 7,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Neem",
      "grams": 6
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "SUM_HERB",
  "plan_query": {
   "op": "sum",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: How many times was Neem prescribed?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "At least count herb Neem: 2.",
  "value": 2,
  "qualifier": "at_least",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:7",
    "prescription_id": 7,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Neem",
      "grams": 6
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_HERB",
  "plan_query": {
   "op": "count",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: When was Neem last prescribed?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: last herb Neem: prescription rx:7; created_at 2026-10-10 05:40:36; grams 6.",
  "value": {
   "prescription": "rx:7",
   "created_at": "2026-10-10 05:40:36",
   "grams": 6
  },
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:7",
    "prescription_id": 7,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Neem",
      "grams": 6
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LAST_HERB",
  "plan_query": {
   "op": "last",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: When was Neem first prescribed?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first herb Neem: prescription rx:6; created_at 2026-10-10 05:40:36; grams 10.",
  "value": {
   "prescription": "rx:6",
   "created_at": "2026-10-10 05:40:36",
   "grams": 10
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_HERB",
  "plan_query": {
   "op": "first",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: Her dosha?": {
  "patient_pk": 4,
  "outcome": "clarify",
  "category": "clarify",
  "stage": "clarify",
  "reason": "UNDERSPECIFIED",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [
   {
    "frame": "LIST_DOSHA",
    "text": "The dosha of each prescription, oldest first.",
    "query": {
     "op": "list",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "LAST_DOSHA",
    "text": "The dosha recorded on the latest prescription.",
    "query": {
     "op": "last",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "FIRST_DOSHA",
    "text": "The dosha recorded on the first prescription.",
    "query": {
     "op": "first",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   }
  ],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "legacy: Which dosha is on the first prescription?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "first dosha: prescription rx:6; dosha Vata.",
  "value": {
   "prescription": "rx:6",
   "dosha": "Vata"
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "FIRST_DOSHA",
  "plan_query": {
   "op": "first",
   "target": "dosha",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: List the dosha of each prescription.": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: list dosha: prescription rx:6; dosha Vata, prescription rx:7; dosha Kapha.",
  "value": [
   {
    "prescription": "rx:6",
    "dosha": "Vata"
   },
   {
    "prescription": "rx:7",
    "dosha": "Kapha"
   }
  ],
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:7",
    "prescription_id": 7,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Neem",
      "grams": 6
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
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
 "legacy: Which herbs has she been given?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: list herb: Neem, Brahmi.",
  "value": [
   "Neem",
   "Brahmi"
  ],
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
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
 "legacy: What changed from the previous prescription to the latest?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "From the known records only: diff prescription: a rx:7; b rx:9; added unknown; removed unknown; changed unknown; dosha_a Kapha; dosha_b unknown; dosha_changed unknown.",
  "value": {
   "a": "rx:7",
   "b": "rx:9",
   "added": null,
   "removed": null,
   "changed": null,
   "dosha_a": "Kapha",
   "dosha_b": null,
   "dosha_changed": null
  },
  "qualifier": "partial",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:7",
    "prescription_id": 7,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Neem",
      "grams": 6
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "DIFF_PREV",
  "plan_query": {
   "op": "diff",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: Did the Neem dose change since the previous prescription?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "Unknown from the record: compare herb Neem depends on records with unknown values.",
  "value": null,
  "qualifier": "unknown",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "CMP_HERB_PREV",
  "plan_query": {
   "op": "compare",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: Did the dosha change?": {
  "patient_pk": 4,
  "outcome": "clarify",
  "category": "clarify",
  "stage": "clarify",
  "reason": "UNDERSPECIFIED",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [
   {
    "frame": "CMP_DOSHA_PREV",
    "text": "The dosha on the previous prescription compared with the latest one.",
    "query": {
     "op": "compare",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "CMP_DOSHA_FIRST",
    "text": "The dosha on the first prescription compared with the latest one.",
    "query": {
     "op": "compare",
     "target": "dosha",
     "herb": null,
     "dosha": null,
     "last_n": null,
     "a": "first",
     "b": "last"
    }
   }
  ],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "legacy: How many prescriptions were recorded as Vata only?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "incomplete",
  "stage": "done",
  "reason": null,
  "text": "At least count dosha Vata: 1.",
  "value": 1,
  "qualifier": "at_least",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_DOSHA",
  "plan_query": {
   "op": "count",
   "target": "dosha",
   "herb": null,
   "dosha": "Vata",
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: How many prescriptions has she had?": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "count prescription: 3.",
  "value": 3,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:7",
    "prescription_id": 7,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Neem",
      "grams": 6
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_RX",
  "plan_query": {
   "op": "count",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "legacy: List all her prescriptions.": {
  "patient_pk": 4,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "list prescription: rx:6, rx:7, rx:9.",
  "value": [
   "rx:6",
   "rx:7",
   "rx:9"
  ],
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:6",
    "prescription_id": 6,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Neem",
      "grams": 10
     },
     {
      "name": "Brahmi",
      "grams": 4
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:7",
    "prescription_id": 7,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Neem",
      "grams": 6
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:9",
    "prescription_id": 9,
    "created_at": "2099-01-01 05:30:00",
    "status": "CONFIRMED",
    "dosha": null,
    "herbs": null,
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LIST_RX",
  "plan_query": {
   "op": "list",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Should we increase the Neem dose?": {
  "patient_pk": 1,
  "outcome": "abstain",
  "category": "unsafe",
  "stage": "screen",
  "reason": "TREATMENT",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "anjali: What disease does she have?": {
  "patient_pk": 1,
  "outcome": "abstain",
  "category": "unsafe",
  "stage": "screen",
  "reason": "DIAGNOSIS",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "anjali: What did the other patient get?": {
  "patient_pk": 1,
  "outcome": "abstain",
  "category": "access_denied",
  "stage": "screen",
  "reason": "OTHER_PATIENT",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "anjali: What is the average Neem dose per prescription?": {
  "patient_pk": 1,
  "outcome": "abstain",
  "category": "unsupported",
  "stage": "coverage",
  "reason": "AMBIGUOUS",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "anjali: Is the Neem dose rising from visit to visit?": {
  "patient_pk": 1,
  "outcome": "abstain",
  "category": "unsupported",
  "stage": "coverage",
  "reason": "AMBIGUOUS",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "malayalam: How much Kutki in total?": {
  "patient_pk": 3,
  "outcome": "abstain",
  "category": "missing",
  "stage": "executor",
  "reason": "NOT_RECORDED",
  "text": "Not recorded: sum herb Kutki.",
  "value": null,
  "qualifier": "not_recorded",
  "verified": true,
  "options": [],
  "evidence": [],
  "plan_frame": "SUM_HERB",
  "plan_query": {
   "op": "sum",
   "target": "herb",
   "herb": "Kutki",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "empty: What was the first prescription?": {
  "patient_pk": 2,
  "outcome": "abstain",
  "category": "missing",
  "stage": "executor",
  "reason": "NOT_RECORDED",
  "text": "Not recorded: first prescription.",
  "value": null,
  "qualifier": "not_recorded",
  "verified": true,
  "options": [],
  "evidence": [],
  "plan_frame": "FIRST_RX",
  "plan_query": {
   "op": "first",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "empty: How many visits has she had?": {
  "patient_pk": 2,
  "outcome": "abstain",
  "category": "missing",
  "stage": "executor",
  "reason": "NOT_RECORDED",
  "text": "count encounter: 0.",
  "value": 0,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [],
  "plan_frame": "COUNT_ENC",
  "plan_query": {
   "op": "count",
   "target": "encounter",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "malayalam: അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?": {
  "patient_pk": 3,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "last prescription: prescription rx:5; created_at 2026-10-10 05:40:36; dosha Vata; herbs Ashwagandha, 15.",
  "value": {
   "prescription": "rx:5",
   "created_at": "2026-10-10 05:40:36",
   "dosha": "Vata",
   "herbs": [
    [
     "Ashwagandha",
     15
    ]
   ]
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:5",
    "prescription_id": 5,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 15
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "LAST_RX",
  "plan_query": {
   "op": "last",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Latest prescription-il puthuthayi enthaanu cherthath?": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "diff prescription: a rx:3; b rx:4; added Guggulu, 12; removed Ashwagandha, 40, Punarnava, 8; changed Neem, 20, 10; dosha_a Vata; dosha_b Kapha; dosha_changed True.",
  "value": {
   "a": "rx:3",
   "b": "rx:4",
   "added": [
    [
     "Guggulu",
     12
    ]
   ],
   "removed": [
    [
     "Ashwagandha",
     40
    ],
    [
     "Punarnava",
     8
    ]
   ],
   "changed": [
    [
     "Neem",
     20,
     10
    ]
   ],
   "dosha_a": "Vata",
   "dosha_b": "Kapha",
   "dosha_changed": true
  },
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "DIFF_PREV",
  "plan_query": {
   "op": "diff",
   "target": "prescription",
   "herb": null,
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": false
 },
 "anjali: Neem amount?": {
  "patient_pk": 1,
  "outcome": "clarify",
  "category": "clarify",
  "stage": "clarify",
  "reason": "UNDERSPECIFIED",
  "text": null,
  "value": null,
  "qualifier": null,
  "verified": false,
  "options": [
   {
    "frame": "SUM_HERB",
    "text": "The total grams of Neem across all prescriptions.",
    "query": {
     "op": "sum",
     "target": "herb",
     "herb": "Neem",
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   },
   {
    "frame": "LAST_HERB",
    "text": "When Neem was last prescribed, and the grams then.",
    "query": {
     "op": "last",
     "target": "herb",
     "herb": "Neem",
     "dosha": null,
     "last_n": null,
     "a": "previous",
     "b": "last"
    }
   }
  ],
  "evidence": [],
  "plan_frame": null,
  "plan_query": null,
  "clarified": false
 },
 "anjali: Neem count? [choice COUNT_HERB]": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "count herb Neem: 3.",
  "value": 3,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "COUNT_HERB",
  "plan_query": {
   "op": "count",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": true
 },
 "anjali: Neem count? [choice SUM_HERB]": {
  "patient_pk": 1,
  "outcome": "answer",
  "category": "answer",
  "stage": "done",
  "reason": null,
  "text": "sum herb Neem: 40.",
  "value": 40,
  "qualifier": "exact",
  "verified": true,
  "options": [],
  "evidence": [
   {
    "kind": "prescription",
    "label": "rx:1",
    "prescription_id": 1,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 30
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:3",
    "prescription_id": 3,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Vata",
    "herbs": [
     {
      "name": "Ashwagandha",
      "grams": 40
     },
     {
      "name": "Neem",
      "grams": 20
     },
     {
      "name": "Punarnava",
      "grams": 8
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   },
   {
    "kind": "prescription",
    "label": "rx:4",
    "prescription_id": 4,
    "created_at": "2026-10-10 05:40:36",
    "status": "Active",
    "dosha": "Kapha",
    "herbs": [
     {
      "name": "Guggulu",
      "grams": 12
     },
     {
      "name": "Neem",
      "grams": 10
     }
    ],
    "encounter_ref": null,
    "closed_at": null
   }
  ],
  "plan_frame": "SUM_HERB",
  "plan_query": {
   "op": "sum",
   "target": "herb",
   "herb": "Neem",
   "dosha": null,
   "last_n": null,
   "a": "previous",
   "b": "last"
  },
  "clarified": true
 }
}

export type RealKey = keyof typeof REAL

/** A deep copy of a real reply, about patient `pk` (the only thing changed), with optional overrides. */
export function reply(key: string, pk = 5, overrides: object = {}): Record<string, unknown> {
  const found = REAL[key]
  if (!found) throw new Error(`no captured reply for ${key}`)
  return { ...JSON.parse(JSON.stringify(found)), patient_pk: pk, ...overrides }
}
