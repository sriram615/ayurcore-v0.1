"""
Migration 017 -- guardrail shadow recording tables (Phase 3A.4, Block 2).

Coverage, each with foreign keys ON and OFF (the triggers must hold either way):
  - schema: both tables, their exact columns, indexes and triggers; no column
    for a candidate fingerprint, patient identity, dosha or herb text;
  - activations: valid SHADOW and OFF rows; mode, rule-set, operator and
    reason constraints; append-only; no row means OFF; the highest
    activation_id is the one in effect;
  - results: every valid status shape (EVALUATED, NOT_EVALUABLE,
    ENGINE_ERROR x3, RULESET_MISMATCH), manual drafts, and each invalid
    shape the CHECK constraints refuse (status, error code, outcome, hashes,
    JSON, stage / prescription_id);
  - clinic isolation: activation, draft and prescription must all be in the
    row's clinic; the activation must be a SHADOW row;
  - strict confirmation pairing: a CONFIRM row needs the
    PRESCRIPTION_CONFIRMED audit event naming exactly its
    (prescription_id, preview_id) pair;
  - one row per draft per stage; results are append-only;
  - the activation race (documented, not prevented here): the database
    accepts a row recorded under a SHADOW activation that is no longer the
    clinic's latest -- enforcing "latest" is the recorder's job;
  - migration 017 changes no existing row; SQLite JSON functions are
    available in the supported runtime.
  - review corrections: EVALUATED / RULESET_MISMATCH must agree with the
    activation's fingerprint (F1); lower-case hex fingerprints, a JSON-array
    results_json and non-blank required strings (F4); the pre-deployment
    SQLite compatibility check (F2).

Every database here is a temporary file migrated from scratch. The project
database is never opened.

Run directly (no pytest required):
    python tests/test_migration_017_guardrail_shadow.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_DIR = os.path.join(ROOT, "migrations")

HASH = "a" * 64
OTHER_HASH = "b" * 64
NOW = "2026-10-02 09:00:00"

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail=""):
    """Raises AssertionError immediately on a failed assertion -- same
    convention as every other test file in this project."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail != "" else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _migrated_db(up_to: int | None = None) -> tuple[sqlite3.Connection, str, str]:
    """A temp database migrated from scratch, optionally only up to `up_to`."""
    from db.migrations import run_migrations
    mig_dir = tempfile.mkdtemp()
    for name in os.listdir(MIGRATIONS_DIR):
        if name[:3].isdigit() and (up_to is None or int(name[:3]) <= up_to):
            shutil.copy(os.path.join(MIGRATIONS_DIR, name), mig_dir)
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn=conn, migrations_dir=mig_dir)
    return conn, path, mig_dir


def _cleanup(conn, path, mig_dir):
    conn.close()
    os.remove(path)
    shutil.rmtree(mig_dir)


def _raises(conn, sql, args, expect: str) -> bool:
    try:
        conn.execute(sql, args)
    except sqlite3.DatabaseError as e:
        return expect in str(e)
    return False


def _activate(conn, clinic, mode="SHADOW", ruleset_hash=HASH, activated_by="operator", reason="trial") -> int:
    shadow = mode == "SHADOW"
    return conn.execute(
        "INSERT INTO guardrail_shadow_activations (clinic_id, mode, ruleset_id, ruleset_version, ruleset_hash, "
        "activated_by, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (clinic, mode, "ENGINEERING-BASELINE" if shadow else None, "1" if shadow else None,
         ruleset_hash if shadow else None, activated_by, reason, NOW)).lastrowid


def _preview(conn, preview_id, clinic, is_manual=0):
    conn.execute(
        "INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
        "ai_prescribed_herbs_json, status, created_at, expires_at) VALUES (?, ?, 'P', ?, 'Vata', '[]', 'PENDING', ?, ?)",
        (preview_id, clinic, is_manual, NOW, NOW))


def _prescription(conn, clinic) -> int:
    return conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) "
                        "VALUES (?, 'P', 'd', 'Active')", (clinic,)).lastrowid


def _user(conn, clinic) -> int:
    return conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, created_at, updated_at) "
                        "VALUES (?, 'x', ?, 'DOCTOR', ?, ?)", (f"u_{clinic}", clinic, NOW, NOW)).lastrowid


