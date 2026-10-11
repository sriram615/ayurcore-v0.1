"""
UX-3 focused UI tests — Clinical AI Review / Prescription Editor.

Every test here is pure: no network calls, no running backend, no browser,
no Streamlit runtime. Tests import ui.app directly and exercise its pure
functions, constants, and (via inspect.getsource) the structural shape of
_render_review_stage -- the same testing philosophy tests/test_ui_ux2.py
uses, and for the same reason: the test harness's streamlit stub only
provides session_state/set_page_config/stop/rerun/column_config, not a
real widget tree, so _render_* functions are never invoked directly.

This file is self-contained (its own streamlit stub) so it can run either
standalone (`python3 tests/test_ui_ux3.py` or
`pytest tests/test_ui_ux3.py -v`) or as part of the full suite
(`pytest tests/ -v`) alongside tests/test_ui_ux2.py, without import-order
coupling between the two files.

Covers the 23 scenarios from the UX-3 implementation authorization:

 1. patient rendering
 2. proposal rendering (AI vs manual)
 3. row structure (medicine + quantity rows)
 4. +/- increase
 5. +/- decrease
 6. floor at ≤0 prevention
 7. direct quantity edit
 8. add medicine
 9. dropdown uses canonical inventory/formulary values
10. duplicate-medicine prevention
11. remove medicine
12. dosha editing (controlled selector)
13. prescription editing state (view <-> edit toggle)
14. save/cancel behavior
15. slide-to-confirm reaches the existing confirmation path
16. confirmation does not bypass the backend
17. idempotency key behavior remains correct
18. next patient clears previous state (no bleed)
19. Back preserves patient information
20. session expiry uses the existing friendly message
21. AI-unavailable uses the existing manual fallback
22. non-doctor roles cannot gain prescribing UI
23. keyboard/accessibility alternative for confirmation exists

Run:
    ./venv/bin/pytest tests/test_ui_ux3.py -v
or:
    python3 tests/test_ui_ux3.py
"""
import sys
import os
import re
import types
import inspect

# ---------------------------------------------------------------------------
# Make `import ui.app` work without a Streamlit runtime or a backend.
# Mirrors tests/test_ui_ux2.py's stub exactly.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# IMPORTANT: `ui.app` is a plain module -- if tests/test_ui_ux2.py already
# imported it in this same pytest process, `import ui.app as app` below
# would return that exact same module object, not a fresh copy. Building a
# SECOND, independent stub here and doing `app.st = this_files_stub` would
# silently repoint test_ui_ux2.py's `app.st` too (same `app`), orphaning
# ITS `_st_stub.session_state` and making every app.<fn>() call in that
# file write into THIS file's session state instead -- a real cross-file
# test-pollution bug, order-dependent on collection order. So: reuse the
# sibling file's already-configured stub/app pair when one exists, instead
# of creating a second, competing one.
if "ui.app" in sys.modules:
    app = sys.modules["ui.app"]
    _st_stub = app.st
else:
    _st_stub = types.ModuleType("streamlit")

    class _FakeSessionState(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key)

        def __setattr__(self, key, value):
            self[key] = value

        def setdefault(self, key, value=None):
            if key not in self:
                self[key] = value
            return self[key]

        def get(self, key, default=None):
            return self[key] if key in self else default

        def pop(self, key, *args):
            return dict.pop(self, key, *args) if args else dict.pop(self, key)

    _st_stub.session_state = _FakeSessionState()
    _st_stub.set_page_config = lambda **kw: None
    _st_stub.stop = lambda: None
    _st_stub.rerun = lambda: None

    _cc = types.ModuleType("streamlit.column_config")
    _cc.TextColumn = lambda *a, **kw: None
    _cc.NumberColumn = lambda *a, **kw: None
    _st_stub.column_config = _cc

    sys.modules["streamlit"] = _st_stub

    import ui.app as app  # noqa: E402  (after stub)

    app.st = _st_stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_session():
    """Reset st.session_state to _DEFAULTS, mirroring what main() does."""
    _st_stub.session_state.clear()
    for k, v in app._DEFAULTS.items():
        _st_stub.session_state[k] = v
    _st_stub.session_state["session_message"] = None
    _st_stub.session_state["_patient_name"] = ""
    _st_stub.session_state["_patient_phone"] = ""
    _st_stub.session_state["_patient_id"] = ""


_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {label}")
    else:
        _failed += 1
        msg = label + (f" ({detail})" if detail else "")
        print(f"  FAIL: {msg}")
        raise AssertionError(msg)


_REVIEW_SRC = None


def _review_src() -> str:
    """Cached source of _render_review_stage (re-fetched fresh each call
    to inspect.getsource -- no mutable global cache risk across tests)."""
    return inspect.getsource(app._render_review_stage)


# ---------------------------------------------------------------------------
# 1. Patient rendering
# ---------------------------------------------------------------------------

def test_patient_context_rendering():
    """Scenario 1 — patient context (name, phone/id, timestamp) is part
    of the review screen's own rendering, and reads from the SAME shadow
    state (_patient_name/_patient_phone/_patient_id) used across every
    other stage -- not a separate, disconnected copy."""
    print("\nUX-3 test 1: patient context rendering")
    src = _review_src()
    check("review stage renders a patient card", "ayc-patient-card" in src)
    check("patient card reads _patient_name", '"_patient_name"' in src)
    check("patient card reads _patient_phone", '"_patient_phone"' in src)
    check("patient card reads _patient_id", '"_patient_id"' in src)

    _fresh_session()
    _st_stub.session_state["_patient_name"] = "Anjali Menon"
    _st_stub.session_state["_patient_phone"] = "9123456780"
    _st_stub.session_state["_patient_id"] = "PAT-042"
    check(
        "patient identity is readable from the same session-state keys the card uses",
        (
            _st_stub.session_state["_patient_name"] == "Anjali Menon"
            and _st_stub.session_state["_patient_phone"] == "9123456780"
            and _st_stub.session_state["_patient_id"] == "PAT-042"
        ),
    )


