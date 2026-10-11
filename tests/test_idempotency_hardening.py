"""
A6.3 hardening, item 2 -- Idempotency-Key hardening.

Per this batch's explicit instruction, this file FIRST determines the
CURRENT behavior of the `Idempotency-Key` header (backend/main.py's
prescribe_confirm, backed by backend/idempotency_store.py) for a set of
edge-case values, and only then adds a bound where genuinely unbounded
work was found -- it does not add validation reflexively.

What was found (see backend/main.py's own new comment on the
`idempotency_key` parameter for the full reasoning):
  - Empty string ("") was ALREADY safe: `if idempotency_key:` in
    prescribe_confirm treats it as falsy, so it behaves exactly like an
    omitted header -- no idempotency_keys row, no dedup guarantee, normal
    pre-A4 success/failure semantics. Documented here as safe; NOT
    changed.
  - Whitespace-only and Unicode keys were ALREADY safe: they are ordinary
    (if unusual) non-empty strings, stored verbatim and scoped by
    (user_id, operation, idempotency_key) exactly like any other key
    value -- dedup/replay/conflict semantics all apply identically, and
    there is no cross-user or cross-clinic exposure since the composite
    key always includes the server-derived user_id. Documented here as
    safe; NOT changed.
  - A very long key WAS genuinely unbounded: the `Idempotency-Key` header
    had no length constraint anywhere in this codebase (not on the
    FastAPI Header() parameter, not in idempotency_store.py, not at the
    SQLite column level) before this batch. Fixed with the smallest
    possible change: `max_length=200` added directly to the existing
    `Header(...)` declaration in backend/main.py -- the same declarative
    bound every other free-text field in this API already uses, giving
    the same automatic 422 response, no new error-handling code.

This file also adds the required real-concurrency test: the SAME
authenticated user, the SAME Idempotency-Key, two DIFFERENT request
payloads (two different previews), submitted concurrently -- proving
exactly one commits, the other gets the existing, deterministic
IdempotencyConflictError -> 409, and neither the database nor the
idempotency_keys row ends up in a corrupted or double-committed state.

A6.3 failure diagnosis + minimal test correction (Sep 2026): a Mac run
of this file reported 3 failures, all three in this file's own tests
(never in production code). A read-only diagnosis (accepted by the
user) classified all three as test/framework issues:
  - The whitespace-only-key and exactly-200-char-boundary-key tests both
    used a HARDCODED, non-randomized key literal ("   " and "k" * 200)
    in an exact-count (`== 1`) database assertion, against this
    project's real, persistent, never-torn-down `db/ayurcore.db`. Each
    run logs in as a brand-new randomized user, so a hardcoded key never
    REPLAYS across runs (no primary-key collision) -- it just
    accumulates one additional row every run, which is the exact same
    test-isolation bug class this project's own test_idempotency.py
    already documented and fixed once (see that file's
    "TEST-ISOLATION INCIDENT" docstring and its `_unique_patient_name()`
    helper). Fixed here the same way: both keys are now unique per run
    (a random token embedded inside the whitespace padding / inside the
    200-character boundary string, respectively), and the row-count
    assertions are additionally scoped to the specific authenticated
    user this test itself created, via the new
    `_count_idempotency_rows_for_user()` helper below.
  - The Unicode-key test sent a Python `str` containing an astral-plane
    character (an emoji, U+1F33F) directly as a raw HTTP header value.
    httpx encodes `str` header values with the 'ascii' codec at
    request-construction time -- inside `client.post(...)`, before any
    ASGI/network call is made -- so this raised UnicodeEncodeError
    client-side, never reaching this application at all. This is an
    HTTP-client/protocol limitation (not a production defect), and the
    test below now documents it as exactly that via a guarded
    `except UnicodeEncodeError`, while separately proving that a
    genuinely representable extended-byte (Latin-1 / obs-text range)
    header value -- sent as raw bytes, the standards-correct way to
    represent such a value over HTTP -- IS handled safely end to end.

A6.3 final diagnosis + minimal test correction (Sep 2026): after the
above fix, one failure remained --
test_unicode_idempotency_key_is_handled_safely's Part B, which asserted
the exact string stored in the database for the raw-bytes extended-byte
key equaled `extended_byte_key.decode("latin-1")`. Root cause, confirmed
empirically against this project's actual installed httpx + Starlette:
Starlette's TestClient transport (`_TestClientTransport.handle_request`
in starlette/testclient.py) rebuilds the ASGI scope's headers via
`[(key.lower().encode(), value.encode()) for key, value in
request.headers.multi_items()]` -- `multi_items()` yields a str already
latin-1-decoded by httpx, and the bare `.encode()` that follows uses
Python's DEFAULT codec (UTF-8), not latin-1, silently re-encoding any
byte >= 0x80 a second time before the ASGI app's own (correct,
single-pass) latin-1 decode ever sees it. This is an artifact of driving
the app in-process through TestClient specifically -- a real ASGI server
(uvicorn) decodes a client's raw wire bytes via latin-1 exactly once, so
production (backend/main.py, backend/idempotency_store.py) is
unaffected and was never at fault. Fixed by no longer asserting what the
exact stored string is: the test now scopes its row-count assertions to
this run's own (user, operation) via the new
`_count_idempotency_rows_for_user_operation()` /
`_idempotency_rows_for_user_operation()` helpers, and reads back
whatever string was actually stored purely for diagnostic visibility.

Run directly (no pytest required):
    python tests/test_idempotency_hardening.py
Or under pytest:
    pytest tests/test_idempotency_hardening.py -v
"""
import os
import secrets
import sqlite3
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Raises AssertionError immediately on a failed assertion -- same
    convention as every other test file in this project (see
    tests/test_rbac.py's module docstring, "PYTEST COMPATIBILITY")."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _create_and_login_user(client, clinic_id: str = "default_clinic"):
    """Same setup as _login_new_user, but also returns the username, so a
    test can scope its own database assertions to exactly the row it
    created (see _count_idempotency_rows_for_user) instead of relying on
    key-string uniqueness alone. _login_new_user (below) is a thin
    wrapper around this for the tests that don't need the username."""
    from backend.auth_service import create_user

    username = f"idem_hardening_test_{secrets.token_hex(6)}"
    password = "idem-hardening-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}, username


def _login_new_user(client, clinic_id: str = "default_clinic") -> dict:
    headers, _username = _create_and_login_user(client, clinic_id=clinic_id)
    return headers


def _set_stock(herb_name: str, grams: int, clinic_id: str = "default_clinic"):
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "UPDATE inventory SET stock_grams = ? WHERE herb_name = ? AND clinic_id = ?",
            (grams, herb_name, clinic_id),
        )
        if cursor.rowcount == 0:
            conn.execute(
                "INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (clinic_id, herb_name, "test botanical name", grams, "TEST-BATCH", 90),
            )
        conn.commit()
    finally:
        conn.close()


