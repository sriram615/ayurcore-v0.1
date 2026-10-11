#!/usr/bin/env python3
"""
API cost for an evaluation run -- computed from RECORDED usage only, kept
separate from grading (scripts/clinical_eval_harness.py writes it as its own
<stamp>_<arm>_<mode>_cost.json; graded reports never contain cost).

    ./venv/bin/python scripts/eval_cost.py RAW_JSON [--prices eval/pricing/gemini_prices.json]

The Gemini API reports token usage (usage_metadata) but not prices or cost.
Cost is therefore computed only when ALL of these hold, and otherwise
reported as "unavailable" with the reason -- never estimated silently:
  1. the price file has an entry for the arm's configured model, with numeric
     USD-per-1M-token prices, a source_url and a retrieved_on date (prices are
     never built into this code);
  2. every API call of a record reported usage_metadata with
     prompt_token_count and total_token_count;
  3. the reported counts are internally consistent:
     prompt + candidates + thoughts + tool_use_prompt == total
     (fields the API omits are zero -- the API leaves out zero counts);
  4. any model_version the API returned starts with the configured model name;
  5. cached tokens, if any, have a configured cached price.
Billing rules are explicit per model in the price file: whether thinking
tokens are billed as output and tool-use prompt tokens as input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PRICES = ROOT / "eval" / "pricing" / "gemini_prices.json"
REQUIRED_PRICE_KEYS = ("input_usd_per_1m_tokens", "output_usd_per_1m_tokens", "thoughts_billed_as_output",
                       "tool_use_prompt_billed_as_input", "source_url", "retrieved_on")
COST_SCHEMA_VERSION = "ayurcore.clinical_eval.cost/v1"


def load_prices(path=DEFAULT_PRICES) -> tuple[dict | None, str | None, str | None]:
    """(config, sha256, problem)."""
    path = pathlib.Path(path)
    if not path.is_file():
        return None, None, f"price file {path} not found"
    raw = path.read_bytes()
    try:
        return json.loads(raw), hashlib.sha256(raw).hexdigest(), None
    except json.JSONDecodeError as exc:
        return None, hashlib.sha256(raw).hexdigest(), f"price file is not valid JSON: {exc}"


def _price_entry(prices: dict | None, model: str | None) -> tuple[dict | None, str | None]:
    if prices is None:
        return None, "no price configuration"
    if not model:
        return None, "no configured model recorded for this arm"
    entry = (prices.get("models") or {}).get(model)
    if entry is None:
        return None, f"no price configured for model {model!r}"
    missing = [k for k in REQUIRED_PRICE_KEYS if entry.get(k) in (None, "")]
    if missing:
        return None, f"price entry for {model!r} lacks {missing}"
    for key in ("input_usd_per_1m_tokens", "output_usd_per_1m_tokens"):
        value = entry[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None, f"price entry for {model!r}: {key} must be a non-negative number"
    return entry, None


def _call_cost(call: dict, model: str, entry: dict) -> tuple[float | None, str | None]:
    if call.get("failed"):
        return 0.0, None  # a request that raised returned no usage; recorded as a failed call, not priced
    if not call.get("usage_metadata_present"):
        return None, "an API call reported no usage_metadata"
    prompt, total = call.get("prompt_token_count"), call.get("total_token_count")
    if prompt is None or total is None:
        return None, "an API call did not report prompt_token_count and total_token_count"
    candidates = call.get("candidates_token_count") or 0
    thoughts = call.get("thoughts_token_count") or 0
    tool_use = call.get("tool_use_prompt_token_count") or 0
    cached = call.get("cached_content_token_count") or 0
    if prompt + candidates + thoughts + tool_use != total:
        return None, "reported token counts are inconsistent with total_token_count"
    version = call.get("model_version")
    if version and not version.startswith(model):
        return None, f"API reported model_version {version!r}, not the priced model {model!r}"
    input_tokens = prompt + (tool_use if entry["tool_use_prompt_billed_as_input"] else 0)
    output_tokens = candidates + (thoughts if entry["thoughts_billed_as_output"] else 0)
    cached_cost = 0.0
    if cached:
        cached_price = entry.get("cached_input_usd_per_1m_tokens")
        if cached_price is None:
            return None, "cached tokens reported but no cached_input_usd_per_1m_tokens configured"
        input_tokens -= cached
        cached_cost = cached * cached_price / 1_000_000
    cost = (input_tokens * entry["input_usd_per_1m_tokens"]
            + output_tokens * entry["output_usd_per_1m_tokens"]) / 1_000_000 + cached_cost
    return cost, None


def cost_report_for(raw_report: dict, raw_path: str | None = None, prices_path=DEFAULT_PRICES) -> dict:
    metadata = raw_report.get("metadata", {})
    arm, mode = metadata.get("arm"), metadata.get("mode")
    model = ((metadata.get("settings") or {}).get(arm) or {}).get("gemini_model")
    prices, prices_sha, price_problem = load_prices(prices_path)
    entry, entry_problem = _price_entry(prices, model) if price_problem is None else (None, price_problem)

    records, totals = [], {}
    for record in raw_report.get("records", []):
        usage = record.get("usage")
        row = {"case_id": record.get("case_id"), "repeat": record.get("repeat"),
               "latency_ms": record.get("latency_ms"), "usage_status": (usage or {}).get("status"),
               "api_calls": (usage or {}).get("api_calls"), "tokens": (usage or {}).get("totals"),
               "cost_usd": None, "cost_status": None, "reason": None}
        if usage is None or usage.get("status") == "not_applicable_selfcheck":
            row["cost_status"], row["reason"] = "not_applicable", "no billed API calls (deterministic or scripted)"
        elif usage.get("status") not in ("recorded",):
            row["cost_status"], row["reason"] = "unavailable", f"usage status {usage.get('status')!r}"
        elif entry is None:
            row["cost_status"], row["reason"] = "unavailable", entry_problem
        else:
            cost, problem = 0.0, None
            for call in usage["calls"]:
                call_cost, problem = _call_cost(call, model, entry)
                if problem:
                    break
                cost += call_cost
            row["cost_status"], row["reason"] = ("unavailable", problem) if problem else ("computed", None)
            row["cost_usd"] = None if problem else round(cost, 8)
        for field, value in (row["tokens"] or {}).items():
            if value is not None:
                totals[field] = totals.get(field, 0) + value
        records.append(row)

    statuses = {r["cost_status"] for r in records}
    if not records or statuses == {"not_applicable"}:
        overall = "not_applicable"
    elif statuses == {"computed"}:
        overall = "computed"
    elif "computed" in statuses:
        overall = "partial"
    else:
        overall = "unavailable"
    computed = [r["cost_usd"] for r in records if r["cost_status"] == "computed"]
    return {
        "schema_version": COST_SCHEMA_VERSION,
        "raw_artifact": raw_path,
        "arm": arm, "mode": mode, "model": model, "provider": metadata.get("provider"),
        "price_file": os.path.relpath(prices_path, ROOT) if pathlib.Path(prices_path).is_absolute() else str(prices_path),
        "price_file_sha256": prices_sha,
        "price_entry": entry,
        "summary": {
            "cost_status": overall,
            "reason": None if overall in ("computed", "not_applicable") else
                      sorted({r["reason"] for r in records if r["reason"] and r["cost_status"] == "unavailable"}),
            # A total only when every record was priced; a partial sum is labelled as such.
            "total_cost_usd": round(sum(computed), 8) if overall == "computed" else None,
            "priced_records_subtotal_usd": round(sum(computed), 8) if overall == "partial" else None,
            "records": len(records),
            "records_computed": sum(1 for r in records if r["cost_status"] == "computed"),
            "usage_totals": totals or None,
            "api_calls": sum(r["api_calls"] or 0 for r in records),
        },
        "records": records,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("raw")
    parser.add_argument("--prices", default=str(DEFAULT_PRICES))
    args = parser.parse_args(argv)
    raw_path = pathlib.Path(args.raw)
    report = cost_report_for(json.loads(raw_path.read_text(encoding="utf-8")), str(raw_path), pathlib.Path(args.prices))
    out = raw_path.with_name(raw_path.name.replace("_raw.json", "_cost_recomputed.json"))
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=1))
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