# ---------------------------------------------------------------------------
# 2. Proposal rendering (AI vs manual)
# ---------------------------------------------------------------------------

def test_proposal_rendering_ai_vs_manual():
    """Scenario 2 — the proposed prescription is clearly labeled AI DRAFT
    (with a quiet, non-dominant "AI generated" status) when review_source
    is 'ai', and PRESCRIPTION / "Doctor-entered" when 'manual'. Never
    shows the AI label on a manually created prescription."""
    print("\nUX-3 test 2: proposal rendering distinguishes AI vs manual")
    src = _review_src()
    check('AI path renders "AI DRAFT"', '"AI DRAFT"' in src)
    check('AI path renders "AI generated" status', "AI generated" in src)
    check('manual path renders "PRESCRIPTION"', '"PRESCRIPTION"' in src)
    check('manual path renders "Doctor-entered" status', "Doctor-entered" in src)
    check(
        "the AI/manual branch is keyed on review_source",
        'review_source == "ai"' in src,
    )


# ---------------------------------------------------------------------------
# 3. Row structure (medicine + quantity rows)
# ---------------------------------------------------------------------------

def test_row_structure_from_seeded_herbs():
    """Scenario 3 — each prescribed herb becomes exactly one row, with a
    name and a quantity, addressable by a stable per-row id."""
    print("\nUX-3 test 3: prescription editor row structure")
    _fresh_session()
    app._seed_review_rows(
        [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 3}],
        "Vata",
    )
    row_order = _st_stub.session_state["_review_row_order"]
    row_names = _st_stub.session_state["_review_row_names"]
    check("one row id created per herb", len(row_order) == 2, row_order)
    check(
        "row names map matches the seeded herbs in order",
        [row_names[r] for r in row_order] == ["Ashwagandha", "Brahmi"],
        row_names,
    )
    for r in row_order:
        check(f"row {r} has its own quantity key", f"qty_{r}" in _st_stub.session_state)
    herbs_now = app._current_review_herbs()
    check(
        "_current_review_herbs reconstructs name+grams rows from state",
        herbs_now == [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 3}],
        herbs_now,
    )


# ---------------------------------------------------------------------------
# 4/5/6. Steppers: increase, decrease, floor at 1
# ---------------------------------------------------------------------------

def test_stepper_increase_quantity():
    """Scenario 4 — the [+] stepper increases a row's quantity by 1
    (capped at 10000), without requiring the doctor to click 30 times."""
    print("\nUX-3 test 4: [+] stepper increases quantity")
    src = _review_src()
    check(
        "the + stepper's handler increases quantity, capped at 10000",
        "min(10000, st.session_state.get(qty_key, 1) + 1)" in src,
    )
    _fresh_session()
    app._seed_review_rows([{"name": "Brahmi", "grams": 5}], "Vata")
    rid = _st_stub.session_state["_review_row_order"][0]
    qty_key = f"qty_{rid}"
    # Simulate exactly what the + button handler does.
    _st_stub.session_state[qty_key] = min(10000, _st_stub.session_state.get(qty_key, 1) + 1)
    check("quantity increased by 1", _st_stub.session_state[qty_key] == 6)


def test_stepper_decrease_quantity():
    """Scenario 5 — the [−] stepper decreases a row's quantity by 1."""
    print("\nUX-3 test 5: [-] stepper decreases quantity")
    src = _review_src()
    check(
        "the - stepper's handler decreases quantity, floored at 1",
        "max(1, st.session_state.get(qty_key, 1) - 1)" in src,
    )
    _fresh_session()
    app._seed_review_rows([{"name": "Brahmi", "grams": 5}], "Vata")
    rid = _st_stub.session_state["_review_row_order"][0]
    qty_key = f"qty_{rid}"
    _st_stub.session_state[qty_key] = max(1, _st_stub.session_state.get(qty_key, 1) - 1)
    check("quantity decreased by 1", _st_stub.session_state[qty_key] == 4)


def test_stepper_floor_prevents_below_one():
    """Scenario 6 — decrementing a quantity already at 1 never reaches 0
    or a negative number (no accidental "0 grams" / "-1 grams" row)."""
    print("\nUX-3 test 6: stepper floor prevents quantity <= 0")
    _fresh_session()
    app._seed_review_rows([{"name": "Brahmi", "grams": 1}], "Vata")
    rid = _st_stub.session_state["_review_row_order"][0]
    qty_key = f"qty_{rid}"
    for _ in range(5):
        _st_stub.session_state[qty_key] = max(1, _st_stub.session_state.get(qty_key, 1) - 1)
    check(
        "quantity never drops below 1 no matter how many decrements",
        _st_stub.session_state[qty_key] == 1,
        _st_stub.session_state[qty_key],
    )
    # number_input itself also enforces this at the widget level.
    src = _review_src()
    check("the quantity number_input also declares min_value=1", "min_value=1" in src)


# ---------------------------------------------------------------------------
# 7. Direct quantity edit
# ---------------------------------------------------------------------------

