"""
AI-3 evaluation: template baseline vs learned salience ranker on frozen synthetic patients (evaluation only).

SYNTHETIC DATA, SYNTHETIC GOLD (ai3.synthetic.reference_policy, same-author). Not clinical validation; see ai3/synthetic.py.

Per patient (a patient with no verifiable fact is skipped) and per arm, with a budget of K facts:
    recall      selected reference-salient facts / min(number of reference-salient facts, K)
    precision   selected reference-salient facts / selected facts
    critical    for the patients that have a CRITICAL fact (a dosha change, an open visit, unknown data on the latest prescription):
                the fraction of those critical facts the brief shows (a miss is a critical fact that was not shown; "unknown data" counts
                as shown when at least one unknown-data fact is shown)
    support     every shown fact is re-verified (AI-2 verifier + displayed-sentence check); anything unsupported is counted
Arms: "template" (fixed standard order), "learned" (the model), "reference" (orders the reference-salient facts first: a ceiling that
uses the labels, shown only to bound the scale of the numbers).
Intervals are percentile bootstrap (2000 resamples, fixed seed) over patients; the arm difference is paired.
"""
from __future__ import annotations

import random
import statistics
from typing import Optional

from ai3.brief import MAX_ITEMS, TEMPLATE_ORDER, template_order, verified_facts
from ai3.facts import Fact, fact_supported
from ai3.salience import SalienceModel, context, rank
from ai3.synthetic import critical_present, critical_selected, reference_policy

ARMS = ("template", "learned", "reference")


def select(arm: str, facts: list[Fact], ctx: dict, model: Optional[SalienceModel], k: int) -> list[Fact]:
    if arm == "template":
        ordered = template_order(facts)
    elif arm == "learned":
        ordered = [f for f, _ in rank(model, facts, ctx)]
    else:
        position = {kind: i for i, kind in enumerate(TEMPLATE_ORDER)}
        ordered = sorted(facts, key=lambda f: (not reference_policy(f.kind, ctx), position[f.kind], f.text))
    return ordered[:k]


def evaluate(cases, model: SalienceModel, k: int = MAX_ITEMS) -> dict:
    rows = []
    unsupported = {a: 0 for a in ARMS}
    shown_total = {a: 0 for a in ARMS}
    for snap, as_of in cases:
        facts, rejected = verified_facts(snap)
        if not facts:
            continue
        ctx = context(snap, as_of, facts)
        salient = [f for f in facts if reference_policy(f.kind, ctx)]
        present = critical_present(facts, ctx)
        row = {"critical_present": len(present)}
        for arm in ARMS:
            chosen = select(arm, facts, ctx, model, k)
            hit = sum(1 for f in chosen if reference_policy(f.kind, ctx))
            row[arm] = {"recall": hit / min(len(salient), k) if salient else 1.0, "precision": hit / len(chosen),
                        "critical_shown": len(critical_selected({f.kind for f in chosen}, present))}
            for f in chosen:
                shown_total[arm] += 1
                if not fact_supported(snap, f)[0]:
                    unsupported[arm] += 1
        rows.append(row)
    n = len(rows)
    with_critical = [r for r in rows if r["critical_present"]]

    def mean(arm, key, subset=rows):
        return statistics.fmean(r[arm][key] for r in subset) if subset else None

    rng = random.Random(20261104)

    def boot(values: list[float]) -> list[float]:
        if not values:
            return [None, None]
        means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(2000))
        return [round(means[49], 4), round(means[1949], 4)]

    out = {"patients_with_facts": n, "k": k, "patients_with_critical_fact": len(with_critical), "arms": {}, "paired_learned_minus_template": {}}
    for arm in ARMS:
        crit_total = sum(r["critical_present"] for r in with_critical)
        crit_shown = sum(r[arm]["critical_shown"] for r in with_critical)
        out["arms"][arm] = {
            "recall": round(mean(arm, "recall"), 4), "recall_ci95": boot([r[arm]["recall"] for r in rows]),
            "precision": round(mean(arm, "precision"), 4), "precision_ci95": boot([r[arm]["precision"] for r in rows]),
            "critical_facts_present": crit_total, "critical_facts_shown": crit_shown,
            "critical_miss_rate": round(1 - crit_shown / crit_total, 4) if crit_total else None,
            "shown_facts": shown_total[arm], "unsupported_shown_facts": unsupported[arm]}
    for key in ("recall", "precision"):
        diffs = [r["learned"][key] - r["template"][key] for r in rows]
        out["paired_learned_minus_template"][key] = {"mean": round(statistics.fmean(diffs), 4), "ci95": boot(diffs)}
    crit_diffs = [(r["learned"]["critical_shown"] - r["template"]["critical_shown"]) / r["critical_present"] for r in with_critical]
    out["paired_learned_minus_template"]["critical_share_shown"] = {"mean": round(statistics.fmean(crit_diffs), 4) if crit_diffs else None, "ci95": boot(crit_diffs)}
    return out
