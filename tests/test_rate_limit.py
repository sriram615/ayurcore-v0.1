"""
Tests for authenticated endpoint rate limiting (Section 4.1).

Verifies:
  1. Route limit enforcement returns 429, Retry-After header, and X-Error-Code: RATE_LIMITED.
  2. Per-user isolation: User A's exhausted quota does not affect User B.
  3. Reset clears the in-process counters.
  4. Unauthenticated (401) and unauthorized (403) calls do not consume budget.
  5. Configuring limit=0 disables rate limiting for that route.
"""
from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
import backend.rate_limit as rate_limit
from backend.auth_service import create_session, create_user


@pytest.fixture(autouse=True)
def clean_rate_limits():
    rate_limit.reset()
    yield
    rate_limit.reset()
    # Restore defaults
    rate_limit.configure("preview", 30, 60.0)
    rate_limit.configure("preview_local", 60, 60.0)
    rate_limit.configure("confirm", 60, 60.0)
    rate_limit.configure("ask", 120, 60.0)


def _create_user(clinic_id: str, role: str, username: str) -> tuple[int, str]:
    user_id = create_user(username=username, password="TestPass123!", clinic_id=clinic_id, role=role)
    token, _ = create_session(user_id)
    return user_id, token


def test_rate_limit_exceeded_returns_429():
    rate_limit.configure("preview_local", 2, window=60.0)
    _, token = _create_user("C_RL1", "DOCTOR", "dr_rl1")

    client = TestClient(main_module.app)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "patient_name": "Test Patient",
        "notes": "Pitta aggravated. Start Neem 95 g. Give Manjistha 70g.",
    }

    # Request 1: succeeds (200)
    res1 = client.post("/prescribe/preview/local", json=payload, headers=headers)
    assert res1.status_code == 200, res1.text

    # Request 2: succeeds (200)
    res2 = client.post("/prescribe/preview/local", json=payload, headers=headers)
    assert res2.status_code == 200, res2.text

    # Request 3: exceeds limit -> 429
    res3 = client.post("/prescribe/preview/local", json=payload, headers=headers)
    assert res3.status_code == 429
    assert res3.headers.get("X-Error-Code") == "RATE_LIMITED"
    assert "Retry-After" in res3.headers
    retry_after = int(res3.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert "Too many requests" in res3.json()["detail"]


def test_rate_limit_isolated_per_user():
    rate_limit.configure("preview_local", 2, window=60.0)
    _, token1 = _create_user("C_RL2", "DOCTOR", "dr_rl2_a")
    _, token2 = _create_user("C_RL2", "DOCTOR", "dr_rl2_b")

    client = TestClient(main_module.app)
    payload = {
        "patient_name": "Test Patient",
        "notes": "Pitta aggravated. Start Neem 95 g. Give Manjistha 70g.",
    }

    # User 1 exhausts budget
    client.post("/prescribe/preview/local", json=payload, headers={"Authorization": f"Bearer {token1}"})
    client.post("/prescribe/preview/local", json=payload, headers={"Authorization": f"Bearer {token1}"})
    res_u1 = client.post("/prescribe/preview/local", json=payload, headers={"Authorization": f"Bearer {token1}"})
    assert res_u1.status_code == 429

    # User 2 has separate budget and succeeds
    res_u2 = client.post("/prescribe/preview/local", json=payload, headers={"Authorization": f"Bearer {token2}"})
    assert res_u2.status_code == 200


def test_rate_limit_reset_clears_budget():
    rate_limit.configure("preview_local", 1, window=60.0)
    _, token = _create_user("C_RL3", "DOCTOR", "dr_rl3")

    client = TestClient(main_module.app)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "patient_name": "Test Patient",
        "notes": "Pitta aggravated. Start Neem 95 g. Give Manjistha 70g.",
    }

    res1 = client.post("/prescribe/preview/local", json=payload, headers=headers)
    assert res1.status_code == 200

    res2 = client.post("/prescribe/preview/local", json=payload, headers=headers)
    assert res2.status_code == 429

    # Reset allows immediate retry
    rate_limit.reset()
    res3 = client.post("/prescribe/preview/local", json=payload, headers=headers)
    assert res3.status_code == 200


def test_unauthenticated_and_unauthorized_do_not_consume_budget():
    rate_limit.configure("preview_local", 1, window=60.0)
    _, doc_token = _create_user("C_RL4", "DOCTOR", "dr_rl4")
    _, auditor_token = _create_user("C_RL4", "AUDITOR", "aud_rl4")

    client = TestClient(main_module.app)
    payload = {"patient_name": "Test", "notes": "notes"}

    # 401 unauthenticated
    res_401 = client.post("/prescribe/preview/local", json=payload)
    assert res_401.status_code == 401

    # 403 forbidden (Auditor cannot preview)
    res_403 = client.post("/prescribe/preview/local", json=payload, headers={"Authorization": f"Bearer {auditor_token}"})
    assert res_403.status_code == 403

    # Doctor still has full quota available (1 request allowed)
    res_doc = client.post(
        "/prescribe/preview/local",
        json={"patient_name": "Test", "notes": "Pitta aggravated. Start Neem 95 g. Give Manjistha 70g."},
        headers={"Authorization": f"Bearer {doc_token}"},
    )
    assert res_doc.status_code == 200


def test_rate_limit_zero_disables_limiting():
    rate_limit.configure("preview_local", 0)
    _, token = _create_user("C_RL5", "DOCTOR", "dr_rl5")

    client = TestClient(main_module.app)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "patient_name": "Test Patient",
        "notes": "Pitta aggravated. Start Neem 95 g. Give Manjistha 70g.",
    }

    for _ in range(5):
        res = client.post("/prescribe/preview/local", json=payload, headers=headers)
        assert res.status_code == 200


def test_unknown_route_raises_key_error():
    with pytest.raises(KeyError, match="unknown rate-limit route 'invalid'"):
        rate_limit.limiter("invalid")