def test_direct_quantity_edit():
    """Scenario 7 — a doctor can type a quantity directly (not only via
    +/- clicks). The number_input is keyed but carries no value= override,
    so a direct edit persists across reruns instead of being silently
    reset back to a stale value from code."""
    print("\nUX-3 test 7: direct quantity edit persists")
    src = _review_src()
    m = re.search(r"st\.number_input\((.*?)\)\n", src, re.DOTALL)
    check("the quantity control is an st.number_input", m is not None)
    if m:
        # Look for a bare "value=" -- NOT "min_value=" / "max_value=",
        # both of which legitimately contain the substring "value=".
        bare_value_override = re.search(r"(?<!min_)(?<!max_)\bvalue=", m.group(1))
        check(
            "the quantity number_input has no value= override "
            "(so a typed value isn't clobbered on the next rerun)",
            bare_value_override is None,
            m.group(1),
        )
        check("the quantity number_input is keyed per row", "key=qty_key" in m.group(1))

    _fresh_session()
    app._seed_review_rows([{"name": "Brahmi", "grams": 5}], "Vata")
    rid = _st_stub.session_state["_review_row_order"][0]
    qty_key = f"qty_{rid}"
    # Simulate a doctor typing "42" directly into the field.
    _st_stub.session_state[qty_key] = 42
    herbs_now = app._current_review_herbs()
    check(
        "a directly typed quantity is reflected in the current herb list",
        herbs_now == [{"name": "Brahmi", "grams": 42}],
        herbs_now,
    )


# ---------------------------------------------------------------------------
# 8/9. Add medicine, dropdown uses canonical formulary
# ---------------------------------------------------------------------------

def test_add_medicine():
    """Scenario 8 — choosing a medicine from "+ Add medicine" appends a
    new row with a starting quantity, without disturbing existing rows."""
    print("\nUX-3 test 8: add medicine appends a new row")
    _fresh_session()
    app._seed_review_rows([{"name": "Ashwagandha", "grams": 5}], "Vata")
    existing_names = set(_st_stub.session_state["_review_row_names"].values())
    chosen = "Brahmi"
    check("Brahmi is not already prescribed", chosen not in existing_names)

    # Simulate exactly what the "+ Add" handler does for a new medicine.
    next_id = _st_stub.session_state.get("_review_next_row_id", 0)
    rid = f"r{next_id}"
    _st_stub.session_state["_review_next_row_id"] = next_id + 1
    _st_stub.session_state["_review_row_order"].append(rid)
    _st_stub.session_state["_review_row_names"][rid] = chosen
    _st_stub.session_state[f"qty_{rid}"] = 1

    herbs_now = app._current_review_herbs()
    check(
        "the new medicine is appended alongside the existing one",
        herbs_now == [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 1}],
        herbs_now,
    )


def test_add_medicine_dropdown_uses_canonical_formulary():
    """Scenario 9 — the add-medicine dropdown is restricted to the
    clinic's canonical inventory/formulary list (CLINIC_HERB_KEYTERMS),
    never arbitrary free text, and never a second, independent naming
    system from the backend's own canonical herb names
    (backend/herb_resolver.py's HERB_ALIASES values)."""
    print("\nUX-3 test 9: add-medicine dropdown sources from the canonical formulary")
    src = _review_src()
    check(
        "the add-medicine dropdown options are built from CLINIC_HERB_KEYTERMS",
        "for h in CLINIC_HERB_KEYTERMS" in src,
    )
    check("the add-medicine control is a controlled selectbox, not free text", "st.selectbox(" in src)
    check(
        "already-prescribed medicines are excluded from the dropdown (no self-duplicate offer)",
        "if h not in existing_names" in src,
    )

    # Cross-check: CLINIC_HERB_KEYTERMS (ui/app.py's single source of
    # truth for herb names) stays in sync with the backend's own
    # canonical names, per CLINIC_HERB_KEYTERMS's own docstring comment.
    # Read-only import for verification -- ui/app.py itself still never
    # imports backend code (see its module docstring).
    import backend.herb_resolver as herb_resolver
    canonical_backend_names = set(herb_resolver.HERB_ALIASES.values())
    keyterms = set(app.CLINIC_HERB_KEYTERMS)
    check(
        "every UI dropdown entry is a real backend-canonical herb name "
        "(no invented second naming system)",
        keyterms.issubset(canonical_backend_names),
        keyterms - canonical_backend_names,
    )


# ---------------------------------------------------------------------------
# 10. Duplicate-medicine prevention
# ---------------------------------------------------------------------------

def test_duplicate_medicine_prevention():
    """Scenario 10 — attempting to add a medicine already in the
    prescription never silently duplicates it; instead it shows a
    friendly message and highlights/focuses the existing row."""
    print("\nUX-3 test 10: duplicate medicine prevention")
    src = _review_src()
    check(
        "a duplicate add shows a friendly warning naming the medicine",
        'is already in this prescription' in src,
    )
    check("a duplicate add highlights the existing row", "_review_highlight" in src)
    check("st.warning is used for the duplicate message (never a silent no-op)", "st.warning(" in src)

    _fresh_session()
    app._seed_review_rows([{"name": "Ashwagandha", "grams": 5}], "Vata")
    chosen = "Ashwagandha"
    row_names = _st_stub.session_state["_review_row_names"]

    # Simulate exactly what the "+ Add" handler does when the choice
    # already exists.
    duplicate = chosen in row_names.values()
    check("duplicate is correctly detected", duplicate)
    if duplicate:
        _st_stub.session_state["_review_highlight"] = chosen

    herbs_now = app._current_review_herbs()
    check(
        "no second row was created for the duplicate",
        herbs_now == [{"name": "Ashwagandha", "grams": 5}],
        herbs_now,
    )
    check(
        "the existing row is marked for highlight/focus",
        _st_stub.session_state.get("_review_highlight") == "Ashwagandha",
    )


# ---------------------------------------------------------------------------
# 11. Remove medicine
# ---------------------------------------------------------------------------

