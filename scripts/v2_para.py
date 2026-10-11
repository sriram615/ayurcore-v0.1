#!/usr/bin/env python3
"""
AI-2 v2 Block 4 paraphrase / morphology benchmark (PARA v1). Written and frozen in Block 4A, BEFORE any
vocabulary or code change of Block 4B.

    ./venv/bin/python scripts/v2_para.py check | build | verify

Splits (fixed per group, a minimal-pair group never straddles splits):
    dev     for building Block 4B (may be inspected and run freely)
    accept  FROZEN acceptance split: evaluated ONCE by Block 4B for its accept / reject decision; nobody runs
            the pipeline on it before that. Its wording deliberately differs from dev (other synonyms and
            word forms of the same classes) so that 4B cannot pass by listing dev words.
Each GROUP targets one failure class found in the Block 4 investigation and holds answerable questions,
near-identical questions that must be refused (minimal-pair negatives) and / or ambiguous questions:
    defect_on            clause-initial "on" read as a prescription verb
    defect_most_recent   "most recently" read as the unsupported superlative "most"
    defect_count_noun    Malayalam count noun (എണ്ണം) read as an obligation ending
    defect_in_total      "in total" with a count read as an amount without a herb
    defect_day           "which day" (divasam) read as a duration
    synonym_change / synonym_recent / synonym_rx / synonym_current   English words outside the lexicons
    morph_manglish / morph_malayalam   inflected forms (case endings, clitics) of known words
    ambiguity            questions with two legitimate readings
    slot                 numbers that name a pair ("two latest")
    control              plain questions Block 2A already handles (regression check)
Gold: query (frame; gold answer from the executor on the S2 reference snapshot), ambiguous (readings),
abstain (reason + evaluation category). English, Manglish, Malayalam. Independence (enforced): no question
equals or near-duplicates (Jaccard >= 0.6) any S3 split, S4, S5, S6, devcal, hardneg, or any string in the
memory / v2 tests and evaluators; no accept question near-duplicates a dev question.
SYNTHETIC; NOT CLINICAL DATA. Not S7.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "eval" / "benchmarks" / "v2_para"
DATA_FILE = OUT_DIR / "para_v1.json"
MANIFEST_FILE = OUT_DIR / "manifest.json"
DATASET_ID = "para_v1"
NEG = {"CLIN": ("clinical", "TREATMENT"), "OTHER": ("other_patient", "OTHER_PATIENT"),
       "UNAV": ("unavailable", "UNAVAILABLE"), "UNSUP": ("unsupported", "AMBIGUOUS")}

# (split, class, language, fixed_herb, [(kind, gold, template), ...])
#   kind Q: gold = frame;  AMB: gold = [frames];  NEG: gold = CLIN | OTHER | UNAV | UNSUP
#   placeholders {h} herb, {hl} lower-case herb, {hm} Malayalam herb name, {n} number
GROUPS = [
    # ------------------------------------------------------------------ dev
    ("dev", "defect_on", "en", None, [
        ("Q", "FIRST_ENC", "On what date was her very first consultation?"),
        ("Q", "LAST_ENC", "On which day was her latest consultation?"),
        ("Q", "LAST_RX", "Which herbs is she currently on?"),
        ("NEG", "CLIN", "On what date should she come back?")]),
    ("dev", "defect_most_recent", "en", None, [
        ("Q", "LAST_HERB", "When was {h} most recently given to her?"),
        ("Q", "LAST_ENC", "When was her most recently recorded consultation?"),
        ("NEG", "UNSUP", "Which herb was given to her most often?")]),
    ("dev", "defect_count_noun", "ml", "Brahmi", [
        ("Q", "COUNT_RX", "ഇവരുടെ പേരിലുള്ള മുഴുവൻ കുറിപ്പടികളുടെയും എണ്ണം പറയൂ"),
        ("Q", "COUNT_ENC", "സന്ദർശനങ്ങളുടെ ആകെ എണ്ണം?"),
        ("NEG", "CLIN", "ബ്രഹ്മി തുടർന്ന് കൊടുക്കണോ?")]),
    ("dev", "defect_in_total", "en", None, [
        ("Q", "COUNT_RX", "How many prescriptions has she had in total?"),
        ("Q", "COUNT_ENC", "Counting everything, how many visits has she made in total?"),
        ("NEG", "OTHER", "How many prescriptions has the clinic issued in total?")]),
    ("dev", "defect_day", "manglish", None, [
        ("Q", "LAST_ENC", "avasaanam vannathu ethu divasam?"),
        ("AMB", ["FIRST_HERB", "LAST_HERB"], "{hl} ethu divasam koduthu?"),
        ("NEG", "UNAV", "{hl} kazhikkenda divasangal ethra?")]),
    ("dev", "synonym_change", "en", None, [
        ("Q", "CMP_HERB_FIRST", "Compared with her first prescription, has the {h} amount climbed?"),
        ("Q", "CMP_HERB_PREV", "Versus the prior prescription, did the {h} amount drop?"),
        ("NEG", "CLIN", "Should the {h} amount climb further?")]),
    ("dev", "synonym_recent", "en", None, [
        ("Q", "LAST_RX", "Show me her newest prescription."),
        ("Q", "LAST_DOSHA", "What dosha does her newest prescription record?"),
        ("NEG", "OTHER", "Show the newest patient's prescription.")]),
    ("dev", "synonym_rx", "en", None, [
        ("Q", "COUNT_RX", "How many scripts has she been given?"),
        ("Q", "LIST_RX", "List her scripts."),
        ("NEG", "OTHER", "List the other patients' scripts.")]),
    ("dev", "morph_manglish", "manglish", None, [
        ("Q", "LIST_DOSHA", "ororo kurippadiyileyum dosham parayu"),
        ("Q", "COUNT_ENC", "ivar ithuvare ethra pravashyam clinicil ethi?"),
        ("NEG", "OTHER", "ororo rogiyudeyum dosham parayu")]),
    ("dev", "morph_manglish", "manglish", None, [
        ("AMB", ["SUM_HERB", "COUNT_HERB"], "{hl} ithuvare ethrayaa koduthathu?"),
        ("Q", "COUNT_HERB", "{hl} ulla kurippadikalude ennam?"),
        ("NEG", "CLIN", "{hl} ini ethrayaa kodukkendathu?")]),
    ("dev", "morph_malayalam", "ml", "Pippali", [
        ("Q", "COUNT_HERB", "തിപ്പലിയുള്ള കുറിപ്പടികൾ എത്ര?"),
        ("Q", "SUM_HERB", "ഇവർക്ക് ഇതുവരെ കിട്ടിയ തിപ്പലി ഗ്രാമിൽ?"),
        ("NEG", "OTHER", "മറ്റു രോഗികൾക്കുള്ള തിപ്പലി എത്ര?")]),
    ("dev", "synonym_current", "en", None, [
        ("Q", "LAST_RX", "Which medicines is she taking these days?"),
        ("Q", "LAST_DOSHA", "What is her dosha these days?"),
        ("NEG", "CLIN", "Which medicines should she be taking these days?")]),
    ("dev", "ambiguity", "en", None, [
        ("AMB", ["SUM_HERB", "LAST_HERB"], "{h} quantity?"),
        ("AMB", ["CMP_HERB_PREV", "CMP_HERB_FIRST"], "Did {h} go up?"),
        ("AMB", ["DIFF_PREV", "DIFF_FIRST"], "What is different now?")]),
    ("dev", "slot", "en", None, [
        ("Q", "CMP_HERB_PREV", "Between her two latest prescriptions, did {h} change?"),
        ("Q", "DIFF_PREV", "Compare her two latest prescriptions."),
        ("NEG", "OTHER", "Compare her two latest prescriptions with her husband's.")]),
    ("dev", "control", "en", None, [
        ("Q", "COUNT_RX", "Count the prescriptions recorded so far."),
        ("Q", "SUM_HERB", "Grams of {h} summed over all her prescriptions?"),
        ("Q", "FIRST_DOSHA", "First prescription: which dosha was written?")]),
    # ------------------------------------------------------------------ accept (frozen; different wording)
    ("accept", "defect_on", "en", None, [
        ("Q", "FIRST_ENC", "On which date was she first seen here?"),
        ("Q", "LAST_ENC", "On what date did her last consultation happen?"),
        ("Q", "FIRST_RX", "On her first visit, what was prescribed?"),
        ("NEG", "CLIN", "On which day should she stop the medicines?")]),
    ("accept", "defect_most_recent", "en", None, [
        ("Q", "LAST_DOSHA", "Most recently, which dosha got noted?"),
        ("Q", "LAST_HERB", "When was {h} given to her most recently, and how much?"),
        ("NEG", "UNSUP", "Most frequently, which dosha got noted?")]),
    ("accept", "defect_count_noun", "ml", "Brahmi", [
        ("Q", "COUNT_HERB", "ബ്രഹ്മി ഉള്ള കുറിപ്പടികളുടെ എണ്ണം പറയൂ"),
        ("Q", "COUNT_ENC", "ഇവർ വന്ന തവണകളുടെ എണ്ണം?"),
        ("NEG", "CLIN", "ബ്രഹ്മി നിർത്താമോ?")]),
    ("accept", "defect_in_total", "en", None, [
        ("Q", "COUNT_HERB", "{h} is in how many prescriptions in total?"),
        ("Q", "COUNT_ENC", "Her consultations, how many in total?"),
        ("NEG", "OTHER", "{h} went to how many patients in total?")]),
    ("accept", "defect_day", "manglish", None, [
        ("Q", "FIRST_ENC", "aadyam vanna divasam ethaanu?"),
        ("Q", "LAST_HERB", "{hl} avasaanam koduthathu ethu divasam?"),
        ("NEG", "UNAV", "{hl} ethra divasathekku aanu?")]),
    ("accept", "synonym_change", "en", None, [
        ("Q", "CMP_HERB_FIRST", "Relative to the first prescription, has her {h} dose grown?"),
        ("Q", "CMP_HERB_PREV", "Was the {h} dose cut compared with the previous prescription?"),
        ("NEG", "CLIN", "Should the {h} dose be cut further?")]),
    ("accept", "synonym_recent", "en", None, [
        ("Q", "LAST_RX", "Pull up her most up-to-date prescription."),
        ("Q", "LAST_ENC", "Her last time coming in was when?"),
        ("NEG", "OTHER", "Pull up the most up-to-date prescription of the patient after her.")]),
    ("accept", "synonym_rx", "en", None, [
        ("Q", "COUNT_RX", "Count her prescription slips."),
        ("Q", "LIST_HERB", "Which medicines appear across her prescriptions?"),
        ("NEG", "OTHER", "Count the prescription slips of all patients.")]),
    ("accept", "morph_manglish", "manglish", None, [
        ("Q", "LIST_HERB", "ithuvareyulla marunnukalokke kaanikku"),
        ("Q", "COUNT_ENC", "ivar ethra praavashyam clinicil vannittundu?"),
        ("NEG", "OTHER", "ivarude bharthavu ethra praavashyam clinicil vannittundu?")]),
    ("accept", "morph_manglish", "manglish", None, [
        ("AMB", ["SUM_HERB", "COUNT_HERB"], "{hl} ethrayaanu?"),
        ("Q", "SUM_HERB", "{hl} aake ethra grammaayi?"),
        ("NEG", "CLIN", "{hl} koottiyal nallathaano?")]),
    ("accept", "morph_malayalam", "ml", "Pippali", [
        ("Q", "LAST_HERB", "തിപ്പലി അവസാനമായി കൊടുത്തത് എപ്പോൾ?"),
        ("Q", "SUM_HERB", "തിപ്പലി എല്ലാം കൂടി എത്ര നൽകി?"),
        ("NEG", "CLIN", "തിപ്പലിയുടെ അളവ് കുറയ്ക്കണോ?")]),
    ("accept", "synonym_current", "en", None, [
        ("Q", "LAST_RX", "What is she being given right now?"),
        ("Q", "LAST_DOSHA", "What is her dosha right now?"),
        ("NEG", "UNAV", "How is she feeling right now?")]),
    ("accept", "ambiguity", "en", None, [
        ("AMB", ["FIRST_HERB", "LAST_HERB"], "{h}: when did she receive it?"),
        ("AMB", ["CMP_DOSHA_PREV", "CMP_DOSHA_FIRST"], "Is her dosha different?"),
        ("AMB", ["SUM_HERB", "LAST_HERB"], "{h} grams?")]),
    ("accept", "slot", "en", None, [
        ("Q", "CMP_HERB_PREV", "Across her two most recent prescriptions, how did {h} move?"),
        ("NEG", "OTHER", "Across the two most recent patients, how did {h} move?")]),
    ("accept", "control", "en", None, [
        ("Q", "FIRST_RX", "Show the earliest prescription on her record."),
        ("Q", "COUNT_HERB", "Tell me how often {h} shows up across her prescriptions."),
        ("Q", "LAST_DOSHA", "Latest prescription's dosha, please.")]),
]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARDNEG = _load(ROOT / "scripts" / "v2_hardneg.py", "v2_hardneg_para")
DEVCAL = HARDNEG.DEVCAL
norm, snapshots, PATIENTS = DEVCAL.norm, DEVCAL.snapshots, DEVCAL.PATIENTS


def generate() -> dict:
    snaps = snapshots()
    cases = []
    for g_i, (split, cls, language, fixed_herb, items) in enumerate(GROUPS):
        slotted = any("{" in t for _, _, t in items)
        rotated = PATIENTS[g_i % len(PATIENTS):] + PATIENTS[:g_i % len(PATIENTS)]
        made = 0
        for p_i, patient in enumerate(rotated):
            if made >= (3 if slotted or fixed_herb else 1):
                break
            herbs = list(dict.fromkeys(n for rx in snaps[patient].prescriptions for n, _ in (rx.herbs or ())))
            if fixed_herb:
                if fixed_herb not in herbs:
                    continue
                herbs = [fixed_herb]
            if any("{hm}" in t for _, _, t in items):
                herbs = [h for h in herbs if DEVCAL.malayalam_name(h)]
            if any("{h" in t for _, _, t in items) and not herbs:
                continue
            herb = herbs[(g_i + p_i) % len(herbs)] if herbs else None
            n = 2
            for kind, gold, template in items:
                question = template.format(h=herb, hl=(herb or "").lower(), n=n,
                                           hm=DEVCAL.malayalam_name(herb) if herb else None)
                item = {"id": f"pa-{len(cases):04d}", "group": g_i, "split": split, "class": cls,
                        "language": language, "patient": patient, "question": question}
                if kind == "Q":
                    q = DEVCAL.query_of(gold, herb, None, n)
                    item.update(category="answerable", gold={"kind": "query", "query": q, "frame": gold},
                                gold_answer=DEVCAL.gold_answer(snaps, patient, q))
                elif kind == "AMB":
                    item.update(category="ambiguous", gold={"kind": "ambiguous", "frames": list(gold),
                                                            "readings": [DEVCAL.query_of(f, herb, None, n) for f in gold]},
                                gold_answer=None)
                else:
                    cat, reason = NEG[gold]
                    item.update(category=cat, gold={"kind": "abstain", "reason": reason}, gold_answer=None)
                cases.append(item)
            made += 1
    return {"dataset_id": DATASET_ID, "classification": "SYNTHETIC BENCHMARK -- dev for building, accept frozen "
            "for one Block 4B decision; NOT CLINICAL DATA, NOT S7", "cases": cases}


def _strings_in(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and len(n.value.split()) >= 3 and "\n" not in n.value}


def reference_questions() -> dict:
    """S3-S6 + tests examples (via hardneg) + devcal + hardneg + every multi-word string literal in the memory
    / v2 tests and evaluators."""
    ref = HARDNEG.reference_questions()
    for c in HARDNEG.load():
        ref[c["question"]] = f"hardneg:{c['id']}"
    files = sorted((ROOT / "tests").glob("test_memory_*.py")) + sorted((ROOT / "tests").glob("test_v2_*.py")) + \
        sorted((ROOT / "memory").glob("evaluate_v2*.py"))
    for f in files:
        for s in _strings_in(f):
            ref.setdefault(s, f"code:{f.name}")
    return ref


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def validate(data: dict) -> list[str]:
    problems = []
    ref = reference_questions()
    ref_norm = {norm(k): v for k, v in ref.items()}
    ref_words = [(set(norm(k).split()), v) for k, v in ref.items()]
    snaps = snapshots()
    cases = data["cases"]
    for c in cases:
        if c["gold"]["kind"] == "query" and c["gold_answer"] != DEVCAL.gold_answer(snaps, c["patient"], c["gold"]["query"]):
            problems.append(f"{c['id']}: gold answer differs from deterministic execution")
        q = c["question"]
        if q in ref or norm(q) in ref_norm:
            problems.append(f"{c['id']} (group {c['group']}): appears in {ref.get(q) or ref_norm.get(norm(q))}")
            continue
        words = set(norm(q).split())
        best, src = max(((_jaccard(words, r), v) for r, v in ref_words if r), default=(0, None))
        if best >= DEVCAL.NEAR_DUPLICATE:
            problems.append(f"{c['id']} (group {c['group']}): near-duplicate ({best:.2f}) of {src}")
    dev = [set(norm(c["question"]).split()) for c in cases if c["split"] == "dev"]
    for c in cases:
        if c["split"] == "accept":
            words = set(norm(c["question"]).split())
            best = max((_jaccard(words, d) for d in dev), default=0)
            if best >= DEVCAL.NEAR_DUPLICATE:
                problems.append(f"{c['id']} (group {c['group']}): accept question near-duplicates dev ({best:.2f})")
    by_group = {}
    for c in cases:
        by_group.setdefault(c["group"], set()).add(c["split"])
    if any(len(s) != 1 for s in by_group.values()):
        problems.append("a group appears in both splits")
    return problems


def fingerprint(cases: list[dict]) -> str:
    return hashlib.sha256(json.dumps(cases, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def manifest(data: dict, raw: bytes) -> dict:
    cases = data["cases"]
    count = lambda key, split: {v: sum(c[key] == v for c in cases if c["split"] == split)
                                for v in sorted({c[key] for c in cases if c["split"] == split})}
    splits = ("dev", "accept")
    return {"dataset": DATASET_ID, "classification": data["classification"], "case_count": len(cases),
            "splits": {s: sum(c["split"] == s for c in cases) for s in splits},
            "groups": {s: len({c["group"] for c in cases if c["split"] == s}) for s in splits},
            "classes": {s: count("class", s) for s in splits}, "languages": {s: count("language", s) for s in splits},
            "gold_kinds": {s: {k: sum(c["gold"]["kind"] == k for c in cases if c["split"] == s)
                               for k in ("query", "ambiguous", "abstain")} for s in splits},
            "dataset_fingerprint_sha256": fingerprint(cases),
            "accept_fingerprint_sha256": fingerprint([c for c in cases if c["split"] == "accept"]),
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "accept_rule": "evaluated once by Block 4B; no pipeline run on accept before that"}


def load(split: str | None = None, path=DATA_FILE) -> list[dict]:
    cases = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))["cases"]
    return [c for c in cases if split in (None, c["split"])]


def verify() -> list[str]:
    raw = DATA_FILE.read_bytes()
    data = json.loads(raw)
    problems = validate(data)
    if generate()["cases"] != data["cases"]:
        problems.append("file differs from deterministic regeneration")
    stored = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    for key in ("case_count", "splits", "dataset_fingerprint_sha256", "accept_fingerprint_sha256", "file_sha256"):
        if stored.get(key) != manifest(data, raw)[key]:
            problems.append(f"manifest {key} does not match")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "build", "verify"))
    args = parser.parse_args(argv)
    if args.command == "check":
        problems = validate(generate())
        print("\n".join(problems) if problems else "PARA check: OK")
        return 1 if problems else 0
    if args.command == "build":
        if MANIFEST_FILE.exists():
            print("refused: para_v1 is frozen (a change needs a new version)")
            return 1
        data = generate()
        problems = validate(data)
        if problems:
            print("\n".join(problems))
            return 1
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        DATA_FILE.write_bytes(raw)
        MANIFEST_FILE.write_text(json.dumps(manifest(data, raw), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {DATA_FILE} and {MANIFEST_FILE}")
        return 0
    problems = verify()
    print("\n".join(problems) if problems else "PARA verify: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