def _get_stock(herb_name: str, clinic_id: str = "default_clinic") -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?", (herb_name, clinic_id)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _count_idempotency_rows(idempotency_key: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()[0]
    finally:
        conn.close()


def _count_idempotency_rows_for_user(idempotency_key: str, username: str) -> int:
    """Same query as _count_idempotency_rows, but additionally scoped (via
    a join to users.username) to the single authenticated user this test
    itself created and logged in as. Combined with a per-run-unique key
    value, this makes the count immune to accumulation from any other
    test run against this project's real, persistent, never-torn-down
    database -- see this file's module docstring, "A6.3 failure diagnosis
    + minimal test correction", for the incident this guards against."""
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM idempotency_keys ik "
            "JOIN users u ON u.user_id = ik.user_id "
            "WHERE ik.idempotency_key = ? AND u.username = ?",
            (idempotency_key, username),
        ).fetchone()[0]
    finally:
        conn.close()


def _count_idempotency_rows_for_user_operation(username: str, operation: str) -> int:
    """Counts idempotency_keys rows for a given user (by username) and
    operation, WITHOUT assuming any particular key string. Used instead of
    _count_idempotency_rows_for_user() wherever the actual stored key
    string cannot be reliably predicted in advance -- see this file's
    module docstring, "A6.3 final diagnosis", for why: Starlette's
    TestClient transport re-encodes an already latin-1-decoded header
    string with a bare `.encode()` (Python's default, UTF-8) when
    rebuilding the ASGI scope, so a raw-bytes header value outside plain
    ASCII does not survive this in-process transport with byte-for-byte
    fidelity. That is a TestClient-only artifact, not a production
    behavior -- but it means a test cannot reliably precompute the exact
    string that will be stored for a value it sent as raw, non-ASCII
    bytes, so this helper scopes by (user, operation) instead of by key."""
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM idempotency_keys ik "
            "JOIN users u ON u.user_id = ik.user_id "
            "WHERE u.username = ? AND ik.operation = ?",
            (username, operation),
        ).fetchone()[0]
    finally:
        conn.close()