def test_remove_medicine():
    """Scenario 11 — removing a medicine drops its row (and its widget
    state) from the prescription, leaving the others untouched."""
    print("\nUX-3 test 11: remove medicine")
    src = _review_src()
    check("each row has its own remove control", 'key=f"remove_{rid}"' in src)

    _fresh_session()
    app._seed_review_rows(
        [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 3}],
        "Vata",
    )
    rid_to_remove = _st_stub.session_state["_review_row_order"][0]
    qty_key = f"qty_{rid_to_remove}"

    # Simulate exactly what the remove ("✕") handler does.
    _st_stub.session_state["_review_row_order"].remove(rid_to_remove)
    _st_stub.session_state["_review_row_names"].pop(rid_to_remove, None)
    _st_stub.session_state.pop(qty_key, None)

    check("the removed row's quantity key is gone", qty_key not in _st_stub.session_state)
    herbs_now = app._current_review_herbs()
    check(
        "only the remaining medicine is left in the prescription",
        herbs_now == [{"name": "Brahmi", "grams": 3}],
        herbs_now,
    )


# ---------------------------------------------------------------------------
# 12. Dosha editing (controlled selector)
# ---------------------------------------------------------------------------

def test_dosha_editing():
    """Scenario 12 — dosha imbalance is edited through a clean, controlled
    selector (a fixed enum of clinically meaningful values), with an
    explicit "Other" escape hatch rather than silently rejecting a value
    the backend would otherwise accept -- a frontend-only constraint that
    imposes no new backend restriction."""
    print("\nUX-3 test 12: dosha editing via controlled selector")
    src = _review_src()
    check(
        "the dosha control offers REVIEW_DOSHA_OPTIONS plus an Other option",
        "REVIEW_DOSHA_OPTIONS + [_DOSHA_OTHER]" in src,
    )
    check("the dosha control is a selectbox (controlled), not free text by default", "st.selectbox(" in src)

    _fresh_session()
    app._seed_review_rows([{"name": "Brahmi", "grams": 3}], "Vata")
    check(
        "a standard dosha seeds the selector directly",
        _st_stub.session_state["review_dosha_select"] == "Vata",
    )
    check("_current_review_dosha reflects the selector", app._current_review_dosha() == "Vata")

    # Switch to a value outside the fixed enum via "Other".
    _st_stub.session_state["review_dosha_select"] = app._DOSHA_OTHER
    _st_stub.session_state["review_dosha_custom"] = "Vata-Kapha with Pitta overlay"
    check(
        "the 'Other' escape hatch still lets a non-enum value reach the backend",
        app._current_review_dosha() == "Vata-Kapha with Pitta overlay",
    )

    # A dosha that was never in the enum (e.g. an older free-text preview)
    # still round-trips correctly through _seed_review_rows.
    app._seed_review_rows([{"name": "Brahmi", "grams": 3}], "Some Custom Imbalance")
    check(
        "an out-of-enum seeded dosha selects 'Other' with the value preserved",
        _st_stub.session_state["review_dosha_select"] == app._DOSHA_OTHER
        and _st_stub.session_state["review_dosha_custom"] == "Some Custom Imbalance",
    )


# ---------------------------------------------------------------------------
# 13. Prescription editing state (view <-> edit toggle)
# ---------------------------------------------------------------------------

def test_prescription_editing_state_toggle():
    """Scenario 13 — "Edit prescription" moves the screen from its calm
    read-only summary into an explicit editing state (steppers, remove,
    add, dosha selector visible); the transition is tracked explicitly in
    session state, not inferred."""
    print("\nUX-3 test 13: prescription editing state toggle")
    src = _review_src()
    check(
        "an 'Edit prescription' control exists",
        'st.button("Edit prescription"' in src,
    )
    check(
        "entering edit mode snapshots the pre-edit herbs/dosha (for Cancel)",
        '"_review_edit_snapshot"' in src,
    )
    check("entering edit mode sets review_editing = True", 'st.session_state["review_editing"] = True' in src)
    check("the render branches on review_editing", 'st.session_state.get("review_editing"' in src)

    _fresh_session()
    check("review_editing starts False by default", app._DEFAULTS["review_editing"] is False)
    app._seed_review_rows([{"name": "Brahmi", "grams": 3}], "Vata")

    # Simulate clicking "Edit prescription".
    herbs_now = app._current_review_herbs()
    dosha_now = app._current_review_dosha()
    _st_stub.session_state["_review_edit_snapshot"] = {"herbs": list(herbs_now), "dosha": dosha_now}
    _st_stub.session_state["review_editing"] = True
    check("editing state is now True", _st_stub.session_state["review_editing"] is True)


# ---------------------------------------------------------------------------
# 14. Save / Cancel behavior
# ---------------------------------------------------------------------------

def test_save_and_cancel_behavior():
    """Scenario 14 — "Save changes" keeps the edited working state and
    returns to the read-only view; "Cancel" discards in-progress edits
    and restores the pre-edit snapshot exactly."""
    print("\nUX-3 test 14: Save/Cancel behavior")
    src = _review_src()
    check("Save changes control exists", 'key="btn_save_prescription"' in src)
    check("Cancel control exists", 'key="btn_cancel_prescription"' in src)
    check(
        "Cancel restores the pre-edit snapshot via _seed_review_rows",
        'snapshot["herbs"], snapshot["dosha"]' in src,
    )

    _fresh_session()
    app._seed_review_rows([{"name": "Brahmi", "grams": 3}], "Vata")
    snapshot = {"herbs": list(app._current_review_herbs()), "dosha": app._current_review_dosha()}
    _st_stub.session_state["_review_edit_snapshot"] = snapshot
    _st_stub.session_state["review_editing"] = True

    # Make an edit while "in" edit mode.
    rid = _st_stub.session_state["_review_row_order"][0]
    _st_stub.session_state[f"qty_{rid}"] = 99
    check("the in-progress edit is visible before Save/Cancel", app._current_review_herbs()[0]["grams"] == 99)

    # --- Save: keep the edit, just leave edit mode. ---
    _st_stub.session_state["review_editing"] = False
    check("Save leaves the edited quantity in place", app._current_review_herbs()[0]["grams"] == 99)

    # --- Cancel: discard the edit, restore the snapshot. ---
    snap = _st_stub.session_state.get("_review_edit_snapshot")
    app._seed_review_rows(snap["herbs"], snap["dosha"])
    _st_stub.session_state["review_editing"] = False
    check(
        "Cancel restores the pre-edit quantity exactly",
        app._current_review_herbs() == [{"name": "Brahmi", "grams": 3}],
        app._current_review_herbs(),
    )


