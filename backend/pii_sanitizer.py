"""
Local PII air-gap for AyurCore.

Enforces: real patient identity → local tokenization → Gemini → local rehydration.
Never: real patient identity → Gemini → redact response.

This module has three responsibilities and nothing else:
  1. tokenize_payload   - replace known PII values with opaque local tokens
                           BEFORE anything is sent to an external AI service.
  2. assert_no_pii_leak - a pre-flight invariant checked immediately before
                           the network call. If it raises, the caller MUST
                           abort and must NOT call the AI service.
  3. rehydrate_payload  - restore real PII into the AI's structured response
                           after it comes back, using a mapping that never
                           left this process.

Design notes (see AyurCore Phase 1 spec):
  - Tokens are generated with `secrets` (CSPRNG), not a counter, and contain
    no fragment of the original value (no name, no digits from a phone/ID).
  - The same PII value always maps to the same token within one call to
    tokenize_payload (deterministic within one request), so multiple
    mentions of the same patient collapse to one token.
  - Longest-value-first replacement prevents a short value (e.g. a surname)
    from partially matching inside a longer one before the longer one gets
    its turn.
  - The token <-> real-value mapping is a plain dict returned to the caller.
    It must be kept request-scoped (a local variable) and never persisted,
    logged, or sent anywhere, including inside AI prompts.
  - Nothing in this module ever logs the values it is given. Callers must
    not log raw `notes` or raw PII values either (see backend/main.py).
"""

from __future__ import annotations

import re
import secrets

# Token shape: [[PAT_<8 uppercase hex chars>]]. Cryptographically random via
# `secrets`, not derived from the name/phone/ID in any way.
_TOKEN_TEMPLATE = "[[PAT_{}]]"
_TOKEN_PATTERN = re.compile(r"\[\[PAT_[0-9A-Fa-f]+\]\]", re.IGNORECASE)

_NAME_TITLES = {"mr", "mrs", "ms", "mx", "dr", "master", "miss", "shri", "smt"}


class PIIViolationError(Exception):
    """
    Raised when unredacted PII is detected in text about to cross (or that
    has crossed) the local/external boundary. This is treated as a hard
    stop, not a warning: the caller must abort before making the network
    call, never attempt to patch the text and retry.
    """
    pass


def _new_token(existing: dict[str, str]) -> str:
    """Generate a fresh cryptographically random token, guaranteed unique
    within `existing`'s keys (collision is astronomically unlikely with
    32 bits of entropy, but we never silently reuse a token)."""
    while True:
        candidate = _TOKEN_TEMPLATE.format(secrets.token_hex(4).upper())
        if candidate not in existing:
            return candidate


def tokenize_payload(notes: str, pii_values: list[str]) -> tuple[str, dict[str, str]]:
    """
    Replace every occurrence of each value in `pii_values` inside `notes`
    with a unique, cryptographically random token, using case-insensitive
    matching. The same value maps to the same token everywhere it appears
    in `notes` (deterministic within this call).

    Values are processed longest-first so that e.g. "Priya Menon" is
    tokenized before a shorter value that might otherwise partially match
    inside it.

    Args:
        notes: the raw clinical note text (may contain PII).
        pii_values: known PII strings to redact (patient name, phone,
            patient ID, etc). Empty/None entries are ignored.

    Returns:
        (sanitized_notes, mapping) where `mapping` is {token: original_value}.
        `mapping` exists only for this call/request and must not be
        persisted or sent anywhere -- it is the only way to reverse a token
        back to real PII.
    """
    sanitized = notes

    # De-duplicate case-insensitively (keep first-seen casing as canonical),
    # drop empty/whitespace-only values -- an empty pattern would match
    # everywhere and corrupt the text.
    deduped: dict[str, str] = {}
    for value in pii_values:
        if not value:
            continue
        stripped = value.strip()
        if not stripped:
            continue
        key = stripped.lower()
        if key not in deduped:
            deduped[key] = stripped

    ordered_values = sorted(deduped.values(), key=len, reverse=True)

    mapping: dict[str, str] = {}
    # Tracks every value already processed (lowercased) -> the token it was
    # assigned, so a shorter *name variant* of an already-tokenized value
    # (e.g. "John" showing up after "John Mathew" was tokenized) reuses the
    # same token instead of minting a new, disconnected one. See
    # expand_name_variants() for the intended way callers supply variants.
    processed_lower_to_token: dict[str, str] = {}

    for original in ordered_values:
        original_lower = original.lower()
        reused_token = None
        for seen_lower, seen_token in processed_lower_to_token.items():
            if _is_name_variant(original_lower, seen_lower):
                reused_token = seen_token
                break

        if reused_token is not None:
            token = reused_token
        else:
            token = _new_token(mapping)
            mapping[token] = original

        processed_lower_to_token[original_lower] = token
        pattern = re.compile(re.escape(original), re.IGNORECASE)
        sanitized = pattern.sub(token, sanitized)

    return sanitized, mapping


