"""
Per-user, per-endpoint rate limiting for the expensive authenticated routes.

Scope and honest limits
-----------------------
This is an in-process fixed-window limiter. It protects a single-process
deployment (which is what v0.1 is: one Uvicorn worker, one SQLite file) from
one account hammering an expensive route -- an LLM draft, the local extractor,
a confirm, or AI-2 question answering. It is deliberately NOT:

  * distributed -- each worker has its own counters, so with N workers the
    effective limit is N x the configured value;
  * durable -- counters reset on restart;
  * a defence against an attacker with many accounts (that needs per-IP and
    edge limiting).

Those properties are documented in documentation/docs/OPERATIONS_RUNBOOK.md
and README "Known limitations" rather than implied away.

Semantics
---------
* Keyed by (route name, authenticated user id), so one doctor cannot exhaust
  another doctor's budget.
* Fixed window of `window` seconds; on rejection returns 429 with
  `Retry-After` and `X-Error-Code: RATE_LIMITED`.
* A limit of 0 disables the route (used by tests that intentionally burst).
* Thread-safe: FastAPI runs sync endpoints in a thread pool.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException

from backend.rbac import AuthenticatedSession, require_auth


@dataclass(frozen=True)
class Rule:
    limit: int
    window: float = 60.0


def _from_env(name: str, default: int) -> int:
    raw = os.environ.get(f"AYURCORE_RATE_LIMIT_{name.upper()}_PER_MINUTE")
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


# Defaults are per minute. The AI draft paths are the ones that can cost money
# or CPU; ask is read-only but still builds a snapshot and runs the planner.
_RULES: dict[str, Rule] = {
    "ask": Rule(_from_env("ask", 120)),
    "preview": Rule(_from_env("preview", 30)),
    "preview_local": Rule(_from_env("preview_local", 60)),
    "confirm": Rule(_from_env("confirm", 60)),
}

_state: dict[tuple[str, int], list[float]] = {}
_lock = threading.Lock()


def configure(name: str, limit: int, window: float = 60.0) -> None:
    """Override one route's budget (tests, or a per-deployment decision)."""
    _RULES[name] = Rule(limit, window)
    reset()


def reset() -> None:
    with _lock:
        _state.clear()


def _check(name: str, user_id: int) -> None:
    rule = _RULES[name]
    if rule.limit <= 0:
        return
    now = time.monotonic()
    cutoff = now - rule.window
    key = (name, user_id)
    with _lock:
        hits = _state.setdefault(key, [])
        while hits and hits[0] <= cutoff:
            hits.pop(0)
        if len(hits) >= rule.limit:
            retry_after = max(1, int(rule.window - (now - hits[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Wait a moment and try again.",
                headers={"Retry-After": str(retry_after), "X-Error-Code": "RATE_LIMITED"},
            )
        hits.append(now)


def limiter(name: str):
    """FastAPI dependency: enforce the named budget for the calling user.

    Must be declared *after* the auth dependency in the endpoint signature.
    Kept local (no module-level singleton) so configuring a rule takes effect
    immediately.
    """
    if name not in _RULES:
        raise KeyError(f"unknown rate-limit route {name!r}")

    def dependency(auth: AuthenticatedSession = Depends(require_auth)) -> None:
        _check(name, auth.user_id)

    return dependency


def snapshot() -> dict[str, dict[str, int]]:
    """Current limits, for the ops runbook and tests."""
    return {name: {"limit": rule.limit, "window_seconds": int(rule.window)} for name, rule in _RULES.items()}