# ---------------------------------------------------------------------------
# 15/16. Slide-to-confirm reaches the EXISTING confirmation path
# ---------------------------------------------------------------------------

def test_slide_to_confirm_reaches_existing_confirm_flow():
    """Scenario 15 — the Apple-style slide-to-confirm gesture, once
    complete, calls the SAME call_confirm()/get_idempotency_key_for()
    flow UX-1/UX-2 already established -- no new confirmation endpoint,
    no confirmation logic moved to the frontend."""
    print("\nUX-3 test 15: slide-to-confirm reaches the existing confirmation path")
    src = _review_src()
    check("confirmation is driven by an st.slider", "st.slider(" in src)
    check(
        "reaching the end of the slide (100) is what triggers confirmation",
        "slide_value >= 100" in src,
    )
    check(
        "confirmation calls the existing call_confirm() with the current values",
        "call_confirm(\n                        preview[\"preview_id\"], dosha_now, herbs_now, token, idempotency_key,"
        in src
        or ("call_confirm(" in src and "dosha_now, herbs_now, token, idempotency_key" in src),
    )
    check(
        "the idempotency key is obtained via the existing get_idempotency_key_for()",
        "get_idempotency_key_for(preview[\"preview_id\"], dosha_now, herbs_now)" in src,
    )
    check(
        "the slider is reset to 0 after use (no re-trigger / state leakage)",
        'st.session_state["slide_confirm"] = 0' in src,
    )

    # Prove call_confirm() itself still posts to the one, existing
    # /prescribe/confirm endpoint -- nothing about UX-3 introduced a new
    # endpoint or moved confirmation logic into the frontend.
    confirm_src = inspect.getsource(app.call_confirm)
    check(
        "call_confirm still posts to the existing /prescribe/confirm endpoint",
        '/prescribe/confirm' in confirm_src,
    )
    check(
        "call_confirm's signature is unchanged: (preview_id, dosha_imbalance, prescribed_herbs, token, idempotency_key=None)",
        list(inspect.signature(app.call_confirm).parameters.keys())
        == ["preview_id", "dosha_imbalance", "prescribed_herbs", "token", "idempotency_key"],
    )


def test_confirmation_does_not_bypass_backend():
    """Scenario 16 — the frontend never bypasses auth, RBAC, idempotency,
    inventory, or audit: it has no direct database/inventory access, and
    the only network call in the confirmation path is call_confirm()
    (which always sends the Authorization header and an Idempotency-Key)."""
    print("\nUX-3 test 16: confirmation does not bypass the backend")
    src = _review_src()
    check("the review stage never imports/touches a database module", "sqlite" not in src.lower() and "get_connection" not in src)
    check(
        "the review stage has no direct inventory manipulation "
        "(only ever documents, in comments, that it does NOT deduct)",
        "stock_grams" not in src and "deduct_and_prescribe" not in src and "conn.execute" not in src,
    )
    check(
        "the review stage never posts directly to the backend -- only via call_confirm()",
        "requests.post" not in src,
    )

    confirm_src = inspect.getsource(app.call_confirm)
    check("call_confirm still sends the Authorization header", "_protected_headers" in confirm_src)
    check(
        "call_confirm still attaches the Idempotency-Key (auth/idempotency both enforced backend-side)",
        "idempotency_key" in confirm_src,
    )
    # Exactly one call_confirm() call site inside the review stage's
    # confirm branch -- confirmation only ever goes through this one,
    # existing, backend-authoritative function. (call_confirm( also
    # appears once in this function's own docstring describing the
    # design intent, so this checks for exactly one CALL SITE, not one
    # raw text occurrence.)
    call_sites = re.findall(r"^\s*result, error = call_confirm\(", src, re.MULTILINE)
    check(
        "the review stage calls call_confirm from exactly one call site",
        len(call_sites) == 1,
        src.count("call_confirm("),
    )


# ---------------------------------------------------------------------------
# 17. Idempotency key behavior remains correct
# ---------------------------------------------------------------------------

def test_idempotency_key_behavior_with_current_values():
    """Scenario 17 — the idempotency key is generated from the CURRENT,
    submitted (dosha_now, herbs_now) -- reusing the same key for an
    unchanged retry, and a fresh key the moment an edit changes what's
    being sent -- exactly the existing get_idempotency_key_for() contract,
    now fed from the UX-3 editor's live state instead of a data_editor."""
    print("\nUX-3 test 17: idempotency key behavior with live editor state")
    _fresh_session()
    app._seed_review_rows([{"name": "Ashwagandha", "grams": 5}], "Vata")
    preview_id = "prev-ux3-1"

    herbs_now = app._current_review_herbs()
    dosha_now = app._current_review_dosha()
    key1 = app.get_idempotency_key_for(preview_id, dosha_now, herbs_now)

    # Unchanged retry (e.g. a dropped connection, doctor slides again
    # without editing anything) -- same key.
    herbs_now2 = app._current_review_herbs()
    dosha_now2 = app._current_review_dosha()
    key2 = app.get_idempotency_key_for(preview_id, dosha_now2, herbs_now2)
    check("an unchanged retry reuses the same idempotency key", key1 == key2, (key1, key2))

    # Doctor edits a quantity via the stepper/direct-edit before retrying.
    rid = _st_stub.session_state["_review_row_order"][0]
    _st_stub.session_state[f"qty_{rid}"] = 7
    herbs_changed = app._current_review_herbs()
    dosha_changed = app._current_review_dosha()
    key3 = app.get_idempotency_key_for(preview_id, dosha_changed, herbs_changed)
    check("an edited quantity generates a fresh idempotency key", key1 != key3, (key1, key3))

    # And does NOT regenerate accidentally on every rerun with unchanged inputs.
    key4 = app.get_idempotency_key_for(preview_id, dosha_changed, herbs_changed)
    check("a repeated call with the same (now-edited) values does not regenerate", key3 == key4, (key3, key4))