def _is_name_variant(value_a_lower: str, value_b_lower: str) -> bool:
    """
    True if the two (already-lowercased) values are the same identity for
    tokenization purposes: identical, or one is a single whitespace-delimited
    word that appears as a whole word inside the other (e.g. "john" is a
    variant of "john mathew"). Deliberately narrow -- this is NOT a fuzzy
    or substring match (so "ann" is not treated as a variant of "anna"),
    to avoid accidentally collapsing two different people's tokens.
    """
    if value_a_lower == value_b_lower:
        return True
    a_words = value_a_lower.split()
    b_words = value_b_lower.split()
    if len(a_words) == 1 and a_words[0] in b_words:
        return True
    if len(b_words) == 1 and b_words[0] in a_words:
        return True
    return False


def expand_name_variants(full_name: str) -> list[str]:
    """
    Given a person's full name, return [full_name, *individual name parts]
    suitable for use as (part of) the `pii_values` passed to
    tokenize_payload, so that a later bare first-name or surname mention in
    the notes collapses to the SAME token as the full name.

    This is a small, explicitly-scoped helper for the common "Name" /
    "Name Surname" case. It is NOT a general PII/NER detector (see the
    module docstring: define PII scope explicitly, don't pretend to have a
    universal detector) -- it only ever looks at the one name string it is
    given. Title words (Mr/Mrs/Dr/...) and parts shorter than 3 characters
    (bare initials) are excluded, since those are common short strings that
    would otherwise get over-tokenized.
    """
    if not full_name or not full_name.strip():
        return []
    stripped = full_name.strip()
    variants = [stripped]
    for part in stripped.split():
        cleaned = part.strip(".,")
        if len(cleaned) < 3:
            continue
        if cleaned.lower() in _NAME_TITLES:
            continue
        variants.append(cleaned)
    return variants


def assert_no_pii_leak(sanitized_notes: str, pii_values: list[str]) -> None:
    """
    Pre-flight safety check. Call this immediately before the text is sent
    to an external AI service, on the exact text that will be sent.

    Raises PIIViolationError if any non-empty value in `pii_values` is still
    present in `sanitized_notes` (case-insensitive substring check). The
    error message intentionally does not include the leaked value itself,
    so that logging or displaying the exception can't leak PII a second
    time -- it only reports that a leak was found.

    Callers MUST treat a raised PIIViolationError as "do not call the AI
    service" -- never catch it, strip the offending text, and retry in the
    same request.
    """
    lowered = sanitized_notes.lower()
    leaked_count = 0
    for value in pii_values:
        if not value:
            continue
        stripped = value.strip()
        if not stripped:
            continue
        if stripped.lower() in lowered:
            leaked_count += 1

    if leaked_count:
        raise PIIViolationError(
            f"PII air-gap violation: {leaked_count} known PII value(s) still "
            "present in text staged for the external AI call. Aborting "
            "before the network request is made."
        )


def rehydrate_payload(ai_json: dict, mapping: dict[str, str]) -> dict:
    """
    Restore real PII into a structure returned by the AI service, replacing
    every token in `mapping` with its real value. Recurses through nested
    dicts and lists so a token is caught wherever it appears in the
    response, not just in one expected field. Token matching is
    case-insensitive to tolerate an AI service that alters token casing.

    `mapping` is consumed read-only and is not mutated or retained by this
    function.
    """
    # Build case-insensitive lookup once: token (any case) -> real value.
    lowered_map = {token.lower(): original for token, original in mapping.items()}

    def _rehydrate_token_case_insensitive(text: str) -> str:
        def _replace(match: re.Match) -> str:
            found = match.group(0)
            return lowered_map.get(found.lower(), found)

        return _TOKEN_PATTERN.sub(_replace, text)

    def _walk(value):
        if isinstance(value, str):
            return _rehydrate_token_case_insensitive(value)
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return _walk(ai_json)


def find_unresolved_tokens(value) -> list[str]:
    """
    Defense-in-depth scan: after rehydrate_payload, walk the result again
    and report any remaining `[[PAT_...]]`-shaped strings. A non-empty
    result means either the mapping didn't cover a token that appears in
    the response (the AI fabricated or mangled one), and callers should
    treat this as an anomaly -- do not persist the response as-is.
    """
    found: list[str] = []

    def _walk(v):
        if isinstance(v, str):
            found.extend(_TOKEN_PATTERN.findall(v))
        elif isinstance(v, dict):
            for item in v.values():
                _walk(item)
        elif isinstance(v, list):
            for item in v:
                _walk(item)

    _walk(value)
    return found


def contains_identity(text: str, pii_values: list) -> bool:
    """True if `text` contains any of `pii_values` (case-insensitive) as a
    whole word/token -- i.e. not merely as part of a longer word, so a short
    name part cannot false-positive inside a clinical term. Used to keep
    patient identity out of free-text clinical fields (dosha_imbalance) that
    are persisted into the audit trail and corrections dataset, which are
    identity-free by design. Callers pass the same identity values they
    tokenize (see expand_name_variants). Like the rest of this module, it
    only knows the identity it is given -- it is not a general PII detector.
    """
    if not text:
        return False
    for value in pii_values:
        if not value or not str(value).strip():
            continue
        pattern = r"(?<!\w)" + re.escape(str(value).strip()) + r"(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False
