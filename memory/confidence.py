"""
Calibrated candidate confidence for AI-2 v2 (Block 3): temperature scaling of the planner's joint scores.

    calibrated(scores, temperature=TEMPERATURE) -> probabilities over the compatible candidates
    fit_temperature(groups) -> temperature minimising the negative log-likelihood of the gold candidate
                               (groups: [(candidate scores, index of the gold candidate), ...])

The planner's joint scores (op + target + selector perceptron margins) are large, so softmax(score)
is saturated at 0 / 1 and badly calibrated. One temperature T is fitted on the DEVCAL DEV split only
(gold-query cases whose gold plan is among the compatible candidates), by a fixed log-spaced grid
search -- deterministic, no dependency. The calibration split is never used for fitting or choosing.

Fitted values (re-derived and checked by tests/test_v2_block3.py):
    TEMPERATURE  14.2109  (grid 0.5 * 1.25**k; NLL of the gold candidate on devcal dev 5.654 -> 0.974, n=84)
    THRESHOLD    0.02     the selective gate: a single consistent reading whose calibrated confidence is below
                          this is CONFIRMED (clarify) instead of answered. Chosen on dev data as the smallest
                          grid value that withholds the one wrong dev answer (hardneg dev); it also withholds
                          3 correct dev answers (as confirmations). Evidence is a single dev error.
"""
from __future__ import annotations

import math

TEMPERATURE = 14.2109
THRESHOLD = 0.02
GRID = tuple(0.5 * 1.25 ** k for k in range(40))


def calibrated(scores: list[float], temperature: float = TEMPERATURE) -> list[float]:
    if not scores:
        return []
    top = max(scores)
    exp = [math.exp((s - top) / temperature) for s in scores]
    total = sum(exp)
    return [e / total for e in exp]


def nll(groups: list[tuple[list[float], int]], temperature: float) -> float:
    return sum(-math.log(max(calibrated(s, temperature)[g], 1e-12)) for s, g in groups) / len(groups)


def fit_temperature(groups: list[tuple[list[float], int]]) -> float:
    return min(GRID, key=lambda t: (nll(groups, t), t))
