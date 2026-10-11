#!/usr/bin/env python3
"""
S1 -- synthetic Notes -> Structured Prescription benchmark (AyurCore v0.1).

SYNTHETIC, PROGRAMMATICALLY GENERATED. NOT CLINICAL VALIDATION. Every note is
built from a gold prescription sampled first, so the gold label is known by
construction; no note is real, and no result on S1 may be presented as a
clinical result.

    ./venv/bin/python scripts/s1_generator.py build     # (re)write eval/benchmarks/s1/
    ./venv/bin/python scripts/s1_generator.py verify    # regenerate in memory, compare with the frozen files

OUTPUT -- the existing case-file schema (ayurcore.clinical_eval.cases/v1,
validated by scripts/clinical_eval_harness.py), so the unchanged harness runs
Control and A0 on it with --cases, and scripts/s1_baseline.py runs the
deterministic baseline; all three are graded by the same grader:
    eval/benchmarks/s1/s1_{train,dev,test}.json   frozen, stratified splits
    eval/benchmarks/s1/manifest.json              sizes, file hashes, dataset fingerprint
Nothing is trained on "train": it is the split for prompt/rule development and
few-shot examples; "dev" for iteration; "test" for reported numbers only.

GENERATION -- one seeded random.Random, no global state, sorted iteration
everywhere (no set/hash ordering), so the same seed and the same formulary
give byte-identical files. The formulary and its aliases come from the
production table backend/herb_resolver.HERB_ALIASES (its sha256 is recorded
in the manifest; changing it changes the dataset, which `verify` reports).

Categories (75 cases each; every case also carries fine-grained tags):
  clean            English, canonical names, plain grams
  alias            English/botanical aliases from the formulary table
  malayalam_script Malayalam-script herb aliases, dosha words and units
  manglish         romanised Malayalam; misspellings; inflected names (-yum/-vum/-um)
  negation         a stocked herb that is stopped/avoided/not given (not in gold); a quantity the
                   doctor revises in the same line ("20 g, no, make it 30 g": gold 30); "X instead of Y"
  distractor       unstocked herbs, past medication, numbers that are not doses
  units            kg, packs (n x m g), number words, gm/gms; one quantity for two herbs ("... each")
  mixed_hard       three or more of the above in one note
Gold conventions (CLAUDE.md, docs/CLINICAL_EVAL_GROUND_TRUTH.md): grams are
TOTAL dispensed grams (a pack line "3 x 50 g" is 150 g); frequency/duration
never appear and are never inferred; dosha is a set; negated and historical
herbs are not prescribed; unstocked herbs are listed in mentioned_not_stocked.

Each case keeps its construction record in `s1_meta` (category, the surface
string and quantity text of every herb mention and its role). Models never
see it: the harness passes only `notes`. tests/test_s1_benchmark.py re-derives
the gold label from it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.herb_resolver import HERB_ALIASES, UnknownHerbError, normalize_herb  # noqa: E402

GENERATOR_VERSION = "s1-gen/1"
DEFAULT_SEED = 20261004
OUTPUT_DIR = os.path.join(ROOT, "eval", "benchmarks", "s1")
HERB_RESOLVER_PATH = os.path.join(ROOT, "backend", "herb_resolver.py")
CASE_SCHEMA_VERSION = "ayurcore.clinical_eval.cases/v1"
EVALUATION_CLASS = "technical_synthetic_unreviewed"
REPORT_LABEL = ("NOT CLINICAL VALIDATION -- S1 synthetic benchmark: programmatically generated notes "
                "with gold labels by construction. A technical evaluation of extraction only.")

CATEGORIES = ("clean", "alias", "malayalam_script", "manglish", "negation", "distractor", "units", "mixed_hard")
CASES_PER_CATEGORY = 75
SPLITS = (("train", 0.6), ("dev", 0.2), ("test", 0.2))
DOSHAS = ("Vata", "Pitta", "Kapha")

# --- vocabulary ------------------------------------------------------------------

def _is_malayalam(text: str) -> bool:
    return any("ഀ" <= ch <= "ൿ" for ch in text)


def _formulary() -> dict:
    """canonical -> {"ascii_aliases": [...], "malayalam_aliases": [...]}, sorted.
    Aliases containing digits ("10 roots") are excluded from generation: they
    would collide with quantity text and make the gold label ambiguous."""
    table: dict[str, dict[str, list[str]]] = {}
    for alias, canonical in sorted(HERB_ALIASES.items()):
        entry = table.setdefault(canonical, {"ascii_aliases": [], "malayalam_aliases": []})
        if alias == canonical.lower() or any(ch.isdigit() for ch in alias) or "(" in alias:
            continue
        entry["malayalam_aliases" if _is_malayalam(alias) else "ascii_aliases"].append(alias)
    return dict(sorted(table.items()))


FORMULARY = _formulary()
HERBS = tuple(FORMULARY)

# Hand-written misspellings; any that the production resolver accepts (i.e. a
# real alias) are dropped below, so "misspelling" always means "not in the table".
_MISSPELLINGS = {
    "Ashwagandha": ["ashwaganda", "aswagandha", "ashvagandha"], "Brahmi": ["bramhi", "brahmee"],
    "Shatavari": ["shatavri", "satavari"], "Triphala": ["thriphala", "tripala"],
    "Guggulu": ["gugulu", "guggullu"], "Haridra": ["haridhra"], "Yashtimadhu": ["yashtimadu", "yastimadhu"],
    "Punarnava": ["punarnavva", "punarnaava"], "Manjistha": ["manjishta", "manjista"],
    "Amalaki": ["amalakki", "amlaki"], "Haritaki": ["harithaki", "haritaky"], "Pippali": ["pipali", "pippli"],
    "Dashamoola": ["dashamula", "dasamoola"], "Shankhpushpi": ["shankapushpi", "sankhpushpi"],
    "Bhringraj": ["bringraj", "bhrungraj"], "Vasaka": ["vasakka"], "Arjuna": ["arjun"],
    "Bibhitaki": ["bibitaki", "vibhitaki"], "Kutki": ["kutaki"],
}


def _rejected_by_resolver(text: str) -> bool:
    try:
        normalize_herb(text)
    except UnknownHerbError:
        return True
    return False


MISSPELLINGS = {h: [m for m in ms if _rejected_by_resolver(m)] for h, ms in sorted(_MISSPELLINGS.items())}
MISSPELLINGS = {h: ms for h, ms in MISSPELLINGS.items() if ms}

# Plants the clinic does not stock (checked against the resolver at import).
UNSTOCKED = ("Tulsi", "Giloy", "Jatamansi", "Shilajit", "Kalmegh", "Musta", "Chitrak", "Sariva")
assert all(_rejected_by_resolver(u) for u in UNSTOCKED), "an UNSTOCKED name is in the formulary"

DOSHA_EN = {"Vata": "Vata", "Pitta": "Pitta", "Kapha": "Kapha"}
DOSHA_MANGLISH = {"Vata": "vatham", "Pitta": "pitham", "Kapha": "kapham"}
DOSHA_ML = {"Vata": "വാതം", "Pitta": "പിത്തം", "Kapha": "കഫം"}

NUMBER_WORDS = {10: "ten", 15: "fifteen", 20: "twenty", 25: "twenty five", 30: "thirty", 40: "forty",
                50: "fifty", 60: "sixty", 75: "seventy five", 80: "eighty", 100: "one hundred"}

FILLERS_EN = ("Complains of poor sleep.", "Joint stiffness in the mornings.", "Appetite is low.",
              "Reports mild acidity after meals.", "Feels tired through the day.")
FILLERS_MANGLISH = ("kore ksheenam und.", "urakkam sheriyavunnilla.", "vayaru veedhanam und.",
                    "visappu kuravanu.", "thalavedana idakku varunnu.")
NUMERIC_FILLERS_EN = ("Symptoms for {n} days.", "Age {a}.", "Sleeps about {h} hours.", "BP 130/80.",
                      "Weight {w} kg.")
NUMERIC_FILLERS_MANGLISH = ("{n} divasamayi vayya.", "prayam {a}.", "{h} manikkoor mathram urakkam.")


# --- quantities ------------------------------------------------------------------

def _plain_grams(rng) -> int:
    return rng.randrange(10, 305, 5)


def _quantity(rng, form: str, grams: int | None = None) -> dict:
    """{"grams", "text", "form", "spec"}; spec lets a test recompute grams."""
    if form == "kg":
        grams = grams or rng.choice((500, 1000, 1500, 2000, 2500, 3000))
        value = grams / 1000
        number = f"{value:g}"
        unit = rng.choice(("kg", "kg", "kilo"))
        return {"grams": grams, "text": f"{number} {unit}", "form": form, "spec": {"kg": value}}
    if form == "packs":
        count, each = rng.choice((2, 3, 4)), rng.choice((25, 50, 100))
        joiner = rng.choice(("{c} x {e} g", "{c} x {e}g", "{c} packets of {e} g"))
        return {"grams": count * each, "text": joiner.format(c=count, e=each), "form": form,
                "spec": {"count": count, "each_grams": each}}
    if form == "words":
        grams = rng.choice(sorted(NUMBER_WORDS))
        return {"grams": grams, "text": f"{NUMBER_WORDS[grams]} grams", "form": form, "spec": {"grams": grams}}
    grams = grams or _plain_grams(rng)
    pattern = {"g": "{g} g", "g_compact": "{g}g", "grams": "{g} grams", "gm": "{g}gm", "gms": "{g} gms",
               "ml_gram": "{g} ഗ്രാം"}[form]
    return {"grams": grams, "text": pattern.format(g=grams), "form": form, "spec": {"grams": grams}}


PLAIN_FORMS = ("g", "g_compact", "grams")
UNIT_FORMS = ("kg", "packs", "words", "gm", "gms")


# --- herb surfaces ------------------------------------------------------------------

def _inflect(name: str) -> str:
    """Manglish coordinating suffix ("and/also"): brahmi -> brahmiyum."""
    last = name[-1]
    if last in "aie":
        return name + "yum"
    if last in "uo":
        return name + "vum"
    return name + "um"


def _surface(rng, herb: str, kind: str) -> tuple[str, list[str]]:
    """(text as written in the note, tags)"""
    if kind == "canonical":
        return herb, []
    if kind == "alias":
        alias = rng.choice(FORMULARY[herb]["ascii_aliases"])
        return (alias.title() if rng.random() < 0.5 else alias), ["alias"]
    if kind == "malayalam":
        return rng.choice(FORMULARY[herb]["malayalam_aliases"]), ["malayalam_script"]
    if kind == "lower":
        return herb.lower(), []
    if kind == "misspelling":
        return rng.choice(MISSPELLINGS[herb]), ["misspelling"]
    if kind == "inflected":
        return _inflect(herb.lower()), ["inflection"]
    raise ValueError(kind)


# --- sentence templates ------------------------------------------------------------------

RX_EN = ("Prescribe {q} of {h}.", "Give {h} {q}.", "{h}: {q}.", "Start {h} {q}.", "Rx {h} {q}.")
RX_MANGLISH = ("{h} {q} kodukkanam.", "{q} {h} venam.", "{h} {q} kodukku.", "{h} {q} edukkanam.")
RX_ML = ("{h} {q} കൊടുക്കണം.", "{h} {q} നൽകുക.")
NEG_EN = ("Stop {h}.", "Do not give {h}.", "Avoid {h} for now.", "Discontinue {h} {q}.", "{h} is not needed.")
NEG_MANGLISH = ("{h} venda.", "{h} nirthanam.", "{h} ini kodukkenda.")
HISTORY_EN = ("Was on {h} {q} last month.", "Previously took {h}.", "Earlier course of {h} {q} completed.")
HISTORY_MANGLISH = ("munpu {h} {q} kazhichirunnu.", "kazhinja maasam {h} eduthirunnu.")
SELF_CORRECTION_EN = ("Give {h} {q1}, no, make it {q2}.", "Rx {h} {q1}; correction: {q2}.",
                      "{h} {q1}, sorry, {q2}.")
SELF_CORRECTION_MANGLISH = ("{h} {q1} alla, {q2} kodukkanam.", "{h} {q1} venda, {q2} mathi.")
INSTEAD_OF_EN = ("Give {h} {q} instead of {n}.", "Replace {n} with {h} {q}.")
INSTEAD_OF_MANGLISH = ("{n} maatti {h} {q} kodukkanam.", "{n}nu pakaram {h} {q} venam.")
SHARED_EN = ("{h1} and {h2} {q} each.", "Give {h1} and {h2}, {q} each.")
SHARED_MANGLISH = ("{h1} {h2} {q} veetham kodukkanam.",)
UNSTOCKED_EN = ("Takes {u} tea at home.", "Family suggested {u}.", "Already using {u} from a local shop.")
UNSTOCKED_MANGLISH = ("veettil {u} kudikkunnund.", "{u} kazhikkunnund ennu paranju.")
DOSHA_SENTENCES_EN = ("Patient has {d} imbalance.", "{d} aggravated.", "Assessment: {d} dosha.",
                      "Predominant {d} imbalance noted.")
DOSHA_SENTENCES_MANGLISH = ("{d} kooduthal aanu.", "{d} dosham kooduthal.", "rogikku {d} prashnam und.")
DOSHA_SENTENCES_ML = ("{d} കൂടുതൽ.", "{d} ദോഷം കൂടുതലാണ്.")


def _dosha_phrase(rng, doshas: list[str], style: str) -> tuple[str, str]:
    """(sentence, dosha surface)"""
    if style == "en":
        words = [DOSHA_EN[d] for d in doshas]
        surface = "-".join(words) if rng.random() < 0.6 else " and ".join(words)
        return rng.choice(DOSHA_SENTENCES_EN).format(d=surface), surface
    if style == "manglish":
        words = [DOSHA_MANGLISH[d] for d in doshas] if rng.random() < 0.6 else [d.lower() for d in doshas]
        surface = "-".join(words) if rng.random() < 0.5 else " ".join(words)
        return rng.choice(DOSHA_SENTENCES_MANGLISH).format(d=surface), surface
    surface = " ".join(DOSHA_ML[d] for d in doshas)
    return rng.choice(DOSHA_SENTENCES_ML).format(d=surface), surface


# --- one case ------------------------------------------------------------------

class _Builder:
    def __init__(self, rng: random.Random, category: str):
        self.rng = rng
        self.category = category
        self.sentences: list[str] = []
        self.mentions: list[dict] = []
        self.tags: set[str] = set()
        self.used: set[str] = set()

    def pick_herbs(self, n: int, pool=HERBS) -> list[str]:
        available = [h for h in pool if h not in self.used]
        chosen = self.rng.sample(available, n)
        self.used.update(chosen)
        return chosen

    def add(self, sentence: str, *, herb=None, surface=None, quantity=None, role=None, unstocked=None):
        self.sentences.append(sentence)
        if herb or unstocked:
            self.record(herb or unstocked, role, surface, quantity)

    def record(self, herb, role, surface, quantity, **extra):
        self.mentions.append({
            "herb": herb, "role": role, "surface": surface,
            "quantity_text": quantity["text"] if quantity else None,
            "quantity_form": quantity["form"] if quantity else None,
            "quantity_spec": quantity["spec"] if quantity else None,
            "grams": quantity["grams"] if quantity and role == "prescribed" else None,
            **extra,
        })

    def self_corrected(self, herb: str, style: str):
        """The doctor revises the quantity in the same line; gold is the revised one."""
        first = _quantity(self.rng, "g")
        final = _quantity(self.rng, "grams")
        while final["grams"] == first["grams"]:
            final = _quantity(self.rng, "grams")
        surface = herb if style == "en" else herb.lower()
        template = self.rng.choice(SELF_CORRECTION_EN if style == "en" else SELF_CORRECTION_MANGLISH)
        self.tags.update(("self_correction", "unit_grams"))
        self.sentences.append(template.format(h=surface, q1=first["text"], q2=final["text"]))
        self.record(herb, "prescribed", surface, final, superseded_quantity_text=first["text"])

    def replaced(self, herb: str, style: str):
        """'Give X instead of Y': Y is named but not prescribed."""
        old = self.pick_herbs(1)[0]
        quantity = _quantity(self.rng, "g")
        surface, old_surface = (herb, old) if style == "en" else (herb.lower(), old.lower())
        template = self.rng.choice(INSTEAD_OF_EN if style == "en" else INSTEAD_OF_MANGLISH)
        self.tags.update(("instead_of", "unit_grams"))
        self.sentences.append(template.format(h=surface, q=quantity["text"], n=old_surface))
        self.record(herb, "prescribed", surface, quantity)
        self.record(old, "replaced", old_surface, None)

    def shared(self, herb1: str, herb2: str, style: str):
        """One quantity applying to two herbs ("... 50 g each"): both get it."""
        quantity = _quantity(self.rng, self.rng.choice(("g", "grams", "gm")))
        if style == "en":
            s1, s2 = herb1, herb2
            template = self.rng.choice(SHARED_EN)
        else:
            s1, s2 = _inflect(herb1.lower()), _inflect(herb2.lower())
            template = self.rng.choice(SHARED_MANGLISH)
            self.tags.add("inflection")
        self.tags.update(("shared_quantity", "unit_gm" if quantity["form"] == "gm" else "unit_grams"))
        self.sentences.append(template.format(h1=s1, h2=s2, q=quantity["text"]))
        self.record(herb1, "prescribed", s1, quantity, shared_quantity=True)
        self.record(herb2, "prescribed", s2, quantity, shared_quantity=True)

    def rx(self, herb: str, kind: str, style: str, form: str):
        surface, tags = _surface(self.rng, herb, kind)
        quantity = _quantity(self.rng, form)
        template = self.rng.choice({"en": RX_EN, "manglish": RX_MANGLISH, "ml": RX_ML}[style])
        self.tags.update(tags)
        self.tags.add({"kg": "unit_kg", "packs": "unit_packs", "words": "number_words", "gm": "unit_gm",
                       "gms": "unit_gm", "ml_gram": "malayalam_unit"}.get(form, "unit_grams"))
        self.add(template.format(h=surface, q=quantity["text"]), herb=herb, surface=surface,
                 quantity=quantity, role="prescribed")

    def negated(self, style: str):
        herb = self.pick_herbs(1)[0]
        template = self.rng.choice(NEG_EN if style == "en" else NEG_MANGLISH)
        quantity = _quantity(self.rng, "g") if "{q}" in template else None
        surface = herb if style == "en" else herb.lower()
        self.tags.add("negation")
        self.add(template.format(h=surface, q=quantity["text"] if quantity else ""), herb=herb,
                 surface=surface, quantity=quantity, role="negated")

    def history(self, style: str):
        herb = self.pick_herbs(1)[0]
        template = self.rng.choice(HISTORY_EN if style == "en" else HISTORY_MANGLISH)
        quantity = _quantity(self.rng, "g") if "{q}" in template else None
        surface = herb if style == "en" else herb.lower()
        self.tags.add("history_mention")
        self.add(template.format(h=surface, q=quantity["text"] if quantity else ""), herb=herb,
                 surface=surface, quantity=quantity, role="history")

    def unstocked(self, style: str):
        name = self.rng.choice(UNSTOCKED)
        surface = name if style == "en" else name.lower()
        self.tags.add("unstocked_mention")
        self.add(self.rng.choice(UNSTOCKED_EN if style == "en" else UNSTOCKED_MANGLISH).format(u=surface),
                 unstocked=name, surface=surface, role="unstocked")

    def numeric_filler(self, style: str):
        template = self.rng.choice(NUMERIC_FILLERS_EN if style == "en" else NUMERIC_FILLERS_MANGLISH)
        self.tags.add("numeric_distractor")
        self.add(template.format(n=self.rng.randint(2, 30), a=self.rng.randint(20, 75),
                                 h=self.rng.randint(3, 6), w=self.rng.randint(45, 90)))

    def filler(self, style: str):
        self.add(self.rng.choice(FILLERS_EN if style == "en" else FILLERS_MANGLISH))


def _build_case(rng: random.Random, category: str) -> dict:
    b = _Builder(rng, category)
    style = {"clean": "en", "alias": "en", "malayalam_script": "ml", "manglish": "manglish",
             "negation": rng.choice(("en", "manglish")), "distractor": rng.choice(("en", "manglish")),
             "units": rng.choice(("en", "en", "manglish")), "mixed_hard": "manglish"}[category]
    b.tags.add({"en": "english", "manglish": "manglish", "ml": "malayalam_script"}[style])
    doshas = sorted(rng.sample(DOSHAS, rng.choice((1, 1, 2))), key=DOSHAS.index)
    dosha_sentence, dosha_surface = _dosha_phrase(rng, doshas, style)

    n_rx = rng.choice((1, 2, 2, 3, 3, 4))
    if category == "malayalam_script":
        pool = [h for h in HERBS if FORMULARY[h]["malayalam_aliases"]]
    elif category == "alias":
        pool = [h for h in HERBS if FORMULARY[h]["ascii_aliases"]]
    else:
        pool = HERBS
    prescribed = b.pick_herbs(n_rx, pool)

    extras: list = []  # callables adding non-prescription sentences
    variant = "none"   # harder prescription lines: self_correction, instead_of, shared
    if category == "negation":
        variant = rng.choice(("none", "self_correction", "instead_of"))
        n_negated = rng.choice((1, 1, 2)) if variant == "none" else rng.choice((0, 1))
        extras += [lambda: b.negated(style) for _ in range(n_negated)]
    if category == "units" and n_rx >= 3 and rng.random() < 0.5:
        variant = "shared"
    if category == "mixed_hard":
        variant = rng.choice(("none", "self_correction", "instead_of") + (("shared",) if n_rx >= 2 else ()))
    if category == "distractor":
        options = [lambda: b.unstocked(style), lambda: b.history(style), lambda: b.numeric_filler(style)]
        extras += rng.sample(options, rng.choice((2, 3)))
    if category == "mixed_hard":
        options = [lambda: b.negated(style), lambda: b.history(style), lambda: b.unstocked(style),
                   lambda: b.numeric_filler(style)]
        extras += rng.sample(options, 2)

    def kind_for(herb: str) -> str:
        if category == "alias":
            return "alias"
        if category == "malayalam_script":
            return "malayalam"
        if category == "manglish":
            return rng.choice(("lower", "lower", "misspelling", "inflected")) if herb in MISSPELLINGS else "lower"
        if category == "mixed_hard":
            options = ["lower", "inflected"] + (["misspelling"] if herb in MISSPELLINGS else []) + (
                ["malayalam"] if FORMULARY[herb]["malayalam_aliases"] else [])
            return rng.choice(options)
        return "canonical"

    def form_for() -> str:
        if category == "units":
            return rng.choice(UNIT_FORMS)
        if category == "malayalam_script":
            return rng.choice(("ml_gram", "g", "grams"))
        if category == "mixed_hard":
            return rng.choice(UNIT_FORMS + PLAIN_FORMS)
        return rng.choice(PLAIN_FORMS)

    rx_lines = [(lambda h=h: b.rx(h, kind_for(h), style, form_for())) for h in prescribed]
    if category == "units":  # at least one non-plain unit
        first = prescribed[0]
        rx_lines[0] = lambda: b.rx(first, "canonical", style, rng.choice(("kg", "packs", "words")))
    if variant == "self_correction":
        rx_lines[0] = lambda: b.self_corrected(prescribed[0], style)
    elif variant == "instead_of":
        rx_lines[0] = lambda: b.replaced(prescribed[0], style)
    elif variant == "shared":
        pair = prescribed[1:3] if category == "units" else prescribed[0:2]
        start = 1 if category == "units" else 0
        rx_lines[start:start + 2] = [lambda: b.shared(pair[0], pair[1], style)]

    b.add(dosha_sentence)
    if rng.random() < 0.5 and category not in ("clean",):
        b.filler(style)
    body = rx_lines + extras
    rng.shuffle(body)
    for add_sentence in body:
        add_sentence()

    notes = " ".join(b.sentences)
    if category == "mixed_hard" and rng.random() < 0.5:
        # Sentence punctuation only: a "." followed by a digit is a decimal ("1.5 kg") and stays.
        notes = " ".join(re.sub(r"\.(?!\d)|:", " ", notes).split())
        b.tags.add("no_punctuation")
    if n_rx > 1:
        b.tags.add("multi_herb")

    gold = {m["herb"]: m["grams"] for m in b.mentions if m["role"] == "prescribed"}
    return {
        "notes": notes,
        "tags": sorted(b.tags),
        "expected": {"dosha": doshas,
                     "herbs": {h: {"grams": gold[h], "tolerance_grams": 0} for h in sorted(gold)}},
        "mentioned_not_stocked": sorted({m["herb"] for m in b.mentions if m["role"] == "unstocked"}),
        "s1_meta": {"category": category, "style": style, "dosha_surface": dosha_surface,
                    "mentions": b.mentions},
    }


def _gold_signature(case: dict) -> str:
    e = case["expected"]
    return json.dumps([e["dosha"], sorted((h, s["grams"]) for h, s in e["herbs"].items())])


def _normalized_note(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


# --- dataset ------------------------------------------------------------------

def generate(seed: int = DEFAULT_SEED, per_category: int = CASES_PER_CATEGORY) -> dict:
    """{"train": [...], "dev": [...], "test": [...]} -- deterministic in (seed, formulary).
    Notes and gold signatures are unique across the WHOLE dataset (rejection
    sampling), so no split can share either with another."""
    rng = random.Random(seed)
    seen_notes, seen_gold = set(), set()
    by_category: dict[str, list[dict]] = {}
    for category in CATEGORIES:
        cases, attempts = [], 0
        while len(cases) < per_category:
            attempts += 1
            if attempts > per_category * 50:
                raise RuntimeError(f"could not generate {per_category} unique {category} cases")
            case = _build_case(rng, category)
            note_key, gold_key = _normalized_note(case["notes"]), _gold_signature(case)
            if note_key in seen_notes or gold_key in seen_gold:
                continue
            seen_notes.add(note_key)
            seen_gold.add(gold_key)
            cases.append(case)
        by_category[category] = cases

    splits: dict[str, list[dict]] = {name: [] for name, _ in SPLITS}
    for category in CATEGORIES:
        cases = by_category[category]
        order = list(range(len(cases)))
        random.Random(f"{seed}:split:{category}").shuffle(order)
        start = 0
        for index, (name, fraction) in enumerate(SPLITS):
            end = len(order) if index == len(SPLITS) - 1 else start + round(fraction * len(order))
            for case_index in sorted(order[start:end]):
                splits[name].append(_as_case(cases[case_index], category, name, case_index, seed))
            start = end
    return splits


def _as_case(raw: dict, category: str, split: str, index: int, seed: int) -> dict:
    case_id = f"s1-{category}-{index:03d}"
    return {
        "case_id": case_id,
        "label": category,
        "provenance": {
            "status": "synthetic_unreviewed",
            "origin": "Programmatically generated synthetic note; gold label by construction; "
                      "not clinical ground truth",
            "source": f"scripts/s1_generator.py ({GENERATOR_VERSION}, seed {seed})",
            "source_label": category,
            "clinician_reviewer": None,
            "reviewed_at": None,
        },
        "tags": raw["tags"],
        "notes": raw["notes"],
        "ambiguous": False,
        "ambiguity_reason": None,
        "expected": raw["expected"],
        "mentioned_not_stocked": raw["mentioned_not_stocked"],
        "safety_annotations": [],
        "annotation_notes": None,
        "source_rationale": None,
        "split": split,
        "s1_meta": raw["s1_meta"],
    }


def dataset_fingerprint(splits: dict) -> str:
    """sha256 of every case's canonical JSON, ordered by case_id: independent
    of file formatting and split file order."""
    cases = sorted((c for name in splits for c in splits[name]), key=lambda c: c["case_id"])
    canonical = json.dumps(cases, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def render(splits: dict, seed: int) -> dict:
    """{filename: text} for every output file, manifest included."""
    files = {}
    for name, _ in SPLITS:
        cases = splits[name]
        files[f"s1_{name}.json"] = json.dumps({
            "schema_version": CASE_SCHEMA_VERSION,
            "corpus_id": f"s1_synthetic_notes_v1_{name}",
            "evaluation_class": EVALUATION_CLASS,
            "report_label": REPORT_LABEL,
            "purpose": "S1 synthetic Notes -> Structured Prescription benchmark (AyurCore v0.1 portfolio "
                       "project). Compares the deterministic baseline, Control and A0 on the same cases "
                       "and grader. Synthetic only; never clinical validation.",
            "conventions": {
                "expected_grams": "TOTAL dispensed grams per herb; a pack line 'n x m g' is n*m grams.",
                "frequency_and_duration": "Never present in S1 notes; never inferred or graded.",
                "expected_dosha": "Set of canonical doshas, compared as sets.",
                "negated_and_history_herbs": "Not prescribed; absent from expected.herbs.",
                "mentioned_not_stocked": "Unstocked plants named in the note; proposing one is flagged.",
                "s1_meta": "Construction record (never shown to a model).",
            },
            "s1": {"generator_version": GENERATOR_VERSION, "seed": seed, "split": name, "count": len(cases)},
            "cases": cases,
        }, indent=1, ensure_ascii=False) + "\n"
    counts = {name: len(splits[name]) for name, _ in SPLITS}
    per_category = {name: {c: sum(1 for x in splits[name] if x["label"] == c) for c in CATEGORIES}
                    for name, _ in SPLITS}
    files["manifest.json"] = json.dumps({
        "benchmark": "S1 synthetic Notes -> Structured Prescription",
        "classification": REPORT_LABEL,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "dataset_fingerprint_sha256": dataset_fingerprint(splits),
        "herb_resolver_sha256": _sha256_file(HERB_RESOLVER_PATH),
        "total_cases": sum(counts.values()),
        "split_counts": counts,
        "split_category_counts": per_category,
        "split_file_sha256": {f"s1_{name}.json": hashlib.sha256(files[f"s1_{name}.json"].encode("utf-8"))
                              .hexdigest() for name, _ in SPLITS},
    }, indent=1, ensure_ascii=False) + "\n"
    return files


def build(output_dir: str = OUTPUT_DIR, seed: int = DEFAULT_SEED) -> dict:
    files = render(generate(seed), seed)
    os.makedirs(output_dir, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(output_dir, name), "w", encoding="utf-8") as f:
            f.write(text)
    return json.loads(files["manifest.json"])


def verify(output_dir: str = OUTPUT_DIR) -> list[str]:
    """Differences between the frozen files and a fresh in-memory generation."""
    manifest_path = os.path.join(output_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return [f"{manifest_path} missing"]
    with open(manifest_path, encoding="utf-8") as f:
        seed = json.load(f)["seed"]
    problems = []
    for name, text in render(generate(seed), seed).items():
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path):
            problems.append(f"{name}: missing")
            continue
        with open(path, encoding="utf-8") as f:
            if f.read() != text:
                problems.append(f"{name}: differs from a fresh generation")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="build only")
    parser.add_argument("--out", default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    if args.command == "build":
        manifest = build(args.out, args.seed)
        print(json.dumps({k: manifest[k] for k in ("total_cases", "split_counts",
                                                     "dataset_fingerprint_sha256")}, indent=1))
        return 0
    problems = verify(args.out)
    for p in problems:
        print(p)
    print("S1 verify:", "OK" if not problems else "DRIFT")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
