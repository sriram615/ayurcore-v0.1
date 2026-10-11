"""
AI-3 Consultation Brief -- a local, deterministic-at-inference, read-only brief of one patient's recorded history.

    from ai3.brief import make_brief
    brief = make_brief(snapshot, as_of="2026-10-10")             # learned arm (default); arm="template" for the baseline

No LLM, no network, no database access: it takes the same read-only PatientSnapshot as ai2 and cites the same records. See ai3/brief.py.
"""