def _idempotency_rows_for_user_operation(username: str, operation: str):
    """Same scoping as _count_idempotency_rows_for_user_operation(), but
    returns the full rows (as dicts) rather than just a count, so a test
    can read back whatever key string was actually stored."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ik.* FROM idempotency_keys ik "
            "JOIN users u ON u.user_id = ik.user_id "
            "WHERE u.username = ? AND ik.operation = ?",
            (username, operation),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _idempotency_row(idempotency_key: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count_prescriptions_for_patient(patient_name: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM prescriptions WHERE patient_name = ?", (patient_name,)
        ).fetchone()[0]
    finally:
        conn.close()


def _unique_patient_name(label: str) -> str:
    """Same isolation strategy as tests/test_idempotency.py's identical
    helper -- see that file's module docstring for the full incident this
    fixes (counting a real, persistent, never-torn-down database by a
    hardcoded literal patient_name)."""
    return f"{label} {secrets.token_hex(6)}"


def _manual_preview_payload(patient_name="Idem Hardening Patient", herb="Ashwagandha", grams=5):
    return {
        "patient_name": patient_name,
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": herb, "grams": grams}],
    }


# --- 1. Empty key: already safe, behaves as "no key sent" ---------------

def test_empty_idempotency_key_behaves_as_no_key_sent():
    print("\nTest: an empty-string Idempotency-Key header is treated exactly like an omitted one (documents existing, unchanged behavior)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("Empty Key Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": ""})
        check("confirm with an empty Idempotency-Key header still succeeds normally", resp.status_code == 200, resp.text)
        check("no idempotency_keys row was created for the literal empty-string key", _count_idempotency_rows("") == 0)
        check("exactly one prescription was created (normal, non-idempotent path)", _count_prescriptions_for_patient(patient_name) == 1)


# --- 2. Whitespace-only key: already safe, used literally ----------------

def test_whitespace_only_idempotency_key_is_used_literally_and_safely():
    print("\nTest: a whitespace-leading/trailing Idempotency-Key is a real, ordinary key value -- it is not trimmed/normalized, and dedup/replay work correctly for it")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, username = _create_and_login_user(client)
        _set_stock("Ashwagandha", 5000)
        # A per-run-unique key is required: this suite runs against the
        # real, persistent, never-torn-down database, and a hardcoded
        # "   " key (this test's original form) accumulated one
        # additional idempotency_keys row every run -- see the module
        # docstring's "A6.3 failure diagnosis" section. Embedding a
        # random token INSIDE the leading/trailing whitespace preserves
        # exactly the property this test exercises (a key that carries
        # real leading/trailing whitespace, which this stack does NOT
        # strip or normalize) while making the key unique per run.
        whitespace_key = f"   {secrets.token_hex(8)}   "
        assert whitespace_key.strip() != whitespace_key, "sanity check: this key must genuinely carry leading/trailing whitespace"
        patient_name = _unique_patient_name("Whitespace Key Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        first = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": whitespace_key})
        check("first confirm with a whitespace-leading/trailing key succeeds", first.status_code == 200, first.text)
        observed_count = _count_idempotency_rows_for_user(whitespace_key, username)
        check(
            "exactly one idempotency_keys row now exists for this run's own whitespace-padded key, scoped to this run's own user",
            observed_count == 1,
            f"observed count = {observed_count} for key={whitespace_key!r}, username={username!r}",
        )

        # Replay: same key, same payload -- must return the ORIGINAL response, not re-execute.
        replay = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": whitespace_key})
        check("replay with the same whitespace-padded key returns the exact original response", replay.status_code == 200 and replay.json() == first.json(), (first.json(), replay.json()))
        check("still exactly ONE prescription -- the replay did not create a second one", _count_prescriptions_for_patient(patient_name) == 1)
        observed_count_after_replay = _count_idempotency_rows_for_user(whitespace_key, username)
        check(
            "still exactly ONE idempotency_keys row -- the replay did not duplicate it",
            observed_count_after_replay == 1,
            f"observed count = {observed_count_after_replay} for key={whitespace_key!r}, username={username!r}",
        )


# --- 3. Unicode key: already safe -----------------------------------------

def test_unicode_idempotency_key_is_handled_safely():
    print("\nTest: an Idempotency-Key value outside plain ASCII is handled safely wherever this HTTP stack can actually represent it as a raw header value; a genuinely non-representable value (an astral-plane emoji, as a Python str) is documented -- not silently miscounted -- as an HTTP-client/protocol limitation, not an application defect")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, username = _create_and_login_user(client)
        _set_stock("Ashwagandha", 5000)

        # --- Part A: document, don't paper over, the HTTP-client limit ---
        # An astral-plane Unicode Python `str` (an emoji, U+1F33F) is NOT
        # a representable raw HTTP header value through this stack: httpx
        # encodes `str` header values with the 'ascii' codec at
        # request-construction time -- inside client.post(...), before
        # any ASGI/network call -- confirmed by the Mac's own reported
        # UnicodeEncodeError ("'ascii' codec can't encode character ...").
        # This is an HTTP-client/protocol limitation (category A in the
        # A6.3 diagnosis), never reaching backend/main.py,
        # idempotency_store.py, or SQLite. It is guarded here, not
        # asserted away as a pass/fail of application behavior -- if this
        # stack's encoding behavior ever changes such that no exception is
        # raised, that is a signal to re-diagnose, not a silent pass, so
        # it is reported via check() with an explicit failure message
        # rather than being ignored.
        astral_key_str = f"🌿-key-{secrets.token_hex(6)}-clinic"
        try:
            client.post(
                "/prescribe/confirm",
                json={"preview_id": "unicode-test-never-reached"},
                headers={**headers, "Idempotency-Key": astral_key_str},
            )
        except UnicodeEncodeError as e:
            print(
                f"=> DOCUMENTED (HTTP-client/protocol limitation, category A -- not an application failure): "
                f"an astral-plane Unicode str header value raises UnicodeEncodeError inside the HTTP client "
                f"itself, before any request reaches this application. ({e})"
            )
        else:
            check(
                "an astral-plane Unicode str WAS representable as a raw header value on this HTTP stack -- if "
                "this ever passes, the HTTP client's header-encoding behavior has changed and this should be "
                "re-diagnosed rather than assumed to still be a client-side limitation",
                False,
                "expected UnicodeEncodeError to be raised by the HTTP client before the request was sent",
            )

        # --- Part B: a genuinely representable extended-byte value IS ---
        # --- handled safely, end to end -----------------------------------
        # A raw `bytes` header value (the standards-correct way to send an
        # obs-text / Latin-1-range header value over HTTP, and the way to
        # bypass httpx's str->ascii encode step entirely) is stored,
        # deduplicated, and replayed correctly, exactly like any other key.
        # 0xE9 is 'e' with an acute accent ('e') in Latin-1.
        #
        # A6.3 final diagnosis (Sep 2026): this test previously asserted
        # the exact stored string was `extended_byte_key.decode("latin-1")`
        # and that assertion failed on the Mac. Root cause, confirmed
        # empirically against this project's actual installed httpx +
        # Starlette: Starlette's TestClient transport
        # (`_TestClientTransport.handle_request` in starlette/testclient.py)
        # rebuilds the ASGI scope's headers via
        # `[(key.lower().encode(), value.encode()) for key, value in
        # request.headers.multi_items()]` -- `multi_items()` yields a str
        # already latin-1-decoded by httpx, and the bare `.encode()` that
        # follows uses Python's DEFAULT codec (UTF-8), not latin-1. That
        # silently re-encodes any byte >= 0x80 before the ASGI app's own
        # (correct, single-pass) latin-1 decode ever sees it, so a raw
        # non-ASCII byte value does NOT survive this in-process transport
        # with round-trip fidelity. This is an artifact of driving the app
        # through TestClient specifically -- a real ASGI server (uvicorn)
        # decodes the client's raw wire bytes via latin-1 exactly once, so
        # production is unaffected. Rather than depend on precisely
        # replicating TestClient's internal double round-trip, this test
        # now proves dedup/replay safety by counting rows scoped to this
        # run's own (user, operation) -- never asserting what the exact
        # stored string is -- and reads back whatever was actually stored
        # for diagnostic visibility only.
        from backend.idempotency_store import OPERATION_PRESCRIBE_CONFIRM

        unique_suffix = secrets.token_hex(6).encode("ascii")
        extended_byte_key = b"caf\xe9-key-" + unique_suffix + b"-clinic"
        try:
            request_headers = [(k.encode("ascii"), v.encode("ascii")) for k, v in headers.items()]
            request_headers.append((b"Idempotency-Key", extended_byte_key))
        except (TypeError, ValueError, AttributeError) as e:
            print(f"=> NOT TESTED: could not construct a raw-bytes header list on this HTTP client ({e}); "
                  f"skipping the extended-byte representability assertions rather than inventing a result.")
            return

        patient_name = _unique_patient_name("Extended Byte Key Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        # A brand-new user (just created above) has made no /prescribe/confirm
        # calls yet, and Part A's request never reached the application at
        # all -- so this must be zero before the request below.
        rows_before = _count_idempotency_rows_for_user_operation(username, OPERATION_PRESCRIBE_CONFIRM)
        check("sanity check: this freshly created user has no prior idempotency rows for this operation", rows_before == 0, rows_before)

        try:
            resp = client.post(
                "/prescribe/confirm",
                json={"preview_id": preview_id},
                headers=request_headers,
            )
        except (TypeError, ValueError, AttributeError) as e:
            print(f"=> NOT TESTED: this HTTP test client does not accept raw bytes header values the way this "
                  f"test expected ({e}); skipping the extended-byte representability assertions rather than "
                  f"inventing a result.")
            return

        check("confirm with a representable extended-byte (Latin-1) Idempotency-Key succeeds (no crash, no 500)", resp.status_code == 200, resp.text)

        rows_after_first = _idempotency_rows_for_user_operation(username, OPERATION_PRESCRIBE_CONFIRM)
        check(
            "exactly one idempotency_keys row now exists for this run's own user+operation "
            "(the exact stored key string is deliberately NOT asserted here -- see this test's Part B "
            "comment / this file's module docstring for why TestClient's transport does not preserve "
            "raw non-ASCII header bytes with round-trip fidelity, a test-transport artifact, not a "
            "production behavior)",
            len(rows_after_first) == 1,
            f"rows for this user+operation = {rows_after_first}",
        )
        actual_stored_key = rows_after_first[0]["idempotency_key"]
        print(f"=> observed (diagnostic only, not asserted): the value actually stored for this run's "
              f"extended-byte header is {actual_stored_key!r} (raw bytes sent were {extended_byte_key!r})")

        replay = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=request_headers)
        check("replay with the identical extended-byte key returns the original response", replay.status_code == 200 and replay.json() == resp.json(), (resp.json(), replay.json()))
        check("still exactly ONE prescription", _count_prescriptions_for_patient(patient_name) == 1)

        rows_after_replay = _idempotency_rows_for_user_operation(username, OPERATION_PRESCRIBE_CONFIRM)
        check(
            "still exactly ONE idempotency_keys row for this user+operation -- the replay did not duplicate it",
            len(rows_after_replay) == 1,
            f"rows for this user+operation = {rows_after_replay}",
        )
        check(
            "the replay's row is the SAME row (not a second row that happens to also count as 1) -- same "
            "primary key values throughout",
            rows_after_replay[0]["idempotency_key"] == actual_stored_key,
            (rows_after_replay[0], actual_stored_key),
        )


# --- 4. Very long key: WAS genuinely unbounded -- now rejected -----------

def test_idempotency_key_over_the_new_bound_is_rejected_with_422():
    print("\nTest: an Idempotency-Key over 200 characters is rejected with 422 (the new bound this batch adds) -- boundary+1 case")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("Oversized Key Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        oversized_key = "k" * 201
        resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": oversized_key})
        check("a 201-character Idempotency-Key is rejected with 422", resp.status_code == 422, resp.text)
        check("no idempotency_keys row was created for the rejected oversized key", _count_idempotency_rows(oversized_key) == 0)
        check("no prescription was created by the rejected request", _count_prescriptions_for_patient(patient_name) == 0)

        # A genuinely pathological size (well beyond the boundary) is
        # rejected the same clean way, not with a 500 from downstream code
        # trying to process it.
        pathological_key = "k" * 100_000
        resp_huge = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": pathological_key})
        check("a 100,000-character Idempotency-Key is also cleanly rejected with 422, not a 500", resp_huge.status_code == 422, resp_huge.status_code)


def test_idempotency_key_at_exactly_the_bound_is_accepted():
    print("\nTest: an Idempotency-Key of exactly 200 characters (the new bound) is accepted normally, produces exactly one row, and replays correctly -- boundary case")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, username = _create_and_login_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("Boundary Key Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        # A per-run-unique key is required for the same reason as the
        # whitespace-key test above (see the module docstring's "A6.3
        # failure diagnosis" section): a hardcoded "k" * 200 literal
        # accumulated one additional idempotency_keys row every run. A
        # random token is embedded in the key while the total length is
        # still forced to exactly 200 characters, preserving the actual
        # boundary condition under test.
        _unique_prefix = f"boundary-{secrets.token_hex(8)}-"
        boundary_key = (_unique_prefix + "k" * 200)[:200]
        assert len(boundary_key) == 200, f"sanity check: boundary key must be exactly 200 chars, got {len(boundary_key)}"

        resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": boundary_key})
        check("a 200-character Idempotency-Key (exactly at the bound) is accepted", resp.status_code == 200, resp.text)
        observed_count = _count_idempotency_rows_for_user(boundary_key, username)
        check(
            "exactly one idempotency_keys row was created for it, scoped to this run's own user",
            observed_count == 1,
            f"observed count = {observed_count} for key={boundary_key!r}, username={username!r}",
        )

        # Replay semantics must remain intact at the boundary too: same
        # key, same payload -- must return the ORIGINAL response, not
        # re-execute and not create a second row or a second prescription.
        replay = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": boundary_key})
        check("replay with the identical 200-character boundary key returns the exact original response", replay.status_code == 200 and replay.json() == resp.json(), (resp.json(), replay.json()))
        check("still exactly ONE prescription -- the replay did not create a second one", _count_prescriptions_for_patient(patient_name) == 1)
        observed_count_after_replay = _count_idempotency_rows_for_user(boundary_key, username)
        check(
            "still exactly ONE idempotency_keys row -- the replay did not duplicate it",
            observed_count_after_replay == 1,
            f"observed count = {observed_count_after_replay} for key={boundary_key!r}, username={username!r}",
        )


# --- 5. Required concurrency test: same key, DIFFERENT payloads ----------

def test_concurrent_confirms_same_key_different_payloads():
    print("\nTest: same user + same Idempotency-Key + DIFFERENT payloads (two different previews), submitted concurrently")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client, clinic_id="idem_hardening_concurrent_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_hardening_concurrent_clinic")

        patient_a = _unique_patient_name("Concurrent Different Payload A")
        patient_b = _unique_patient_name("Concurrent Different Payload B")
        preview_a = client.post(
            "/prescribe/manual-preview",
            json=_manual_preview_payload(patient_name=patient_a, herb="Ashwagandha", grams=7),
            headers=headers,
        ).json()["preview_id"]
        preview_b = client.post(
            "/prescribe/manual-preview",
            json=_manual_preview_payload(patient_name=patient_b, herb="Ashwagandha", grams=13),
            headers=headers,
        ).json()["preview_id"]

        shared_key = secrets.token_hex(16)
        results = {}
        errors = {}
        barrier = threading.Barrier(2)

        def attempt(name: str, preview_id: str):
            try:
                barrier.wait()
                results[name] = client.post(
                    "/prescribe/confirm",
                    json={"preview_id": preview_id},
                    headers={**headers, "Idempotency-Key": shared_key},
                )
            except Exception as e:
                errors[name] = e

        t1 = threading.Thread(target=attempt, args=("A", preview_a))
        t2 = threading.Thread(target=attempt, args=("B", preview_b))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        check("both concurrent requests completed without raising", not errors, errors)
        check("both concurrent requests returned a response", set(results.keys()) == {"A", "B"}, results)

        statuses = {name: r.status_code for name, r in results.items()}
        winners = [name for name, status in statuses.items() if status == 200]
        losers = [name for name, status in statuses.items() if status == 409]
        check(
            "exactly ONE of the two concurrent, different-payload requests committed (200)",
            len(winners) == 1,
            statuses,
        )
        check(
            "exactly ONE of the two got the deterministic conflict rejection (409), never a 500 or a silent drop",
            len(losers) == 1,
            statuses,
        )
        loser_name = losers[0]
        check(
            "the loser's 409 is the existing IdempotencyConflictError -- reused key, different request",
            "different request" in results[loser_name].text,
            results[loser_name].text,
        )

        winner_patient = patient_a if winners[0] == "A" else patient_b
        loser_patient = patient_b if winners[0] == "A" else patient_a
        check("exactly ONE prescription total was created -- for the WINNER's patient only", _count_prescriptions_for_patient(winner_patient) == 1, winner_patient)
        check("ZERO prescriptions were created for the LOSER's patient -- the losing payload was never executed", _count_prescriptions_for_patient(loser_patient) == 0, loser_patient)

        winner_grams = 7 if winners[0] == "A" else 13
        check(
            "inventory reflects exactly ONE deduction -- the winner's grams only, never both, never neither",
            _get_stock("Ashwagandha", clinic_id="idem_hardening_concurrent_clinic") == 5000 - winner_grams,
            _get_stock("Ashwagandha", clinic_id="idem_hardening_concurrent_clinic"),
        )

        check("the replayed/rejected response never carries the WINNER's prescription data", "prescription_id" not in results[loser_name].text, results[loser_name].text)

        row = _idempotency_row(shared_key)
        check("exactly one idempotency_keys row exists for the shared key -- no corrupted/duplicate row", _count_idempotency_rows(shared_key) == 1, row)
        check(
            "that row is fully populated (SUCCEEDED), never left in a NULL/half-written state, per "
            "migrations/006's documented two-state invariant",
            row is not None and row["response_status_code"] is not None and row["response_body_json"] is not None,
            row,
        )


_ALL_TESTS = [
    test_empty_idempotency_key_behaves_as_no_key_sent,
    test_whitespace_only_idempotency_key_is_used_literally_and_safely,
    test_unicode_idempotency_key_is_handled_safely,
    test_idempotency_key_over_the_new_bound_is_rejected_with_422,
    test_idempotency_key_at_exactly_the_bound_is_accepted,
    test_concurrent_confirms_same_key_different_payloads,
]


def run_tests():
    print("--- A6.3 hardening: Idempotency-Key edge cases + concurrency ---\n")
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")
        print()
    print(f"--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    return _function_failures


if __name__ == "__main__":
    _failures = run_tests()
    if _failures:
        sys.exit(1)
