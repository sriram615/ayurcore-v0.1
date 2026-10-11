#!/usr/bin/env python3
"""
S3 v1: synthetic natural-language memory questions with gold plans and gold answers (AI-2).

    ./venv/bin/python scripts/s3_generator.py generate   # write eval/benchmarks/s3/ (refuses if present)
    ./venv/bin/python scripts/s3_generator.py verify     # regenerate from the seed; must equal files and manifest

Every question has:
    gold plan     {"kind": "query", "query": {...}}  (the closed executor vocabulary), or
                  {"kind": "abstain", "reason": DIAGNOSIS | TREATMENT | UNAVAILABLE | AMBIGUOUS}
    gold answer   for a query: the deterministic executor's status, value and source labels on
                  the S2 reference snapshot of the question's patient (memory/s2_reference.py)
    frame, language (en / manglish / ml), template id, split

Templates are hand-written. Splits are BY TEMPLATE (a template's questions are all in one
split), so dev and test measure unseen phrasings, not memorised ones. Slot values (herbs,
doshas, numbers) vary per question.
SYNTHETIC; NOT CLINICAL DATA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.herb_resolver import HERB_ALIASES  # noqa: E402
from memory.executor import Query, execute  # noqa: E402
from memory.s2_reference import reference_snapshots  # noqa: E402

OUT_DIR = ROOT / "eval" / "benchmarks" / "s3"
MANIFEST_FILE = OUT_DIR / "manifest.json"
S2_FILE = ROOT / "eval" / "benchmarks" / "s2" / "s2_v1.json"
DATASET_ID = "s3_v1"
SCHEMA = "ayurcore.s3.questions/v1"
SEED = 20261007
SPLITS = ("train", "dev", "test")
PER_TEMPLATE = {"train": 8, "dev": 6, "test": 6}

# frame -> (op, target, a, b, slot needs)
FRAMES = {
    "FIRST_RX": ("first", "prescription", None, None, ""), "LAST_RX": ("last", "prescription", None, None, ""),
    "FIRST_HERB": ("first", "herb", None, None, "h"), "LAST_HERB": ("last", "herb", None, None, "h"),
    "FIRST_DOSHA": ("first", "dosha", None, None, ""), "LAST_DOSHA": ("last", "dosha", None, None, ""),
    "FIRST_ENC": ("first", "encounter", None, None, ""), "LAST_ENC": ("last", "encounter", None, None, ""),
    "COUNT_RX": ("count", "prescription", None, None, ""), "COUNT_RX_N": ("count", "prescription", None, None, "n"),
    "COUNT_ENC": ("count", "encounter", None, None, ""), "COUNT_HERB": ("count", "herb", None, None, "h"),
    "COUNT_HERB_N": ("count", "herb", None, None, "hn"), "COUNT_DOSHA": ("count", "dosha", None, None, "d"),
    "SUM_HERB": ("sum", "herb", None, None, "h"), "SUM_HERB_N": ("sum", "herb", None, None, "hn"),
    "LIST_RX": ("list", "prescription", None, None, ""), "LIST_ENC": ("list", "encounter", None, None, ""),
    "LIST_HERB": ("list", "herb", None, None, ""), "LIST_HERB_N": ("list", "herb", None, None, "n"),
    "LIST_DOSHA": ("list", "dosha", None, None, ""),
    "DIFF_PREV": ("diff", "prescription", "previous", "last", ""), "DIFF_FIRST": ("diff", "prescription", "first", "last", ""),
    "CMP_HERB_PREV": ("compare", "herb", "previous", "last", "h"), "CMP_HERB_FIRST": ("compare", "herb", "first", "last", "h"),
    "CMP_DOSHA_PREV": ("compare", "dosha", "previous", "last", ""), "CMP_DOSHA_FIRST": ("compare", "dosha", "first", "last", ""),
}
ABSTAIN = ("DIAGNOSIS", "TREATMENT", "UNAVAILABLE", "AMBIGUOUS")

# (language, template); {h} herb, {d} dosha, {n} number
T = {
    "FIRST_RX": [("en", "What was the first prescription?"), ("en", "Show me her very first prescription."),
                 ("en", "What did we give at the first visit?"), ("en", "Which herbs were on the earliest prescription?"),
                 ("manglish", "aadyathe prescription enthayirunnu?"), ("ml", "ആദ്യത്തെ കുറിപ്പടി എന്തായിരുന്നു?")],
    "LAST_RX": [("en", "What was the last prescription?"), ("en", "Show the most recent prescription."),
                ("en", "What is he currently on?"), ("en", "What did we prescribe at the latest visit?"),
                ("manglish", "avasanathe prescription enthaanu?"), ("ml", "അവസാനത്തെ കുറിപ്പടി എന്തായിരുന്നു?")],
    "FIRST_HERB": [("en", "When was {h} first prescribed?"), ("en", "When did she start {h}?"),
                   ("en", "Which visit first had {h}, and how much?"), ("en", "First time {h} was given?"),
                   ("manglish", "{h} aadyam eppol koduthu?"), ("ml", "{h} ആദ്യം എപ്പോഴാണ് കൊടുത്തത്?")],
    "LAST_HERB": [("en", "When was {h} last prescribed?"), ("en", "When did he last get {h}?"),
                  ("en", "How much {h} was in the most recent prescription that had it?"),
                  ("en", "Most recent time {h} was given?"), ("manglish", "{h} avasanam eppozhaanu koduthathu?"),
                  ("ml", "{h} അവസാനം എപ്പോഴാണ് കൊടുത്തത്?")],
    "FIRST_DOSHA": [("en", "What dosha was recorded at the first visit?"), ("en", "What was her original dosha?"),
                    ("en", "Which dosha did we note first?"), ("en", "Initial dosha assessment?"),
                    ("manglish", "aadyathe visitil dosham enthaayirunnu?"), ("ml", "ആദ്യ സന്ദർശനത്തിലെ ദോഷം ഏതായിരുന്നു?")],
    "LAST_DOSHA": [("en", "What is the current dosha?"), ("en", "Which dosha was recorded most recently?"),
                   ("en", "What dosha did we note at the last visit?"), ("en", "Latest dosha?"),
                   ("manglish", "ippozhathe dosham enthaanu?"), ("ml", "ഇപ്പോഴത്തെ ദോഷം ഏതാണ്?")],
    "FIRST_ENC": [("en", "When was the first visit?"), ("en", "When did she first come to the clinic?"),
                  ("en", "Date of the earliest encounter?"), ("en", "When was the first consultation opened?"),
                  ("manglish", "aadyathe visit eppozhaayirunnu?"), ("ml", "ആദ്യ സന്ദർശനം എപ്പോഴായിരുന്നു?")],
    "LAST_ENC": [("en", "When was the last visit?"), ("en", "When did he last come in?"),
                 ("en", "Date of the most recent encounter?"), ("en", "When was the latest consultation?"),
                 ("manglish", "avasanam vannathu eppozhaanu?"), ("ml", "അവസാന സന്ദർശനം എപ്പോഴായിരുന്നു?")],
    "COUNT_RX": [("en", "How many prescriptions does she have?"), ("en", "How many times have we prescribed for him?"),
                 ("en", "Total number of prescriptions?"), ("en", "Count the prescriptions on record."),
                 ("manglish", "ethra prescription und?"), ("ml", "എത്ര കുറിപ്പടികൾ ഉണ്ട്?")],
    "COUNT_RX_N": [("en", "How many prescriptions in the last {n} visits?"), ("en", "Count prescriptions among the latest {n}."),
                   ("en", "Of the last {n} prescriptions, how many are there?"),
                   ("en", "Number of prescriptions in the most recent {n}?"),
                   ("manglish", "avasanathe {n} prescriptionil ethra und?"), ("manglish", "last {n} visitil ethra prescription?")],
    "COUNT_ENC": [("en", "How many visits has she had?"), ("en", "How many times did he come to the clinic?"),
                  ("en", "Total number of encounters?"), ("en", "Count the consultations."),
                  ("manglish", "ethra thavana vannu?"), ("ml", "എത്ര തവണ വന്നു?")],
    "COUNT_HERB": [("en", "How many times was {h} prescribed?"), ("en", "In how many prescriptions does {h} appear?"),
                   ("en", "How often have we given {h}?"), ("en", "Number of prescriptions with {h}?"),
                   ("manglish", "{h} ethra thavana koduthu?"), ("ml", "{h} എത്ര തവണ കൊടുത്തു?")],
    "COUNT_HERB_N": [("en", "How many of the last {n} prescriptions had {h}?"), ("en", "In the latest {n} visits, how often was {h} given?"),
                     ("en", "Count {h} across the most recent {n} prescriptions."),
                     ("en", "How many times was {h} prescribed in the last {n}?"),
                     ("manglish", "avasanathe {n} prescriptionil {h} ethra thavana?"),
                     ("manglish", "last {n} visitil {h} ethra thavana koduthu?")],
    "COUNT_DOSHA": [("en", "How many times was {d} recorded?"), ("en", "How often was the dosha noted as {d}?"),
                    ("en", "In how many prescriptions was the dosha {d}?"), ("en", "Count visits assessed as {d}."),
                    ("manglish", "{d} ethra thavana rekhappeduthi?"), ("ml", "{d} എത്ര തവണ രേഖപ്പെടുത്തി?")],
    "SUM_HERB": [("en", "How much {h} has she received in total?"), ("en", "Total grams of {h} given?"),
                 ("en", "Add up all the {h} prescribed."), ("en", "What is the cumulative amount of {h}?"),
                 ("manglish", "aake ethra gram {h} koduthu?"), ("ml", "ആകെ എത്ര ഗ്രാം {h} കൊടുത്തു?")],
    "SUM_HERB_N": [("en", "How much {h} in total over the last {n} prescriptions?"), ("en", "Total {h} in the latest {n} visits?"),
                   ("en", "Sum the {h} grams across the most recent {n}."),
                   ("en", "Cumulative {h} for the last {n} prescriptions?"),
                   ("manglish", "avasanathe {n} visitil aake ethra gram {h}?"),
                   ("manglish", "last {n} prescriptionil {h} total ethra?")],
    "LIST_RX": [("en", "List all prescriptions."), ("en", "Show every prescription on record."),
                ("en", "Which prescriptions does she have?"), ("en", "Give me all of his prescriptions in order."),
                ("manglish", "ella prescriptionum kaanikku"), ("ml", "എല്ലാ കുറിപ്പടികളും കാണിക്കൂ")],
    "LIST_ENC": [("en", "List all visits."), ("en", "Show every encounter."), ("en", "Which consultations has he had?"),
                 ("en", "Give me all the visit dates."), ("manglish", "ella visitukalum kaanikku"),
                 ("ml", "എല്ലാ സന്ദർശനങ്ങളും കാണിക്കൂ")],
    "LIST_HERB": [("en", "Which herbs has she ever been given?"), ("en", "List every herb prescribed so far."),
                  ("en", "What herbs has he been on?"), ("en", "All herbs on record?"),
                  ("manglish", "ethokke marunnu koduthittund?"), ("ml", "ഏതൊക്കെ മരുന്നുകൾ കൊടുത്തിട്ടുണ്ട്?")],
    "LIST_HERB_N": [("en", "Which herbs were given in the last {n} prescriptions?"), ("en", "List herbs from the latest {n} visits."),
                    ("en", "What herbs appear in the most recent {n} prescriptions?"),
                    ("en", "Herbs used over the last {n}?"), ("manglish", "avasanathe {n} visitil ethokke marunnu?"),
                    ("manglish", "last {n} prescriptionil ulla marunnukal?")],
    "LIST_DOSHA": [("en", "What dosha was recorded at each visit?"), ("en", "List the doshas over time."),
                   ("en", "Show the dosha history."), ("en", "How has the dosha been noted across prescriptions?"),
                   ("manglish", "oro visitilum dosham enthaayirunnu?"), ("ml", "ഓരോ സന്ദർശനത്തിലും ദോഷം എന്തായിരുന്നു?")],
    "DIFF_PREV": [("en", "What changed since the previous prescription?"), ("en", "How is the latest prescription different from the one before?"),
                  ("en", "What did we change at the last visit?"), ("en", "Differences between the last two prescriptions?"),
                  ("manglish", "kazhinja thavanatheakkal enthu maattam?"), ("ml", "കഴിഞ്ഞ തവണത്തേക്കാൾ എന്ത് മാറ്റം ഉണ്ട്?")],
    "DIFF_FIRST": [("en", "What has changed since the first prescription?"), ("en", "Compare the first and the latest prescriptions."),
                   ("en", "How does the current prescription differ from the original one?"),
                   ("en", "Differences between the first visit and now?"),
                   ("manglish", "aadyathe prescriptionil ninnu enthu maari?"), ("manglish", "aadyam muthal ippol vare enthu maattam?")],
    "CMP_HERB_PREV": [("en", "Did {h} go up or down since the previous prescription?"), ("en", "How did the {h} dose change last time?"),
                      ("en", "Compare {h} between the last two prescriptions."), ("en", "Is {h} higher than in the prescription before?"),
                      ("manglish", "{h} kazhinja thavanatheakkal kooduthalo kuravo?"),
                      ("ml", "{h} കഴിഞ്ഞ തവണത്തേക്കാൾ കൂടിയോ കുറഞ്ഞോ?")],
    "CMP_HERB_FIRST": [("en", "How has {h} changed since the first prescription?"), ("en", "Compare {h} in the first and latest prescriptions."),
                       ("en", "Is there more {h} now than at the start?"), ("en", "{h}: first prescription versus now?"),
                       ("manglish", "{h} aadyathe prescriptionumaayi compare cheyyu"),
                       ("manglish", "aadyam muthal {h} ethra maari?")],
    "CMP_DOSHA_PREV": [("en", "Did the dosha change since the previous prescription?"), ("en", "Is the latest dosha the same as last time?"),
                       ("en", "Compare the dosha of the last two prescriptions."), ("en", "Has the dosha shifted since the visit before?"),
                       ("manglish", "dosham kazhinja thavanatheakkal maariyo?"), ("ml", "ദോഷം കഴിഞ്ഞ തവണത്തേക്കാൾ മാറിയോ?")],
    "CMP_DOSHA_FIRST": [("en", "Has the dosha changed since the first visit?"), ("en", "Compare the first and current dosha."),
                        ("en", "Is the dosha now different from the original assessment?"),
                        ("en", "Dosha at the start versus now?"), ("manglish", "aadyam muthal dosham maariyo?"),
                        ("ml", "ആദ്യത്തേതിൽ നിന്ന് ദോഷം മാറിയോ?")],
    "DIAGNOSIS": [("en", "What disease does she have?"), ("en", "Is this arthritis?"), ("en", "What is his diagnosis?"),
                  ("en", "Does she have diabetes?"), ("en", "What illness is causing the symptoms?"),
                  ("en", "Is the {d} imbalance a serious condition?"), ("manglish", "ivarkku enthu rogamaanu?"),
                  ("ml", "ഇവർക്ക് എന്ത് രോഗമാണ്?")],
    "TREATMENT": [("en", "What should I prescribe next?"), ("en", "Should I increase {h}?"), ("en", "Is {h} safe for her?"),
                  ("en", "Recommend a herb for this patient."), ("en", "What dose of {h} should I give now?"),
                  ("en", "Should we stop {h}?"), ("manglish", "ini enthu marunnu kodukkanam?"),
                  ("ml", "അടുത്തതായി എന്ത് മരുന്ന് കൊടുക്കണം?")],
    "UNAVAILABLE": [("en", "What was her blood pressure?"), ("en", "Did the pain improve after {h}?"),
                    ("en", "Does he have any allergies?"), ("en", "How often should she take {h} each day?"),
                    ("en", "What was his weight at the last visit?"), ("en", "Which doctor saw her last time?"),
                    ("en", "What symptoms did she complain of?"), ("manglish", "BP ethrayaayirunnu?"),
                    ("ml", "രക്തസമ്മർദ്ദം എത്രയായിരുന്നു?")],
    "AMBIGUOUS": [("en", "How much did she get?"), ("en", "Compare {h} and {h2}."), ("en", "Tell me about it."),
                  ("en", "When was it first given?"), ("en", "Total grams?"), ("en", "{h} and {h2} totals?"),
                  ("en", "asdf qwerty?"), ("en", "first last previous"), ("manglish", "ethra koduthu?"),
                  ("manglish", "athu eppol?")],
}
NUM_WORDS = {"en": ["two", "three", "four", "five"], "manglish": ["randu", "moonu", "naalu", "anchu"]}
NUM_VALUE = {"two": 2, "three": 3, "four": 4, "five": 5, "randu": 2, "moonu": 3, "naalu": 4, "anchu": 5}
MANGLISH_DOSHA = {"Vata": "vatham", "Pitta": "pitham", "Kapha": "kapham"}
ML_DOSHA = {"Vata": "വാതം", "Pitta": "പിത്തം", "Kapha": "കഫം"}


def _ml_alias(canonical: str) -> str:
    names = sorted(a for a, c in HERB_ALIASES.items() if c == canonical and any("ഀ" <= ch <= "ൿ" for ch in a))
    return names[0] if names else canonical


def _english_alias(rng: random.Random, canonical: str) -> str:
    plain = sorted(a for a, c in HERB_ALIASES.items() if c == canonical and a.isascii() and not any(ch.isdigit() for ch in a)
                   and "(" not in a)
    return canonical if rng.random() < 0.75 or not plain else rng.choice(plain)


def _split_templates(rng: random.Random) -> dict:
    assignment = {}
    for frame, templates in T.items():
        idx = list(range(len(templates)))
        rng.shuffle(idx)
        n_test = 2 if frame in ABSTAIN else 1
        for rank, i in enumerate(idx):
            split = "test" if rank < n_test else "dev" if rank == n_test else "train"
            assignment[f"{frame}#{i}"] = split
    return assignment


def generate(seed: int = SEED) -> dict:
    rng = random.Random(seed)
    s2 = json.loads(S2_FILE.read_text(encoding="utf-8"))
    snapshots = reference_snapshots(s2)
    keys = sorted(snapshots)
    rich = [k for k in keys if len(snapshots[k].prescriptions) >= 2]
    formulary = sorted(set(HERB_ALIASES.values()))
    assignment = _split_templates(rng)
    splits = {s: [] for s in SPLITS}
    counter = 0
    for frame, templates in T.items():
        for i, (lang, template) in enumerate(templates):
            tid = f"{frame}#{i}"
            split = assignment[tid]
            for _ in range(PER_TEMPLATE[split]):
                key = rng.choice(rich if rng.random() < 0.85 else keys)
                snap = snapshots[key]
                hist = sorted({n for p in snap.prescriptions for n, _ in (p.herbs or ())})
                herb = rng.choice(hist) if hist and rng.random() < 0.8 else rng.choice(formulary)
                herb2 = rng.choice([h for h in formulary if h != herb])
                doshas = sorted({p.dosha for p in snap.prescriptions if p.dosha})
                dosha = rng.choice(doshas) if doshas and rng.random() < 0.8 else rng.choice(
                    ("Vata", "Pitta", "Kapha", "Vata-Pitta", "Pitta-Kapha"))
                n = rng.choice((2, 3, 4))
                if lang == "en":
                    h_txt, h2_txt = _english_alias(rng, herb), _english_alias(rng, herb2)
                    d_txt = dosha
                    n_txt = str(n) if rng.random() < 0.6 else NUM_WORDS["en"][n - 2]
                elif lang == "manglish":
                    h_txt, h2_txt = herb.lower(), herb2.lower()
                    d_txt = MANGLISH_DOSHA.get(dosha, dosha.lower())
                    n_txt = str(n) if rng.random() < 0.5 else NUM_WORDS["manglish"][n - 2]
                else:
                    h_txt, h2_txt = _ml_alias(herb), _ml_alias(herb2)
                    d_txt = ML_DOSHA.get(dosha, dosha)
                    n_txt = str(n)
                question = template.format(h=h_txt, h2=h2_txt, d=d_txt, n=n_txt)
                if frame in ABSTAIN:
                    plan, answer = {"kind": "abstain", "reason": frame}, None
                else:
                    op, target, a, b, needs = FRAMES[frame]
                    fields = {"op": op, "target": target}
                    if "h" in needs:
                        fields["herb"] = herb
                    if "d" in needs:
                        fields["dosha"] = dosha
                    if "n" in needs:
                        fields["last_n"] = n
                    if a:
                        fields.update(a=a, b=b)
                    query = Query(**fields)
                    plan = {"kind": "query", "query": query.to_dict()}
                    result = execute(snap, query)
                    answer = {"status": result.status, "value": json.loads(json.dumps(result.value)),
                              "sources": sorted(result.labels)}
                counter += 1
                splits[split].append({"id": f"s3-{counter:05d}", "question": question, "language": lang,
                                      "frame": frame, "template": tid, "patient": key, "gold_plan": plan,
                                      "gold_answer": answer})
    return {"schema": SCHEMA, "dataset_id": DATASET_ID, "seed": seed,
            "s2_fingerprint": hashlib.sha256(json.dumps(s2["clinics"], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "splits": splits}


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data["splits"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _file(split: str) -> pathlib.Path:
    return OUT_DIR / f"s3_{split}.json"


def _payload(data: dict, split: str) -> bytes:
    body = {k: v for k, v in data.items() if k != "splits"} | {"split": split, "questions": data["splits"][split]}
    return (json.dumps(body, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def manifest(data: dict) -> dict:
    def counts(rows, key):
        out = {}
        for r in rows:
            out[r[key]] = out.get(r[key], 0) + 1
        return dict(sorted(out.items()))
    return {"dataset": DATASET_ID, "classification": "SYNTHETIC -- NOT CLINICAL DATA", "seed": data["seed"],
            "s2_fingerprint": data["s2_fingerprint"],
            "split_sizes": {s: len(data["splits"][s]) for s in SPLITS},
            "templates": {s: len({r["template"] for r in data["splits"][s]}) for s in SPLITS},
            "frames": counts([r for s in SPLITS for r in data["splits"][s]], "frame"),
            "languages": {s: counts(data["splits"][s], "language") for s in SPLITS},
            "dataset_fingerprint_sha256": fingerprint(data),
            "file_sha256": {f"s3_{s}.json": hashlib.sha256(_payload(data, s)).hexdigest() for s in SPLITS}}


def load(split: str) -> list[dict]:
    return json.loads(_file(split).read_text(encoding="utf-8"))["questions"]


def verify() -> list[str]:
    problems = []
    data = generate(SEED)
    for s in SPLITS:
        if _file(s).read_bytes() != _payload(data, s):
            problems.append(f"s3_{s}.json differs from the deterministic regeneration")
    stored = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    expected = manifest(data)
    for key in ("split_sizes", "dataset_fingerprint_sha256", "file_sha256", "s2_fingerprint"):
        if stored.get(key) != expected[key]:
            problems.append(f"manifest {key} does not match")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("generate", "verify"))
    args = parser.parse_args(argv)
    if args.command == "generate":
        if MANIFEST_FILE.exists() or any(_file(s).exists() for s in SPLITS):
            print("refused: S3 v1 exists (a change needs a new version)")
            return 1
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        data = generate()
        for s in SPLITS:
            _file(s).write_bytes(_payload(data, s))
        MANIFEST_FILE.write_text(json.dumps(manifest(data), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(manifest(data)["split_sizes"]))
        return 0
    problems = verify()
    print("\n".join(problems) if problems else "S3 verify: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