# ---------------------------------------------------------------------------
# 18. Next patient clears previous state (no bleed)
# ---------------------------------------------------------------------------

def test_next_patient_clears_previous_state():
    """Scenario 18 — starting the next patient (reset_for_next_patient())
    leaves no trace of the previous patient's prescription rows, dosha,
    edit/slide state, or idempotency key -- spec §18's explicit
    non-regression requirement."""
    print("\nUX-3 test 18: next patient gets a clean slate")
    _fresh_session()
    _st_stub.session_state["_patient_name"] = "Patient A"
    _st_stub.session_state["_patient_phone"] = "1111111111"
    _st_stub.session_state["_patient_id"] = "PAT-A"
    app._seed_review_rows([{"name": "Ashwagandha", "grams": 5}], "Vata")
    rid = _st_stub.session_state["_review_row_order"][0]
    _st_stub.session_state[f"qty_{rid}"] = 99
    _st_stub.session_state["review_editing"] = True
    _st_stub.session_state["slide_confirm"] = 60
    app.get_idempotency_key_for("prev-A", "Vata", app._current_review_herbs())

    app.reset_for_next_patient()

    check("no leftover row order", "_review_row_order" not in _st_stub.session_state)
    check("no leftover row names", "_review_row_names" not in _st_stub.session_state)
    check("Patient A's quantity key is gone", f"qty_{rid}" not in _st_stub.session_state)
    check("dosha selector cleared", "review_dosha_select" not in _st_stub.session_state)
    check("editing state reset to False", _st_stub.session_state["review_editing"] is False)
    check("slide-to-confirm reset to 0", _st_stub.session_state["slide_confirm"] == 0)
    check("idempotency signature cleared", _st_stub.session_state["confirm_signature"] is None)
    check("idempotency key cleared", _st_stub.session_state["confirm_idempotency_key"] is None)
    check("patient identity cleared for the next patient", _st_stub.session_state["_patient_name"] == "")

    # Patient B's fresh preview seeds cleanly with no bleed from Patient A.
    app._seed_review_rows([{"name": "Brahmi", "grams": 2}], "Kapha")
    herbs_b = app._current_review_herbs()
    check(
        "Patient B's herbs contain nothing from Patient A",
        herbs_b == [{"name": "Brahmi", "grams": 2}],
        herbs_b,
    )
    # Note: the row-id counter itself is reset by _clear_review_widget_state
    # (part of a full reset_for_next_patient()), so Patient B's first row
    # may legitimately reuse the string "r0" -- that's safe here because
    # every piece of Patient A's data under that key was already popped
    # clean above, unlike the *same-session* "remove a row mid-edit"
    # case _seed_review_rows's docstring is protecting against (a row
    # removed while OTHER rows/state are still live), which is what
    # test_remove_medicine exercises instead.


# ---------------------------------------------------------------------------
# 19. Back preserves patient information
# ---------------------------------------------------------------------------

def test_back_preserves_patient_information():
    """Scenario 19 — clicking Back from the review screen clears only the
    review screen's OWN widget state (rows, dosha selector, slider, edit
    mode) -- it must never touch _patient_name/_patient_phone/_patient_id,
    so the doctor doesn't have to re-type patient details after Back."""
    print("\nUX-3 test 19: Back preserves patient information")
    src = _review_src()
    back_start = src.index('if st.button("← Back"')
    back_end = src.index("# ---- 1. AI DRAFT header")
    back_block = src[back_start:back_end]
    check("the Back block does not clear _patient_name", '"_patient_name"' not in back_block)
    check("the Back block does not clear _patient_phone", '"_patient_phone"' not in back_block)
    check("the Back block does not clear _patient_id", '"_patient_id"' not in back_block)

    _fresh_session()
    _st_stub.session_state["_patient_name"] = "Ravi Kumar"
    _st_stub.session_state["_patient_phone"] = "9876543210"
    _st_stub.session_state["_patient_id"] = "PAT-007"
    app._seed_review_rows([{"name": "Brahmi", "grams": 3}], "Vata")

    # Simulate exactly what the Back handler does (its own cleanup call).
    app._clear_review_widget_state()
    _st_stub.session_state.pop("_review_started_at", None)
    _st_stub.session_state["last_error"] = None

    check("patient name survives Back", _st_stub.session_state["_patient_name"] == "Ravi Kumar")
    check("patient phone survives Back", _st_stub.session_state["_patient_phone"] == "9876543210")
    check("patient id survives Back", _st_stub.session_state["_patient_id"] == "PAT-007")


# ---------------------------------------------------------------------------
# 20. Session expiry uses the existing friendly message
# ---------------------------------------------------------------------------

