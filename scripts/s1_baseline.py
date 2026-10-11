#!/usr/bin/env python3
"""
Deterministic baseline for Notes -> Structured Prescription, and the
three-arm comparison (Deterministic Baseline vs Control vs A0).

    # baseline over a case file -> raw + graded artifacts (same schema and grader as the harness)
    ./venv/bin/python scripts/s1_baseline.py run --cases eval/benchmarks/s1/s1_test.json
    # Control / A0 on the same cases: the unchanged harness
    ./venv/bin/python scripts/clinical_eval_harness.py --arm control --selfcheck --cases eval/benchmarks/s1/s1_test.json
    # per-category / per-tag table across arms
    ./venv/bin/python scripts/s1_baseline.py compare --cases eval/benchmarks/s1/s1_test.json G1.json G2.json G3.json

NOT CLINICAL VALIDATION: S1 is synthetic. Results describe extraction on
generated text only.

THE BASELINE (extract) -- classical, rule-based, no model:
  - herbs: the production alias table (backend/herb_resolver.HERB_ALIASES),
    longest match first, with boundaries that also work for Malayalam
    script; then, for leftover tokens, Manglish suffix stripping
    (-yum/-vum/-um) and an edit distance of 1 (adjacent swaps count as 1)
    against single-word aliases of 5+ letters;
  - quantities: packs (n x m g, n packets of m g), kg/kilo, g/gm/gms/gram(s)
    and the Malayalam "ഗ്രാം", English number words before "grams"; anything
    else (days, years, hours, kg body weight beyond the 10 000 g cap) is not
    a dose;
  - pairing: within a clause (split on ; ! ? , newlines and . except in decimals), each herb
    takes the nearest unclaimed quantity, preferring the one that follows it;
  - negation/history: a cue within the herb's clause and close to it ("stop",
    "avoid", "do not", "venda", "nirth...", "previously", "munpu", ...) drops
    the herb, after it has claimed its quantity (so the quantity cannot move
    to another herb);
  - dosha: a transliteration table (Latin and Malayalam spellings) -> set.
It does NOT import the S1 generator (tests check this): its rules were written
against the formulary and general phrasing, not against generator internals.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import statistics
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.herb_resolver import HERB_ALIASES  # noqa: E402
from backend.schemas import ClinicalParse  # noqa: E402

HARNESS_PATH = pathlib.Path(ROOT) / "scripts" / "clinical_eval_harness.py"
ARM = "baseline"
GRAMS_MAX = 10000
CANONICAL_DOSHAS = ("Vata", "Pitta", "Kapha")

_ML = "ഀ-ൿ"
_WORD = rf"[\w{_ML}]"

DOSHA_WORDS = {
    "vata": "Vata", "vaata": "Vata", "vatha": "Vata", "vaatha": "Vata", "vatham": "Vata", "vaatham": "Vata",
    "വാതം": "Vata", "വാത": "Vata",
    "pitta": "Pitta", "pita": "Pitta", "pitha": "Pitta", "pittha": "Pitta", "pitham": "Pitta",
    "pittham": "Pitta", "പിത്തം": "Pitta", "പിത്ത": "Pitta",
    "kapha": "Kapha", "kafa": "Kapha", "kapham": "Kapha", "kafam": "Kapha", "കഫം": "Kapha", "കഫ": "Kapha",
}
NEGATION_CUES = ("stop", "stopped", "discontinue", "discontinued", "avoid", "do not", "don't", "not needed",
                 "no more", "venda", "vendaa", "nirthanam", "nirthi", "nirthuka", "kodukkenda",
                 "previously", "earlier", "was on", "last month", "last year", "munpu", "mumbu",
                 "kazhichirunnu", "eduthirunnu", "kazhinja")
CUE_WINDOW_CHARS = 30
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
                "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

_GRAM_UNIT = r"(?:g|gm|gms|gr|gram|grams|ഗ്രാം)"
_PACK_RE = re.compile(rf"(\d+)\s*(?:x|×|packets?\s+of|packs?\s+of)\s*(\d+)\s*{_GRAM_UNIT}(?![a-z])")
_KG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kg|kgs|kilo|kilos|kilogram|kilograms)(?![a-z])")
_GRAM_RE = re.compile(rf"(\d+)\s*{_GRAM_UNIT}(?![a-z])")
_WORDS_RE = re.compile(r"((?:[a-z]+[\s-])*?[a-z]+)\s+(?:grams?|gms?)(?![a-z])")
_CLAUSE_SPLIT = re.compile(r"\.(?!\d)|[;!?,\n]")  # a "." before a digit is a decimal point


def _alias_patterns():
    aliases = sorted(HERB_ALIASES.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    return [(re.compile(rf"(?<!{_WORD}){re.escape(alias)}(?!{_WORD})"), canonical) for alias, canonical in aliases]


_ALIAS_PATTERNS = _alias_patterns()
_FUZZY_ALIASES = sorted({a: c for a, c in HERB_ALIASES.items()
                         if a.isascii() and a.isalpha() and len(a) >= 5}.items())


def _osa_distance_at_most_one(a: str, b: str) -> bool:
    """Optimal string alignment distance <= 1 (one insert/delete/substitute or one adjacent swap)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        return len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return True
    return False