def _confirmed(conn, clinic, prescription_id, preview_id, user_id):
    conn.execute(
        "INSERT INTO audit_events (clinic_id, actor_user_id, event_type, prescription_id, preview_id, "
        "event_data_json, created_at, previous_event_hash, event_hash) "
        "VALUES (?, ?, 'PRESCRIPTION_CONFIRMED', ?, ?, '{}', ?, NULL, ?)",
        (clinic, user_id, prescription_id, preview_id, NOW, f"h{prescription_id}"))


_RESULT_COLUMNS = ("clinic_id", "activation_id", "stage", "preview_id", "prescription_id", "is_manual", "status",
                   "error_code", "engine_version", "engine_fingerprint", "code_ruleset_id", "code_ruleset_version",
                   "code_ruleset_hash", "outcome", "results_json", "created_at")
_INSERT_RESULT = (f"INSERT INTO guardrail_shadow_results ({', '.join(_RESULT_COLUMNS)}) "
                  f"VALUES ({', '.join('?' for _ in _RESULT_COLUMNS)})")


def _result(clinic, activation_id, preview_id, stage="PREVIEW", prescription_id=None, is_manual=0,
            status="EVALUATED", **overrides) -> tuple:
    """A row of the given status's valid shape; overrides make it invalid."""
    row = {"clinic_id": clinic, "activation_id": activation_id, "stage": stage, "preview_id": preview_id,
           "prescription_id": prescription_id, "is_manual": is_manual, "status": status, "error_code": None,
           "engine_version": None, "engine_fingerprint": None, "code_ruleset_id": None,
           "code_ruleset_version": None, "code_ruleset_hash": None, "outcome": None, "results_json": None,
           "created_at": NOW}
    if status == "EVALUATED":
        row.update(engine_version="3A.3", engine_fingerprint=HASH, code_ruleset_id="ENGINEERING-BASELINE",
                   code_ruleset_version="1", code_ruleset_hash=HASH, outcome="PASS",
                   results_json='[{"rule_id": "ENG-QUANTITY", "outcome": "PASS"}]')
    elif status == "NOT_EVALUABLE":
        row.update(error_code="CANDIDATE_INVALID")
    elif status == "ENGINE_ERROR":
        row.update(error_code="UNEXPECTED")
    elif status == "RULESET_MISMATCH":
        row.update(code_ruleset_id="ENGINEERING-BASELINE", code_ruleset_version="1", code_ruleset_hash=OTHER_HASH)
    row.update(overrides)
    return tuple(row[c] for c in _RESULT_COLUMNS)


def _each_foreign_key_mode(body):
    """Runs `body(conn, tag)` on a fresh, fully migrated database with
    foreign keys ON, then again with them OFF."""
    for fk in ("ON", "OFF"):
        conn, path, mig_dir = _migrated_db()
        try:
            conn.execute(f"PRAGMA foreign_keys = {fk}")
            body(conn, f"fk {fk}")
        finally:
            _cleanup(conn, path, mig_dir)


# --- schema ---------------------------------------------------------------------------