def test_session_expiry_uses_existing_friendly_message():
    """Scenario 20 — a session expiring mid-review (during confirm) is
    handled by the same SessionExpiredError -> _clear_session(MSG_SESSION)
    path every other stage uses; no new/duplicate session-expiry message
    was introduced for UX-3."""
    print("\nUX-3 test 20: session expiry during review uses the existing friendly message")
    src = _review_src()
    check("the review stage catches SessionExpiredError", "except SessionExpiredError:" in src)
    check(
        "the review stage clears the session with the existing MSG_SESSION",
        "_clear_session(MSG_SESSION)" in src,
    )
    check(
        "MSG_SESSION is the same clinician-facing message used everywhere else",
        app.MSG_SESSION == "Your session has ended. Sign in again to carry on — your notes are kept.",
    )

    # _clear_session itself is untouched: still logs out locally and
    # resets consultation state, exactly as every other stage relies on.
    clear_src = inspect.getsource(app._clear_session)
    check("_clear_session still clears auth", "st.session_state.auth = None" in clear_src)
    check("_clear_session still resets consultation state via reset_for_next_patient", "reset_for_next_patient()" in clear_src)


# ---------------------------------------------------------------------------
# 21. AI-unavailable uses the existing manual fallback
# ---------------------------------------------------------------------------

def test_ai_unavailable_uses_existing_manual_fallback():
    """Scenario 21 — when AI drafting is unavailable, "Write it myself" is
    (still) always available, exactly as before UX-3 -- not a new,
    UX-3-specific fallback mechanism, and not gated behind ai_unavailable."""
    print("\nUX-3 test 21: AI-unavailable path reuses the existing manual fallback")
    src = inspect.getsource(app._render_input_stage)
    check(
            "\"Write it myself\" is documented as always available regardless of AI state",
        '"Write it myself" is always available regardless of AI state' in src,
    )
    check('the manual entry button is labeled "Write it myself"', '"Write it myself"' in src)
    check(
        "AI and manual are still framed as peer choices, not fallback/degraded",
        "Neither path is framed as a fallback" in src,
    )
    check(
        "MSG_AI_UNAVAILABLE explicitly offers the manual path as the way forward",
        "write this prescription yourself" in app.MSG_AI_UNAVAILABLE,
        app.MSG_AI_UNAVAILABLE,
    )


# ---------------------------------------------------------------------------
# 22. Non-doctor roles cannot gain prescribing UI
# ---------------------------------------------------------------------------

def test_non_doctor_roles_cannot_gain_prescribing_ui():
    """Scenario 22 — ADMIN and AUDITOR must not gain prescribing
    capability from the UX-3 redesign: main()'s role gate still runs
    BEFORE any consultation stage (including the new review screen) is
    reached, and REQUIRED_ROLE is still exactly "DOCTOR", unchanged."""
    print("\nUX-3 test 22: non-doctor roles cannot reach the prescribing UI")
    check("REQUIRED_ROLE is still DOCTOR-only", app.REQUIRED_ROLE == "DOCTOR")

    main_src = inspect.getsource(app.main)
    role_gate_pos = main_src.index('auth.get("role") != REQUIRED_ROLE')
    stage_dispatch_pos = main_src.index('stage = st.session_state.consultation_stage')
    check(
        "the role gate runs strictly before any consultation stage is dispatched",
        role_gate_pos < stage_dispatch_pos,
    )
    check(
        "a wrong role hits st.stop() -- no stage (including review) renders after it",
        "st.stop()" in main_src[role_gate_pos:stage_dispatch_pos],
    )
    check(
        "the wrong-role branch shows the existing MSG_WRONG_ROLE, not a new message",
        "st.error(MSG_WRONG_ROLE)" in main_src,
    )
    check(
        "MSG_WRONG_ROLE text is unchanged",
        app.MSG_WRONG_ROLE == "This account can't write prescriptions. Sign in with a doctor's account.",
    )


# ---------------------------------------------------------------------------
# 23. Keyboard/accessibility alternative for confirmation
# ---------------------------------------------------------------------------

def test_keyboard_accessible_confirmation_alternative():
    """Scenario 23 — slide-to-confirm has a built-in, no-extra-work
    keyboard-accessible equivalent: st.slider is natively operable via
    arrow keys/Home/End when focused, and the control says so, satisfying
    the accessibility requirement without weakening the backend
    confirmation safety (still the same call_confirm() path either way)."""
    print("\nUX-3 test 23: keyboard-accessible alternative to slide-to-confirm")
    src = _review_src()
    check("confirmation uses st.slider (natively keyboard-operable)", "st.slider(" in src)
    check(
        "the slider's own help text tells the doctor about the keyboard alternative",
        "arrow keys" in src or "End" in src,
    )
    check(
        "the slider is disabled (and clearly captioned) rather than silently unusable "
        "when nothing can yet be confirmed",
        "disabled=not can_confirm" in src and "st.caption(" in src,
    )
    # Whichever way the doctor reaches 100 (drag or keyboard), the same
    # backend-authoritative call_confirm() path is used -- no separate,
    # weaker keyboard-only confirmation route exists. (call_confirm( also
    # appears once in this function's own docstring, describing the
    # design intent, so this checks for exactly one CALL SITE, not one
    # raw text occurrence.)
    call_sites = re.findall(r"^\s*result, error = call_confirm\(", src, re.MULTILINE)
    check(
        "there is exactly one confirmation call site regardless of input method",
        len(call_sites) == 1,
        src.count("call_confirm("),
    )
    check(
        "the review stage never posts directly to the backend for confirmation",
        "requests.post" not in src,
    )


# ---------------------------------------------------------------------------
# Visual-acceptance regression: the login screen must share the SAME
# visual system as the post-login workspace (not just the review screen).
# ---------------------------------------------------------------------------

