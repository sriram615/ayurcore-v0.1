"""
AI-2 Consultation Memory -- deterministic foundation (evaluation module, not a production API).

    snapshot.py   PatientSnapshot, read-only, from confirmed prescriptions and encounters,
                  through the existing store functions only
    timeline.py   chronological Timeline with stable source labels
    executor.py   fixed operations first / last / count / sum / list / diff / compare,
                  every result carrying its evidence
    answer.py     deterministic template claims (no model)
    verifier.py   deterministic claim / citation verifier (reject = abstain; never repairs)
    s2_build.py   materialises the synthetic S2 histories through the real API into an
                  isolated database

No LLM or learned model is used here. Unknown historical values are kept as unknown
and never inferred. SYNTHETIC DATA ONLY; NOT CLINICAL VALIDATION.
"""