def test_schema_tables_columns_indexes_and_triggers():
    conn, path, mig_dir = _migrated_db()
    try:
        activation_cols = [r["name"] for r in conn.execute("PRAGMA table_info(guardrail_shadow_activations)")]
        check("activation columns", activation_cols == [
            "activation_id", "clinic_id", "mode", "ruleset_id", "ruleset_version", "ruleset_hash",
            "activated_by", "reason", "created_at"], activation_cols)
        result_cols = [r["name"] for r in conn.execute("PRAGMA table_info(guardrail_shadow_results)")]
        check("result columns", result_cols == ["result_id", *_RESULT_COLUMNS], result_cols)
        stored = " ".join(activation_cols + result_cols)
        check("no candidate fingerprint, patient identity, dosha or herb column",
              not any(word in stored for word in ("candidate", "patient", "dosha", "herb")), stored)
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                               "AND tbl_name LIKE 'guardrail_shadow_%'")}
        check("all seven triggers exist", triggers == {
            "guardrail_shadow_activations_no_update", "guardrail_shadow_activations_no_delete",
            "guardrail_shadow_results_activation_check", "guardrail_shadow_results_preview_check",
            "guardrail_shadow_results_prescription_check", "guardrail_shadow_results_no_update",
            "guardrail_shadow_results_no_delete"}, triggers)
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                              "AND tbl_name LIKE 'guardrail_shadow_%' AND name NOT LIKE 'sqlite_%'")}
        check("indexes exist", indexes == {"idx_shadow_activations_clinic", "idx_shadow_results_clinic_time",
                                           "idx_shadow_results_activation"}, indexes)
        check("UNIQUE (preview_id, stage) exists", any(
            [c["name"] for c in conn.execute(f"PRAGMA index_info({r['name']})")] == ["preview_id", "stage"]
            for r in conn.execute("PRAGMA index_list(guardrail_shadow_results)") if r["unique"]))
        check("017 is recorded as applied", conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 17").fetchone()[0] == 1)
        check("both tables start empty", conn.execute("SELECT COUNT(*) FROM guardrail_shadow_activations").fetchone()[0]
              == conn.execute("SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 0)
    finally:
        _cleanup(conn, path, mig_dir)


def test_sqlite_json_functions_are_available():
    version = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    check(f"SQLite {sqlite3.sqlite_version} >= 3.38 (JSON functions built in)", version >= (3, 38, 0))
    conn = sqlite3.connect(":memory:")
    try:
        check("json_valid works", conn.execute("SELECT json_valid('[1]'), json_valid('{bad')").fetchone() == (1, 0))
    finally:
        conn.close()


# --- activations ------------------------------------------------------------------------

def test_activation_constraints_off_by_default_and_append_only():
    def body(conn, tag):
        latest = ("SELECT mode FROM guardrail_shadow_activations WHERE clinic_id = ? "
                  "ORDER BY activation_id DESC LIMIT 1")
        check(f"[{tag}] a clinic with no activation row has none (OFF by default)",
              conn.execute(latest, ("clinic_a",)).fetchone() is None)
        first = _activate(conn, "clinic_a")
        check(f"[{tag}] a SHADOW row is accepted", conn.execute(latest, ("clinic_a",)).fetchone()["mode"] == "SHADOW")
        second = _activate(conn, "clinic_a", mode="OFF")
        check(f"[{tag}] an OFF row is accepted and, being the latest, is in effect",
              second > first and conn.execute(latest, ("clinic_a",)).fetchone()["mode"] == "OFF")

        ins = ("INSERT INTO guardrail_shadow_activations (clinic_id, mode, ruleset_id, ruleset_version, ruleset_hash, "
               "activated_by, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
        good = {"clinic_id": "clinic_a", "mode": "SHADOW", "ruleset_id": "R", "ruleset_version": "1",
                "ruleset_hash": HASH, "activated_by": "operator", "reason": "trial", "created_at": NOW}
        off = {**good, "mode": "OFF", "ruleset_id": None, "ruleset_version": None, "ruleset_hash": None}
        invalid = [
            ("an ENFORCE mode", {**good, "mode": "ENFORCE"}),
            ("SHADOW without a rule-set hash", {**good, "ruleset_hash": None}),
            ("SHADOW without a rule-set id", {**good, "ruleset_id": None}),
            ("SHADOW without a rule-set version", {**good, "ruleset_version": None}),
            ("a 63-character hash", {**good, "ruleset_hash": "a" * 63}),
            ("OFF carrying a rule set", {**off, "ruleset_id": "R"}),
            ("OFF carrying a hash", {**off, "ruleset_hash": HASH}),
            ("an empty clinic", {**good, "clinic_id": ""}),
            ("a 65-character clinic", {**good, "clinic_id": "c" * 65}),
            ("a blank operator", {**good, "activated_by": "   "}),
            ("a 101-character operator", {**good, "activated_by": "o" * 101}),
            ("a blank reason", {**good, "reason": " "}),
            ("a 501-character reason", {**good, "reason": "r" * 501}),
            ("no operator", {**good, "activated_by": None}),
            ("no timestamp", {**good, "created_at": None}),
        ]
        for label, row in invalid:
            check(f"[{tag}] activation with {label} rejected",
                  _raises(conn, ins, tuple(row.values()), "constraint failed"))
        check(f"[{tag}] activation UPDATE rejected", _raises(
            conn, "UPDATE guardrail_shadow_activations SET mode = 'OFF' WHERE activation_id = ?", (first,),
            "shadow_activation_immutable"))
        check(f"[{tag}] activation DELETE rejected", _raises(
            conn, "DELETE FROM guardrail_shadow_activations WHERE activation_id = ?", (first,),
            "shadow_activation_immutable"))
        check(f"[{tag}] activations unchanged", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_activations").fetchone()[0] == 2)

    _each_foreign_key_mode(body)


# --- results ----------------------------------------------------------------------------

def test_valid_result_shapes_including_manual_drafts():
    def body(conn, tag):
        act = _activate(conn, "clinic_a")
        for n, (status, extra) in enumerate((("EVALUATED", {}), ("NOT_EVALUABLE", {}),
                                             ("ENGINE_ERROR", {"error_code": "ENGINE_CHANGED"}),
                                             ("ENGINE_ERROR", {"error_code": "RULESET_INVALID"}),
                                             ("ENGINE_ERROR", {"error_code": "UNEXPECTED"}),
                                             ("ENGINE_ERROR", {"engine_version": "3A.3", "engine_fingerprint": HASH}),
                                             ("RULESET_MISMATCH", {}))):
            preview_id = f"p{n}"
            _preview(conn, preview_id, "clinic_a")
            conn.execute(_INSERT_RESULT, _result("clinic_a", act, preview_id, status=status, **extra))
        _preview(conn, "manual", "clinic_a", is_manual=1)
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "manual", is_manual=1))
        check(f"[{tag}] every valid status shape and a manual draft are accepted",
              conn.execute("SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 8)
        check(f"[{tag}] the manual draft is recorded as manual", conn.execute(
            "SELECT is_manual FROM guardrail_shadow_results WHERE preview_id = 'manual'").fetchone()[0] == 1)

    _each_foreign_key_mode(body)


def test_invalid_result_shapes_are_rejected():
    def body(conn, tag):
        act = _activate(conn, "clinic_a")
        _preview(conn, "p", "clinic_a")
        invalid = [
            ("an unknown error code", "ENGINE_ERROR", {"error_code": "CRASHED"}),
            ("an unknown outcome", "EVALUATED", {"outcome": "BLOCK"}),
            ("an unknown stage", "EVALUATED", {"stage": "DRAFT"}),
            ("EVALUATED without an outcome", "EVALUATED", {"outcome": None}),
            ("EVALUATED without results", "EVALUATED", {"results_json": None}),
            ("EVALUATED with an error code", "EVALUATED", {"error_code": "UNEXPECTED"}),
            ("EVALUATED without an engine version", "EVALUATED", {"engine_version": None}),
            ("EVALUATED without an engine fingerprint", "EVALUATED", {"engine_fingerprint": None}),
            ("EVALUATED with results that are not JSON", "EVALUATED", {"results_json": "not json"}),
            ("a 63-character engine fingerprint", "EVALUATED", {"engine_fingerprint": "a" * 63}),
            ("a 63-character rule-set hash", "EVALUATED", {"code_ruleset_hash": "a" * 63}),
            ("NOT_EVALUABLE with an engine error code", "NOT_EVALUABLE", {"error_code": "UNEXPECTED"}),
            ("NOT_EVALUABLE without a code", "NOT_EVALUABLE", {"error_code": None}),
            ("NOT_EVALUABLE with an outcome", "NOT_EVALUABLE", {"outcome": "PASS"}),
            ("NOT_EVALUABLE with results", "NOT_EVALUABLE", {"results_json": "[]"}),
            ("ENGINE_ERROR with the candidate code", "ENGINE_ERROR", {"error_code": "CANDIDATE_INVALID"}),
            ("ENGINE_ERROR without a code", "ENGINE_ERROR", {"error_code": None}),
            ("ENGINE_ERROR with an outcome", "ENGINE_ERROR", {"outcome": "REVIEW"}),
            ("RULESET_MISMATCH with an outcome", "RULESET_MISMATCH", {"outcome": "PASS"}),
            ("RULESET_MISMATCH with results", "RULESET_MISMATCH", {"results_json": "[]"}),
            ("RULESET_MISMATCH with an error code", "RULESET_MISMATCH", {"error_code": "UNEXPECTED"}),
            ("RULESET_MISMATCH without the in-code rule-set hash", "RULESET_MISMATCH", {"code_ruleset_hash": None}),
            ("a PREVIEW row with a prescription", "EVALUATED", {"prescription_id": 1}),
            ("no timestamp", "EVALUATED", {"created_at": None}),
        ]
        for label, status, overrides in invalid:
            # A row whose code hash differs from the activation's is refused first by
            # the F1 trigger (BEFORE INSERT); otherwise by a CHECK. Either way, rejected.
            check(f"[{tag}] result with {label} rejected", any(
                _raises(conn, _INSERT_RESULT, _result("clinic_a", act, "p", status=status, **overrides), expect)
                for expect in ("constraint failed", "shadow_ruleset_hash_inconsistent")))
        # The BEFORE INSERT draft trigger runs before the CHECK, so is_manual = 2
        # is refused by whichever fires first; either way nothing is written.
        check(f"[{tag}] result with an is_manual of 2 rejected", any(
            _raises(conn, _INSERT_RESULT, _result("clinic_a", act, "p", is_manual=2), expect)
            for expect in ("constraint failed", "shadow_preview_invalid")))
        # The F1 consistency trigger (BEFORE INSERT) refuses this before the CHECK can.
        check(f"[{tag}] result with EVALUATED without the in-code rule-set hash rejected", any(
            _raises(conn, _INSERT_RESULT, _result("clinic_a", act, "p", code_ruleset_hash=None), expect)
            for expect in ("constraint failed", "shadow_ruleset_hash_inconsistent")))
        unknown_status = list(_result("clinic_a", act, "p"))
        unknown_status[_RESULT_COLUMNS.index("status")] = "SKIPPED"
        check(f"[{tag}] result with an unknown status rejected", _raises(
            conn, _INSERT_RESULT, tuple(unknown_status), "constraint failed"))
        check(f"[{tag}] a CONFIRM row without a prescription rejected (CONFIRM trigger or CHECK)", any(
            _raises(conn, _INSERT_RESULT, _result("clinic_a", act, "p", stage="CONFIRM"), expect)
            for expect in ("constraint failed", "shadow_prescription_invalid")))
        check(f"[{tag}] nothing was written", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 0)

    _each_foreign_key_mode(body)


def test_clinic_isolation_and_activation_mode():
    def body(conn, tag):
        act_a = _activate(conn, "clinic_a")
        act_b = _activate(conn, "clinic_b")
        off_a = _activate(conn, "clinic_a", mode="OFF")
        _preview(conn, "pa", "clinic_a")
        _preview(conn, "pb", "clinic_b")
        _preview(conn, "pm", "clinic_a", is_manual=1)
        cases = [
            ("another clinic's activation", _result("clinic_a", act_b, "pa"), "shadow_activation_invalid"),
            ("an OFF activation", _result("clinic_a", off_a, "pa"), "shadow_activation_invalid"),
            ("a nonexistent activation", _result("clinic_a", 999, "pa"), "shadow_activation_invalid"),
            ("another clinic's draft", _result("clinic_a", act_a, "pb"), "shadow_preview_invalid"),
            ("a nonexistent draft", _result("clinic_a", act_a, "nope"), "shadow_preview_invalid"),
            ("an is_manual that differs from the draft's (AI draft as manual)",
             _result("clinic_a", act_a, "pa", is_manual=1), "shadow_preview_invalid"),
            ("an is_manual that differs from the draft's (manual draft as AI)",
             _result("clinic_a", act_a, "pm", is_manual=0), "shadow_preview_invalid"),
        ]
        for label, row, expect in cases:
            check(f"[{tag}] result with {label} rejected ({expect})", _raises(conn, _INSERT_RESULT, row, expect))
        check(f"[{tag}] nothing was written", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 0)

    _each_foreign_key_mode(body)


def test_strict_confirmation_pairing():
    def body(conn, tag):
        act = _activate(conn, "clinic_a")
        act_b = _activate(conn, "clinic_b")
        user_a, user_b = _user(conn, "clinic_a"), _user(conn, "clinic_b")
        for preview_id in ("p1", "p2", "p3"):
            _preview(conn, preview_id, "clinic_a")
        _preview(conn, "pb", "clinic_b")
        rx1, rx2, rx3 = (_prescription(conn, "clinic_a") for _ in range(3))
        rx_b = _prescription(conn, "clinic_b")
        _confirmed(conn, "clinic_a", rx1, "p1", user_a)          # rx1 <- p1
        _confirmed(conn, "clinic_a", rx2, "p2", user_a)          # rx2 <- p2
        _confirmed(conn, "clinic_b", rx_b, "pb", user_b)         # clinic B's own pair
        cases = [
            ("no PRESCRIPTION_CONFIRMED event for the prescription (rx3)",
             _result("clinic_a", act, "p3", stage="CONFIRM", prescription_id=rx3)),
            ("an event that pairs the prescription with a different draft (rx1 with p2)",
             _result("clinic_a", act, "p2", stage="CONFIRM", prescription_id=rx1)),
            ("a nonexistent prescription",
             _result("clinic_a", act, "p1", stage="CONFIRM", prescription_id=rx1 + 999)),
            ("another clinic's prescription",
             _result("clinic_a", act, "p1", stage="CONFIRM", prescription_id=rx_b)),
        ]
        for label, row in cases:
            check(f"[{tag}] CONFIRM with {label} rejected", _raises(conn, _INSERT_RESULT, row,
                                                                    "shadow_prescription_invalid"))
        check(f"[{tag}] nothing was written", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 0)

        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p1", stage="CONFIRM", prescription_id=rx1))
        conn.execute(_INSERT_RESULT, _result("clinic_b", act_b, "pb", stage="CONFIRM", prescription_id=rx_b))
        check(f"[{tag}] CONFIRM rows with their exact audit pairing are accepted, per clinic", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_results WHERE stage = 'CONFIRM'").fetchone()[0] == 2)
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p1"))
        check(f"[{tag}] the same draft may have one PREVIEW and one CONFIRM row", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_results WHERE preview_id = 'p1'").fetchone()[0] == 2)
        check(f"[{tag}] a second PREVIEW row for the draft rejected", _raises(
            conn, _INSERT_RESULT, _result("clinic_a", act, "p1", status="NOT_EVALUABLE"), "UNIQUE"))
        check(f"[{tag}] a second CONFIRM row for the draft rejected", _raises(
            conn, _INSERT_RESULT, _result("clinic_a", act, "p1", stage="CONFIRM", prescription_id=rx1), "UNIQUE"))

    _each_foreign_key_mode(body)


def test_results_are_append_only():
    def body(conn, tag):
        act = _activate(conn, "clinic_a")
        _preview(conn, "p", "clinic_a")
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p"))
        check(f"[{tag}] result UPDATE rejected", _raises(
            conn, "UPDATE guardrail_shadow_results SET outcome = 'REJECT'", (), "shadow_result_immutable"))
        check(f"[{tag}] result DELETE rejected", _raises(
            conn, "DELETE FROM guardrail_shadow_results", (), "shadow_result_immutable"))
        check(f"[{tag}] the row is unchanged", tuple(conn.execute(
            "SELECT status, outcome FROM guardrail_shadow_results").fetchone()) == ("EVALUATED", "PASS"))

    _each_foreign_key_mode(body)


def test_activation_race_is_left_to_the_recorder():
    """Documented behaviour: the database only requires the referenced
    activation to be a SHADOW row of the same clinic, not the clinic's
    latest. A recorder that read the activation just before a newer row
    was appended can still record under it; recording only under the latest
    activation is the recorder's responsibility (tested with the recorder)."""
    def body(conn, tag):
        older = _activate(conn, "clinic_a")
        _activate(conn, "clinic_a", mode="OFF")                    # switched off after `older` was read
        _preview(conn, "p1", "clinic_a")
        conn.execute(_INSERT_RESULT, _result("clinic_a", older, "p1"))
        check(f"[{tag}] a row under a SHADOW activation superseded by OFF is accepted by the database",
              conn.execute("SELECT activation_id FROM guardrail_shadow_results").fetchone()[0] == older)
        newer = _activate(conn, "clinic_a", ruleset_hash=OTHER_HASH)  # re-activated with a new rule set
        _preview(conn, "p2", "clinic_a")
        conn.execute(_INSERT_RESULT, _result("clinic_a", older, "p2"))
        check(f"[{tag}] a row under a SHADOW activation superseded by another SHADOW row is accepted",
              newer > older and conn.execute(
                  "SELECT COUNT(*) FROM guardrail_shadow_results WHERE activation_id = ?", (older,)).fetchone()[0] == 2)

    _each_foreign_key_mode(body)


# --- review corrections: F1 fingerprint consistency, F4 formats, F2 compatibility -------------

def test_f1_drift_status_must_match_the_activation_fingerprint():
    """EVALUATED needs code_ruleset_hash == the activation's ruleset_hash;
    RULESET_MISMATCH needs them to differ. Other statuses are unconstrained."""
    def body(conn, tag):
        act = _activate(conn, "clinic_a", ruleset_hash=HASH)
        for n in range(6):
            _preview(conn, f"p{n}", "clinic_a")
        inconsistent = [
            ("EVALUATED whose code hash differs from the activation's",
             _result("clinic_a", act, "p0", code_ruleset_hash=OTHER_HASH)),
            ("RULESET_MISMATCH whose code hash equals the activation's",
             _result("clinic_a", act, "p0", status="RULESET_MISMATCH", code_ruleset_hash=HASH)),
        ]
        for label, row in inconsistent:
            check(f"[{tag}] {label} rejected", _raises(conn, _INSERT_RESULT, row, "shadow_ruleset_hash_inconsistent"))
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p1", code_ruleset_hash=HASH))
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p2", status="RULESET_MISMATCH", code_ruleset_hash=OTHER_HASH))
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p3", status="NOT_EVALUABLE", code_ruleset_hash=OTHER_HASH))
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p4", status="ENGINE_ERROR", code_ruleset_hash=HASH))
        conn.execute(_INSERT_RESULT, _result("clinic_a", act, "p5", status="ENGINE_ERROR"))
        check(f"[{tag}] valid counterparts accepted: EVALUATED matching, MISMATCH differing, "
              "other statuses with any or no code hash", conn.execute(
                  "SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 5)
        older = act
        newer = _activate(conn, "clinic_a", ruleset_hash=OTHER_HASH)
        _preview(conn, "race", "clinic_a")
        check(f"[{tag}] consistency is judged against the row's own activation, not the latest",
              _raises(conn, _INSERT_RESULT, _result("clinic_a", older, "race", code_ruleset_hash=OTHER_HASH),
                      "shadow_ruleset_hash_inconsistent")
              and newer > older)
        conn.execute(_INSERT_RESULT, _result("clinic_a", older, "race", code_ruleset_hash=HASH))

    _each_foreign_key_mode(body)


def test_f4_formats_hex_fingerprints_json_array_and_non_blank_strings():
    def body(conn, tag):
        act = _activate(conn, "clinic_a")
        _preview(conn, "p", "clinic_a")
        bad_hashes = (("upper-case hex", "A" * 64), ("non-hex letters", "z" * 64), ("a dash", "a" * 63 + "-"),
                      ("63 characters", "a" * 63), ("65 characters", "a" * 65), ("empty", ""))
        for column in ("engine_fingerprint", "code_ruleset_hash"):
            for label, value in bad_hashes:
                check(f"[{tag}] {column} with {label} rejected", _raises(
                    conn, _INSERT_RESULT, _result("clinic_a", act, "p", status="ENGINE_ERROR", **{column: value}),
                    "constraint failed"))
        ins = ("INSERT INTO guardrail_shadow_activations (clinic_id, mode, ruleset_id, ruleset_version, ruleset_hash, "
               "activated_by, reason, created_at) VALUES (?, 'SHADOW', ?, ?, ?, 'op', 'r', ?)")
        for label, value in bad_hashes:
            check(f"[{tag}] activation ruleset_hash with {label} rejected", _raises(
                conn, ins, ("clinic_a", "R", "1", value, NOW), "constraint failed"))
        for label, args in (("a blank rule-set id", ("clinic_a", " ", "1", HASH, NOW)),
                            ("an empty rule-set version", ("clinic_a", "R", "", HASH, NOW)),
                            ("a blank clinic", ("  ", "R", "1", HASH, NOW)),
                            ("an empty timestamp", ("clinic_a", "R", "1", HASH, ""))):
            check(f"[{tag}] activation with {label} rejected", _raises(conn, ins, args, "constraint failed"))
        check(f"[{tag}] a lower-case hex activation hash is accepted",
              conn.execute(ins, ("clinic_a", "R", "1", "0123456789abcdef" * 4, NOW)).rowcount == 1)

        for label, value in (("an object", "{}"), ("a number", "5"), ("a string", '"x"'), ("JSON null", "null"),
                             ("malformed JSON", "not json"), ("an empty string", "")):
            check(f"[{tag}] results_json that is {label} rejected (a clean CHECK failure)", _raises(
                conn, _INSERT_RESULT, _result("clinic_a", act, "p", results_json=value), "CHECK constraint failed"))
        for label, overrides in (("a blank engine version", {"engine_version": "  "}),
                                 ("an empty in-code rule-set id", {"code_ruleset_id": ""}),
                                 ("a blank in-code rule-set version", {"code_ruleset_version": " "}),
                                 ("an empty timestamp", {"created_at": ""}),
                                 ("a blank timestamp", {"created_at": "  "})):
            check(f"[{tag}] result with {label} rejected", _raises(
                conn, _INSERT_RESULT, _result("clinic_a", act, "p", **overrides), "constraint failed"))
        check(f"[{tag}] nothing invalid was written", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 0)
        for n, results in enumerate(("[]", '[{"rule_id": "ENG-QUANTITY", "outcome": "PASS"}]')):
            _preview(conn, f"ok{n}", "clinic_a")
            conn.execute(_INSERT_RESULT, _result("clinic_a", act, f"ok{n}", results_json=results))
        check(f"[{tag}] JSON arrays (empty and non-empty) are accepted", conn.execute(
            "SELECT COUNT(*) FROM guardrail_shadow_results").fetchone()[0] == 2)

    _each_foreign_key_mode(body)


def test_f2_pre_deployment_compatibility_check():
    import importlib.util
    import subprocess
    spec = importlib.util.spec_from_file_location("check_sqlite_compat",
                                                  os.path.join(ROOT, "scripts", "check_sqlite_compat.py"))
    compat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compat)
    check("the supported runtime passes", compat.problems() == [], compat.problems())
    check("3.38.0 is the minimum accepted", compat.problems("3.38.0") == [])
    check("3.37.2 is rejected", any("older than the required 3.38.0" in p for p in compat.problems("3.37.2")))
    check("an unrecognised version is rejected", compat.problems("3.x") != [])

    class NoJson:
        def __init__(self, path):
            self.conn = sqlite3.connect(path)

        def execute(self, sql, *args):
            if "json_" in sql:
                raise sqlite3.OperationalError("no such function: json_valid")
            return self.conn.execute(sql, *args)

        def close(self):
            self.conn.close()

    check("a runtime without JSON functions is rejected",
          any("JSON functions are not usable" in p for p in compat.problems("3.51.2", connect=NoJson)))
    run = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_sqlite_compat.py")],
                         capture_output=True, text=True, timeout=60)
    check("the script exits 0 and reports OK on this runtime", run.returncode == 0 and run.stdout.startswith("OK"),
          run.stdout + run.stderr)
    check("the script opens no database file", "connect(\":memory:\")" in open(
        os.path.join(ROOT, "scripts", "check_sqlite_compat.py"), encoding="utf-8").read())


# --- historical compatibility --------------------------------------------------------------

def test_migration_017_changes_no_existing_row():
    conn, path, mig_dir = _migrated_db(up_to=16)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _preview(conn, "existing", "clinic_a")
        rx = _prescription(conn, "clinic_a")
        _confirmed(conn, "clinic_a", rx, "existing", _user(conn, "clinic_a"))
        conn.commit()
        tables = ("prescription_previews", "prescriptions", "audit_events", "users")
        before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        check("before 017, no shadow tables exist", conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'guardrail_shadow_%'").fetchone()[0] == 0)
        shutil.copy(os.path.join(MIGRATIONS_DIR, "017_guardrail_shadow.py"), mig_dir)
        from db.migrations import run_migrations
        run_migrations(conn=conn, migrations_dir=mig_dir)
        after = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        check("existing rows are byte-for-byte unchanged", after == before)
        check("017 applied on top of 001-016", conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 17)
    finally:
        _cleanup(conn, path, mig_dir)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