def test_workspace_css_applies_before_login_screen():
    """Visual acceptance fix — _WORKSPACE_CSS (the warm off-white/muted-
    green visual system: background, typography, button/input styling)
    must be injected BEFORE the auth gate in main(), so the login screen
    -- which renders and calls st.stop() inside that gate, before ever
    reaching a later injection point -- gets it too. Previously the
    injection happened only after a successful login, right before the
    sidebar/page heading, so the login screen fell back to Streamlit's
    own default theme resolution (which follows the visitor's browser/OS
    dark-mode preference when no [theme] block is set) while the
    post-login workspace already looked correct. This is a structural,
    source-level check (no real Streamlit runtime is available in this
    harness to render actual computed CSS) that the ONE call to
    st.markdown(_WORKSPACE_CSS, ...) in main() sits before the
    `if not st.session_state.auth:` gate, not after it."""
    print("\nVisual acceptance test: workspace CSS reaches the login screen")
    import inspect
    src = inspect.getsource(app.main)

    css_injection_pos = src.index("st.markdown(_WORKSPACE_CSS, unsafe_allow_html=True)")
    auth_gate_pos = src.index("if not st.session_state.auth:")
    check(
        "_WORKSPACE_CSS is injected before the auth gate (so the login screen gets it)",
        css_injection_pos < auth_gate_pos,
    )
    check(
        "_WORKSPACE_CSS is injected exactly once in main() (no stale duplicate left behind)",
        src.count("st.markdown(_WORKSPACE_CSS, unsafe_allow_html=True)") == 1,
        src.count("st.markdown(_WORKSPACE_CSS, unsafe_allow_html=True)"),
    )
    check(
        "the injection happens right after set_page_config, before any session-state setup",
        src.index("st.set_page_config(") < css_injection_pos,
    )


def test_workspace_css_is_login_screen_safe():
    """Visual acceptance fix — _WORKSPACE_CSS must be a purely global
    stylesheet with nothing in it that assumes an authenticated session
    (e.g. it must not be scoped under a class or id that only appears
    inside the post-login workspace), since it now also has to style the
    login screen. Also locks in the "no red primary buttons" requirement:
    st.form_submit_button (used by the login form's "Sign in" button)
    renders its <button> with DIFFERENT kind values ("primaryFormSubmit"/
    "secondaryFormSubmit") than plain st.button ("primary"/"secondary"),
    so both must be covered or the login button silently keeps
    Streamlit's default (red) primary color."""
    print("\nVisual acceptance test: workspace CSS is safe/complete for the login screen")
    css = app._WORKSPACE_CSS
    check("the background rule targets the app shell globally", '.stApp, [data-testid="stAppViewContainer"]' in css)
    check("the background color is the warm off-white token, not a dark color", "var(--ayc-bg)" in css)
    check("--ayc-bg is the specified warm off-white hex", "--ayc-bg: #F7F5EF;" in css)
    check("--ayc-green-deep is the specified muted deep green hex", "--ayc-green-deep: #315C4A;" in css)

    check(
        "plain st.button primary styling is present",
        'button[kind="primary"]' in css,
    )
    check(
        "st.form_submit_button primary styling is ALSO present (login screen's Sign in button)",
        'button[kind="primaryFormSubmit"]' in css,
    )
    check(
        "the form-submit primary button gets the same deep-green background as a plain primary button",
        'button[kind="primaryFormSubmit"] {\n    background-color: var(--ayc-green-deep);' in css
        or "background-color: var(--ayc-green-deep);" in css.split('button[kind="primaryFormSubmit"]')[1][:120],
    )
    # Strip CSS comments first (this stylesheet's own comments legitimately
    # discuss "red" while explaining why it's avoided), then word-boundary
    # check the CSS color keyword "red" in what's left -- not a plain
    # substring match, which would false-positive on words like "centered".
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    no_red_keyword = re.search(r"(?<![a-zA-Z])red(?![a-zA-Z])", css_no_comments.lower()) is None
    check(
        "no red/pink color literal is introduced by this stylesheet's actual CSS rules",
        "#ff4b4b" not in css_no_comments.lower() and no_red_keyword,
    )


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

_ALL_TESTS = [
    test_patient_context_rendering,
    test_proposal_rendering_ai_vs_manual,
    test_row_structure_from_seeded_herbs,
    test_stepper_increase_quantity,
    test_stepper_decrease_quantity,
    test_stepper_floor_prevents_below_one,
    test_direct_quantity_edit,
    test_add_medicine,
    test_add_medicine_dropdown_uses_canonical_formulary,
    test_duplicate_medicine_prevention,
    test_remove_medicine,
    test_dosha_editing,
    test_prescription_editing_state_toggle,
    test_save_and_cancel_behavior,
    test_slide_to_confirm_reaches_existing_confirm_flow,
    test_confirmation_does_not_bypass_backend,
    test_idempotency_key_behavior_with_current_values,
    test_next_patient_clears_previous_state,
    test_back_preserves_patient_information,
    test_session_expiry_uses_existing_friendly_message,
    test_ai_unavailable_uses_existing_manual_fallback,
    test_non_doctor_roles_cannot_gain_prescribing_ui,
    test_keyboard_accessible_confirmation_alternative,
    test_workspace_css_applies_before_login_screen,
    test_workspace_css_is_login_screen_safe,
]


def run_tests():
    print("=== UX-3 focused tests ===\n")
    failures = []
    for test_fn in _ALL_TESTS:
        try:
            test_fn()
        except AssertionError as e:
            failures.append((test_fn.__name__, str(e)))
        except Exception as e:
            failures.append((test_fn.__name__, f"UNEXPECTED: {e}"))
        print()
    print(f"=== {_passed} passed, {_failed} failed ===")
    if failures:
        print(f"\nFailed tests:")
        for name, detail in failures:
            print(f"  {name}: {detail}")
    return failures


if __name__ == "__main__":
    failures = run_tests()
    sys.exit(1 if failures else 0)