def _resolve_token(token: str):
    if token in HERB_ALIASES:
        return HERB_ALIASES[token]
    for suffix in ("yum", "vum", "um"):
        if token.endswith(suffix) and token[: -len(suffix)] in HERB_ALIASES:
            return HERB_ALIASES[token[: -len(suffix)]]
    if token.isascii() and token.isalpha() and len(token) >= 5:
        matches = sorted({c for alias, c in _FUZZY_ALIASES if _osa_distance_at_most_one(token, alias)})
        if len(matches) == 1:
            return matches[0]
    return None


def _number_from_words(text: str):
    words = text.replace("-", " ").split()
    if not words or any(w not in NUMBER_WORDS and w != "hundred" for w in words):
        return None
    total, current = 0, 0
    for w in words:
        if w == "hundred":
            current = (current or 1) * 100
        else:
            current += NUMBER_WORDS[w]
    return total + current


def _find_quantities(clause: str) -> list[tuple[int, int, int]]:
    """[(start, end, grams)] without overlaps; most specific patterns first."""
    taken = [False] * len(clause)
    found = []

    def claim(start, end, grams):
        if any(taken[start:end]) or not 0 < grams <= GRAMS_MAX:
            return
        for i in range(start, end):
            taken[i] = True
        found.append((start, end, grams))

    for m in _PACK_RE.finditer(clause):
        claim(m.start(), m.end(), int(m.group(1)) * int(m.group(2)))
    for m in _KG_RE.finditer(clause):
        grams = float(m.group(1)) * 1000
        if grams == int(grams):
            claim(m.start(), m.end(), int(grams))
    for m in _GRAM_RE.finditer(clause):
        claim(m.start(), m.end(), int(m.group(1)))
    for m in _WORDS_RE.finditer(clause):
        # shortest suffix of the matched words that parses as a number
        words = m.group(1).replace("-", " ").split()
        for i in range(len(words)):
            value = _number_from_words(" ".join(words[i:]))
            if value:
                offset = m.group(0).find(words[i])
                claim(m.start() + offset, m.end(), value)
                break
    return sorted(found)


def _find_herbs(clause: str) -> list[tuple[int, int, str]]:
    taken = [False] * len(clause)
    found = []
    for pattern, canonical in _ALIAS_PATTERNS:
        for m in pattern.finditer(clause):
            if not any(taken[m.start():m.end()]):
                for i in range(m.start(), m.end()):
                    taken[i] = True
                found.append((m.start(), m.end(), canonical))
    for m in re.finditer(rf"{_WORD}+", clause):
        if any(taken[m.start():m.end()]):
            continue
        canonical = _resolve_token(m.group(0))
        if canonical:
            found.append((m.start(), m.end(), canonical))
    return sorted(found)


def _negated(clause: str, start: int, end: int) -> bool:
    window = clause[max(0, start - CUE_WINDOW_CHARS): end + CUE_WINDOW_CHARS]
    return any(re.search(rf"(?<![a-z]){re.escape(cue)}(?![a-z])", window) for cue in NEGATION_CUES)


def _dosha(text: str) -> str:
    found = {DOSHA_WORDS[w] for w in re.findall(rf"[a-z{_ML}]+", text) if w in DOSHA_WORDS}
    return "-".join(d for d in CANONICAL_DOSHAS if d in found)


def extract(notes: str) -> ClinicalParse:
    """Deterministic: the same notes always give the same ClinicalParse."""
    text = notes.lower()
    herbs: dict[str, int] = {}
    for clause in _CLAUSE_SPLIT.split(text):
        mentions = _find_herbs(clause)
        quantities = _find_quantities(clause)
        claimed = set()
        for start, end, canonical in mentions:
            best = None
            for index, (q_start, q_end, grams) in enumerate(quantities):
                if index in claimed:
                    continue
                gap = q_start - end if q_start >= end else start - q_end
                key = (gap, 0 if q_start >= end else 1)  # prefer the following quantity on ties
                if best is None or key < best[0]:
                    best = (key, index, grams)
            if best is None:
                continue
            claimed.add(best[1])
            if _negated(clause, start, end) or canonical in herbs:
                continue
            herbs[canonical] = best[2]
    return ClinicalParse(dosha_imbalance=_dosha(text),
                         prescribed_herbs=[{"name": n, "grams": g} for n, g in herbs.items()])


# --- runner: raw + graded artifacts, graded by the harness's own grader ----------

def _harness():
    spec = importlib.util.spec_from_file_location("clinical_eval_harness", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_file(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def run(case_file, output_dir=None) -> dict:
    """Baseline over a case file. Writes <stamp>_baseline_deterministic_{raw,graded}.json
    in the harness's raw/graded schema and returns the graded report."""
    h = _harness()
    from backend.pii_sanitizer import assert_no_pii_leak, tokenize_payload

    case_file = pathlib.Path(case_file)
    case_data = h.load_case_file(case_file)
    output_dir = pathlib.Path(output_dir or h.OUTPUT_DIR)
    records = []
    for case in case_data["cases"]:
        pii_values = ["Test Patient"]  # same sanitisation step the harness applies to Control/A0
        sanitized, _ = tokenize_payload(case["notes"], pii_values)
        assert_no_pii_leak(sanitized, pii_values)
        record = {"case_id": case["case_id"], "repeat": 0, "arm": ARM,
                  "notes_sha256": hashlib.sha256(case["notes"].encode("utf-8")).hexdigest(),
                  "status": None, "parsed": None, "exception": None}
        started = time.monotonic()
        try:
            record["parsed"] = extract(sanitized).model_dump()
            record["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - kept in the raw record, as the harness does
            record["status"] = "exception"
            record["exception"] = {"type": type(exc).__name__, "message": str(exc), "chain": []}
        record["latency_ms"] = int((time.monotonic() - started) * 1000)
        record["tool_calls"] = []
        record["log_records"] = []
        records.append(record)

    metadata = {
        "arm": ARM, "mode": "deterministic", "parser": "scripts/s1_baseline.py::extract",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "case_file": str(case_file), "case_file_sha256": h.sha256_file(case_file),
        "case_schema_version": case_data["schema_version"], "corpus_id": case_data.get("corpus_id"),
        "evaluation_class": case_data.get("evaluation_class"),
        "baseline_sha256": _sha256_file(__file__), "harness_sha256": h.sha256_file(HARNESS_PATH),
        "herb_resolver_sha256": h.sha256_file(h.HERB_RESOLVER_PATH), "repeats": 1,
    }
    raw_report = {"schema_version": h.RAW_SCHEMA_VERSION, "metadata": metadata, "records": records}
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    raw_path = output_dir / f"{stamp}_{ARM}_deterministic_raw.json"
    graded_path = output_dir / f"{stamp}_{ARM}_deterministic_graded.json"
    raw_path.write_text(json.dumps(raw_report, indent=2, ensure_ascii=False), encoding="utf-8")
    graded = h.build_graded_report(raw_report, case_data, str(raw_path))
    graded_path.write_text(json.dumps(graded, indent=2, ensure_ascii=False), encoding="utf-8")
    graded["_paths"] = {"raw": str(raw_path), "graded": str(graded_path)}
    return graded


# --- comparison --------------------------------------------------------------------

def _slice_metrics(results: list[dict]) -> dict:
    graded = [r for r in results if r["counts_as_clinical_grade"]]
    tp = sum(r["herbs"]["tp"] for r in graded)
    fp = sum(r["herbs"]["fp"] for r in graded)
    fn = sum(r["herbs"]["fn"] for r in graded)
    quantities = [q for r in graded for q in r["herbs"]["quantities"].values()]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else (0.0 if graded else None)
    return {
        "attempts": len(results),
        "errors": sum(1 for r in results if r["error_category"]),
        "pass_rate": (sum(1 for r in graded if r["outcome"] == "PASS") / len(graded)) if graded else None,
        "dosha_exact_rate": (sum(1 for r in graded if r["dosha"]["exact"]) / len(graded)) if graded else None,
        "herb_precision": precision, "herb_recall": recall, "herb_f1": f1,
        "quantity_exact_rate": (sum(1 for q in quantities if q["within_tolerance"]) / len(quantities))
        if quantities else None,
        "unresolved_herb_attempts": sum(1 for r in results if r["hallucination"]
                                        and r["hallucination"]["unresolved_herbs"]),
        "mean_doctor_edits": statistics.fmean(r["doctor_edits"]["total"] for r in graded) if graded else None,
    }


def compare(case_file, graded_paths) -> dict:
    """{arm: {"overall": metrics, "by_category": {...}, "by_tag": {...}}}. Refuses graded
    reports produced from a different case file (sha256 mismatch)."""
    h = _harness()
    case_file = pathlib.Path(case_file)
    case_sha = h.sha256_file(case_file)
    cases = {c["case_id"]: c for c in h.load_case_file(case_file)["cases"]}
    table = {}
    for path in graded_paths:
        report = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        if report["metadata"]["case_file_sha256"] != case_sha:
            raise ValueError(f"{path} was graded against a different case file")
        arm = report["metadata"]["arm"]
        if arm in table:
            raise ValueError(f"two reports for arm {arm!r}")
        results = report["results"]
        by_category, by_tag = {}, {}
        for r in results:
            case = cases[r["case_id"]]
            by_category.setdefault(case.get("label"), []).append(r)
            for tag in case.get("tags", []):
                by_tag.setdefault(tag, []).append(r)
        table[arm] = {
            "mode": report["metadata"].get("mode"),
            "overall": _slice_metrics(results),
            "by_category": {k: _slice_metrics(v) for k, v in sorted(by_category.items())},
            "by_tag": {k: _slice_metrics(v) for k, v in sorted(by_tag.items())},
        }
    return table


def _fmt(value) -> str:
    return "  -  " if value is None else f"{value:.3f}"


def _print_comparison(table: dict) -> None:
    print("NOT CLINICAL VALIDATION -- synthetic benchmark; technical evaluation of extraction only.")
    arms = list(table)
    print(f"{'slice':<28}" + "".join(f"{arm + ' F1/pass':>22}" for arm in arms))
    slices = ["overall"] + [f"category:{c}" for c in table[arms[0]]["by_category"]] + \
             [f"tag:{t}" for t in table[arms[0]]["by_tag"]]
    for name in slices:
        row = f"{name:<28}"
        for arm in arms:
            if name == "overall":
                m = table[arm]["overall"]
            elif name.startswith("category:"):
                m = table[arm]["by_category"].get(name[9:])
            else:
                m = table[arm]["by_tag"].get(name[4:])
            row += f"{(_fmt(m['herb_f1']) + ' / ' + _fmt(m['pass_rate'])) if m else '-':>22}"
        print(row)
    for arm in arms:
        if table[arm]["mode"] == "selfcheck":
            print(f"NOTE: {arm} results are from --selfcheck (scripted responses), not a model.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--cases", required=True)
    p_run.add_argument("--output-dir", default=None)
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("--cases", required=True)
    p_cmp.add_argument("--json", default=None, help="also write the table as JSON")
    p_cmp.add_argument("graded", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "run":
        graded = run(args.cases, args.output_dir)
        c = graded["summary"]["clinical"]
        print(f"baseline: pass {c['passed']}/{c['graded_attempts']}  herb P/R "
              f"{c['herb_micro_precision']}/{c['herb_micro_recall']}  dosha exact {c['dosha_exact']}")
        print(f"  {graded['summary']['disclaimer']}")
        print(f"  raw    : {graded['_paths']['raw']}\n  graded : {graded['_paths']['graded']}")
        return 0
    table = compare(args.cases, args.graded)
    _print_comparison(table)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(table, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
