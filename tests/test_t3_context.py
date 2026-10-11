"""
Candidate T3 context checks R1-R5 (extractor/verify_context.py), adversarial tests.

Every note here is newly hand-written for these tests (checked below not to
coincide with any S1 or S1-OOD v1 note). Each case states the upstream output
(possibly wrong, as a mis-labelling tagger would produce it) and the codes the
checks must raise -- or that nothing may be raised for a legitimate note.
R5 is a warning and must never produce a violation. The checks must not modify
the parse and must not import the T2 assembler.
"""
import ast
import copy
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.schemas import ClinicalParse
from extractor import verify_context
from extractor.verify_context import check_context


def _find(note, text, nth=0):
    start = -1
    for _ in range(nth + 1):
        start = note.index(text, start + 1)
    return start, start + len(text)


def build(note, facts, labels=()):
    """facts: [(herb text, nth, qty text, nth, canonical, grams)]; labels: extra [(text, nth, type)].
    Output facts are also labelled HERB_RX / QTY, as the tagger would have to label them."""
    spans, herbs, report = [], [], {"facts": [], "dosha_spans": []}
    for htext, hn, qtext, qn, name, grams in facts:
        hs, qs = _find(note, htext, hn), _find(note, qtext, qn)
        spans += [(*hs, "HERB_RX"), (*qs, "QTY")]
        herbs.append({"name": name, "grams": grams})
        report["facts"].append({"name": name, "grams": grams,
                                "herb_span": {"start": hs[0], "end": hs[1], "text": htext},
                                "quantity_span": {"start": qs[0], "end": qs[1], "text": qtext}})
    for text, nth, kind in labels:
        spans.append((*_find(note, text, nth), kind))
    return spans, {"dosha_imbalance": "", "prescribed_herbs": herbs}, report


def run(note, facts, labels=()):
    spans, parsed, report = build(note, facts, labels)
    before = copy.deepcopy(parsed), copy.deepcopy(report)
    out = check_context(note, spans, parsed, report)
    assert (parsed, report) == before, "check_context modified its input"
    return {v["code"] for v in out["violations"]}, {w["code"] for w in out["warnings"]}


# (label, note, facts, labels, codes that MUST be raised; empty set = must verify)
CASES = [
    # R1 conflicting quantities
    ("R1 cross-sentence correction, earlier output", "Kapha. Give Kutki 30 g. Correction for Kutki: 35 g.",
     [("Kutki", 0, "30 g", 0, "Kutki", 30)], (), {"conflicting_quantities"}),
    ("R1 three values, middle output", "Arjuna 20 g, no, 25 g, no, 30 g.",
     [("Arjuna", 0, "25 g", 0, "Arjuna", 25)], (), {"conflicting_quantities"}),
    ("R1 amount before herb, earlier output", "Vata. 50 g Shatavari venam. Shatavari 60 g mathi.",
     [("Shatavari", 0, "50 g", 0, "Shatavari", 50)], (), {"conflicting_quantities"}),
    ("R1 alias and canonical name", "Pitta. Amla 70 g. Amalaki should be 90 g.",
     [("Amla", 0, "70 g", 0, "Amalaki", 70)], (), {"conflicting_quantities"}),
    ("R1 forged link to the superseded amount", "Give Punarnava 40 g, no, make it 45 g.",
     [("Punarnava", 0, "40 g", 0, "Punarnava", 40)], (), {"conflicting_quantities"}),
    ("R1 ok: same value repeated", "Kutki 30 g. Kutki 30 g as noted.",
     [("Kutki", 0, "30 g", 0, "Kutki", 30)], (), set()),
    ("R1 ok: correction, last output", "Give Punarnava 40 g, no, make it 45 g.",
     [("Punarnava", 0, "45 g", 0, "Punarnava", 45)], [("40 g", 0, "QTY_OLD")], set()),
    ("R1 ok: correction unlabelled, last output", "Rx Kutki 15 g; correction: 20 g.",
     [("Kutki", 0, "20 g", 0, "Kutki", 20)], (), set()),
    ("R1 ok: pack amount", "Bibhitaki 3 x 50 g.", [("Bibhitaki", 0, "3 x 50 g", 0, "Bibhitaki", 150)], (), set()),
    ("R1 ok: shared amount", "Neem and Haridra, 45 g each.",
     [("Neem", 0, "45 g", 0, "Neem", 45), ("Haridra", 0, "45 g", 0, "Haridra", 45)], (), set()),
    ("R1 ok: earlier value of the same herb labelled as history", "Had Guggulu 20 g a year ago. Guggulu 40 g now.",
     [("Guggulu", 1, "40 g", 0, "Guggulu", 40)], [("Guggulu", 0, "HERB_HIST"), ("20 g", 0, "QTY_OTHER")], set()),
    ("R1 ok: later history amount of the same herb labelled", "Guggulu 40 g now. Had Guggulu 20 g a year ago.",
     [("Guggulu", 0, "40 g", 0, "Guggulu", 40)], [("Guggulu", 1, "HERB_HIST"), ("20 g", 0, "QTY_OTHER")], set()),
    # R2 alternatives
    ("R2 two amounts with or", "Vata. Arjuna 30 g or 45 g.",
     [("Arjuna", 0, "30 g", 0, "Arjuna", 30)], (), {"alternative_quantities"}),
    ("R2 either..or, last output (not a correction)", "Vata. Arjuna either 30 g or 45 g.",
     [("Arjuna", 0, "45 g", 0, "Arjuna", 45)], (), {"alternative_quantities"}),
    ("R2 slash between amounts", "Arjuna 30/45 g.", [("Arjuna", 0, "45 g", 0, "Arjuna", 45)], (),
     {"alternative_quantities"}),
    ("R2 bare number or amount", "Arjuna 30 or 45 g.", [("Arjuna", 0, "45 g", 0, "Arjuna", 45)], (),
     {"alternative_quantities"}),
    ("R2 Manglish disjunction", "Arjuna 30 g allenkil 45 g kodukkanam.",
     [("Arjuna", 0, "45 g", 0, "Arjuna", 45)], (), {"alternative_quantities"}),
    ("R2 alternative herbs, shared amount", "Pitta. Neem or Haridra 40 g.",
     [("Haridra", 0, "40 g", 0, "Haridra", 40)], (), {"alternative_herbs"}),
    ("R2 alternative herbs with amounts", "Neem 40 g or Haridra 40 g.",
     [("Neem", 0, "40 g", 0, "Neem", 40)], (), {"alternative_herbs"}),
    ("R2 ok: or between vehicles", "Kapha. Pippali 25 g with honey or warm water.",
     [("Pippali", 0, "25 g", 0, "Pippali", 25)], (), set()),
    ("R2 ok: or after the amount, not an amount", "Pippali 25 g, twice daily or as needed.",
     [("Pippali", 0, "25 g", 0, "Pippali", 25)], (), set()),
    ("R2 ok: blood pressure slash", "Kapha. BP 130/80. Pippali 25 g.",
     [("Pippali", 0, "25 g", 0, "Pippali", 25)], (), set()),
    ("R2 ok: or in another sentence", "Walk or swim daily. Pippali 25 g.",
     [("Pippali", 0, "25 g", 0, "Pippali", 25)], (), set()),
    ("R2 ok: and between herbs", "Neem and Haridra 40 g each.",
     [("Neem", 0, "40 g", 0, "Neem", 40), ("Haridra", 0, "40 g", 0, "Haridra", 40)], (), set()),
    # R3 historical context
    ("R3 ago after the amount", "Took Vasaka 50 g two weeks ago.",
     [("Vasaka", 0, "50 g", 0, "Vasaka", 50)], (), {"historical_context"}),
    ("R3 previously before the herb", "Previously Bhringraj 30 g was given.",
     [("Bhringraj", 0, "30 g", 0, "Bhringraj", 30)], (), {"historical_context"}),
    ("R3 Manglish munpu / -irunnu", "munpu Kutki 20 g kazhichirunnu.",
     [("Kutki", 0, "20 g", 0, "Kutki", 20)], (), {"historical_context"}),
    ("R3 Malayalam past adjective", "കഴിഞ്ഞ മാസം ബ്രഹ്മി 30 g.",
     [("ബ്രഹ്മി", 0, "30 g", 0, "Brahmi", 30)], (), {"historical_context"}),
    ("R3 the last N weeks", "Punarnava 40 g for the last 3 weeks.",
     [("Punarnava", 0, "40 g", 0, "Punarnava", 40)], (), {"historical_context"}),
    ("R3 ok: re-order verb makes a past reference current", "Continue Brahmi 30 g as last month.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], (), set()),
    ("R3 ok: history in another sentence", "Neem was used last month. Brahmi 30 g today.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], [("Neem", 0, "HERB_HIST")], set()),
    ("R3 ok: history in another clause", "Sleep better since last visit, so Brahmi 30 g.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], (), set()),
    ("R3 ok: present anchor between cue and herb", "Was on Neem last month and now Brahmi 30 g.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], [("Neem", 0, "HERB_HIST")], set()),
    ("R3 ok: present anchor, cue nearer the current herb", "Neem course ended last month now Brahmi 30 g.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], [("Neem", 0, "HERB_HIST")], set()),
    ("R3 ok: cue nearer another herb", "Brahmi 30 g after the Neem course of last month.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], [("Neem", 0, "HERB_HIST")], set()),
    ("R3 ok: before food", "Brahmi 30 g before food.", [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], (), set()),
    ("R3 ok: last as a verb", "Brahmi 30 g should last two weeks.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], (), set()),
    # R4 unresolved status
    ("R4 same herb stopped and prescribed", "Stop Manjistha. Manjistha 40 g.",
     [("Manjistha", 1, "40 g", 0, "Manjistha", 40)], [("Manjistha", 0, "HERB_NEG")], {"contested_status"}),
    ("R4 same herb replaced and prescribed", "Neem 30 g. Haridra 40 g instead of Neem.",
     [("Neem", 0, "30 g", 0, "Neem", 30), ("Haridra", 0, "40 g", 0, "Haridra", 40)], [("Neem", 1, "HERB_REPL")],
     {"contested_status"}),
    ("R4 negation before the herb", "Do not give Neem 40 g.", [("Neem", 0, "40 g", 0, "Neem", 40)], (),
     {"negated_context"}),
    ("R4 determiner no", "No Neem 40 g this time.", [("Neem", 0, "40 g", 0, "Neem", 40)], (), {"negated_context"}),
    ("R4 Manglish venda after the amount", "Neem 40 g venda.", [("Neem", 0, "40 g", 0, "Neem", 40)], (),
     {"negated_context"}),
    ("R4 Manglish -enda suffix", "Neem 40 g kodukkenda.", [("Neem", 0, "40 g", 0, "Neem", 40)], (),
     {"negated_context"}),
    ("R4 ok: correction with no", "Give Punarnava 40 g, no, make it 45 g.",
     [("Punarnava", 0, "45 g", 0, "Punarnava", 45)], [("40 g", 0, "QTY_OLD")], set()),
    ("R4 ok: correction with alla", "Shatavari 40 g alla, 50 g kodukkanam.",
     [("Shatavari", 0, "50 g", 0, "Shatavari", 50)], [("40 g", 0, "QTY_OLD")], set()),
    ("R4 ok: correction with venda", "Vasaka 20 g venda, 25 g mathi.",
     [("Vasaka", 0, "25 g", 0, "Vasaka", 25)], [("20 g", 0, "QTY_OLD")], set()),
    ("R4 ok: correction with sorry", "Bibhitaki 30 g, sorry, 35 g.",
     [("Bibhitaki", 0, "35 g", 0, "Bibhitaki", 35)], [("30 g", 0, "QTY_OLD")], set()),
    ("R4 ok: negation after a comma", "Neem 40 g, not Haridra.", [("Neem", 0, "40 g", 0, "Neem", 40)],
     [("Haridra", 0, "HERB_NEG")], set()),
    ("R4 ok: negation ends the previous sentence", "Oily food: no. Neem 40 g.",
     [("Neem", 0, "40 g", 0, "Neem", 40)], (), set()),
    ("R4 ok: another herb stopped", "Do not give Haridra. Neem 40 g.", [("Neem", 0, "40 g", 0, "Neem", 40)],
     [("Haridra", 0, "HERB_NEG")], set()),
    # unpunctuated Manglish: a clause-final verb ends the clause
    ("Manglish ok: amount before the next herb", "kapham kooduthal vasaka 40 g kodukkanam 60 g pippali venam",
     [("vasaka", 0, "40 g", 0, "Vasaka", 40), ("pippali", 0, "60 g", 0, "Pippali", 60)], (), set()),
    ("Manglish ok: past verb ends the previous clause", "munpu neem 30 g kazhichirunnu brahmi 25 g kodukkanam",
     [("brahmi", 0, "25 g", 0, "Brahmi", 25)], [("neem", 0, "HERB_HIST"), ("30 g", 0, "QTY_OTHER")], set()),
    ("Manglish ok: past time phrase belongs to its own clause",
     "kutki 45 g kodukkanam kazhinja aazhcha neem eduthirunnu",
     [("kutki", 0, "45 g", 0, "Kutki", 45)], [("neem", 0, "HERB_HIST")], set()),
    ("Manglish ok: stop verb ends the previous clause", "kutki nirthanam 45 g haridra venam",
     [("haridra", 0, "45 g", 0, "Haridra", 45)], [("kutki", 0, "HERB_NEG")], set()),
    ("Manglish ok: prohibitive ends the previous clause", "arjuna ini kodukkenda 2 x 60 g guggulu venam",
     [("guggulu", 0, "2 x 60 g", 0, "Guggulu", 120)], [("arjuna", 0, "HERB_NEG")], set()),
    ("Manglish ok: past time phrase modifies the following herb",
     "brahmi 25 g pinne kazhinja maasam neem 30 g eduthirunnu",
     [("brahmi", 0, "25 g", 0, "Brahmi", 25)], [("neem", 0, "HERB_HIST"), ("30 g", 0, "QTY_OTHER")], set()),
    ("Manglish ok: negation opens the next clause", "brahmi 20 g kodukkanam avoid neem",
     [("brahmi", 0, "20 g", 0, "Brahmi", 20)], [("neem", 0, "HERB_NEG")], set()),
    ("Manglish: past clause itself is rejected", "munpu neem 30 g kazhichirunnu brahmi 25 g kodukkanam",
     [("neem", 0, "30 g", 0, "Neem", 30)], (), {"historical_context"}),
    ("Manglish: past time phrase with its herb", "kazhinja aazhcha triphala 50 g eduthirunnu",
     [("triphala", 0, "50 g", 0, "Triphala", 50)], (), {"historical_context"}),
    ("Manglish: correction across a comma, earlier output", "arjuna 30 g alla, 35 g kodukkanam",
     [("arjuna", 0, "30 g", 0, "Arjuna", 30)], (), {"conflicting_quantities"}),
    ("Manglish: correction with venda, earlier output", "pippali 20 g venda, 30 g mathi",
     [("pippali", 0, "20 g", 0, "Pippali", 20)], (), {"conflicting_quantities"}),
    ("Manglish: own prohibitive verb", "haridra 45 g kodukkenda brahmi 20 g venam",
     [("haridra", 0, "45 g", 0, "Haridra", 45)], (), {"negated_context"}),
    # v3 G1 -- non-formulary items next to a dose (unstocked plants)
    ("G1 dose of an unstocked plant used for a herb", "Pitta. Manjistha 80 g, Lodhra 15 g.",
     [("Manjistha", 0, "15 g", 0, "Manjistha", 15)], (), {"foreign_quantity"}),
    ("G1 '<dose> of <plant>' used for a herb", "Kapha. Vasaka 40 g plus 10 g of Sitopaladi.",
     [("Vasaka", 0, "10 g", 0, "Vasaka", 10)], (), {"foreign_quantity"}),
    ("G1 Manglish unstocked dose", "pitham. manjistha 60 g, nannari 25 g.",
     [("manjistha", 0, "25 g", 0, "Manjistha", 25)], (), {"foreign_quantity"}),
    ("G1 Malayalam unstocked dose", "വാതം. ബ്രഹ്മി 40 g, ജടാമാംസി 15 g.",
     [("ബ്രഹ്മി", 0, "15 g", 0, "Brahmi", 15)], (), {"foreign_quantity"}),
    ("G1 alternative with an unstocked plant", "Vata. Shatavari 30 g or Vidari 30 g.",
     [("Shatavari", 0, "30 g", 0, "Shatavari", 30)], (), {"alternative_herbs"}),
    ("G1 ok: herb keeps its own dose next to an unstocked one", "Pitta. Manjistha 80 g, Lodhra 15 g.",
     [("Manjistha", 0, "80 g", 0, "Manjistha", 80)], (), set()),
    ("G1 ok: Manglish dose before its herb after a filler", "vatham kooduthal visappu illa 50 g ashwagandha venam",
     [("ashwagandha", 0, "50 g", 0, "Ashwagandha", 50)], (), set()),
    ("G1 ok: verb before the dose", "Pitta. Give 40 g of Neem.", [("Neem", 0, "40 g", 0, "Neem", 40)], (), set()),
    ("G1 ok: modifier before the dose", "Pitta. Neem about 40 g.", [("Neem", 0, "40 g", 0, "Neem", 40)], (), set()),
    # v3 G2 -- broader alternatives
    ("G2 disjunction with words between the doses", "Vata. Punarnava 60 g or, failing that, 40 g.",
     [("Punarnava", 0, "60 g", 0, "Punarnava", 60)], (), {"alternative_quantities"}),
    ("G2 conditional substitute", "Kapha. Guggulu 30 g; if Guggulu is short, give Pippali 20 g instead.",
     [("Guggulu", 0, "30 g", 0, "Guggulu", 30), ("Pippali", 0, "20 g", 0, "Pippali", 20)], (),
     {"conditional_alternative"}),
    ("G2 Manglish conditional substitute", "kapham. vasaka 40 g kittiyillenkil pippali 30 g pakaram.",
     [("pippali", 0, "30 g", 0, "Pippali", 30)], (), {"conditional_alternative"}),
    ("G2 ok: plain conditional indication", "Pitta. Amalaki 100 g if constipated.",
     [("Amalaki", 0, "100 g", 0, "Amalaki", 100)], (), set()),
    ("G2 ok: or between vehicles before another herb", "Vata. Brahmi 30 g with ghee or milk; Shankhpushpi 20 g.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30), ("Shankhpushpi", 0, "20 g", 0, "Shankhpushpi", 20)], (), set()),
    ("G2 ok: or in a frequency phrase", "Kapha. Pippali 25 g twice daily or as needed.",
     [("Pippali", 0, "25 g", 0, "Pippali", 25)], (), set()),
    # v3 G3 -- broader historical context
    ("G3 under a history header", "Vata. Medication history: Ashwagandha 50 g. Plan: Brahmi 30 g.",
     [("Ashwagandha", 0, "50 g", 0, "Ashwagandha", 50)], (), {"historical_context"}),
    ("G3 past-tense use verb", "Pitta. She tried Neem 40 g for a month without relief.",
     [("Neem", 0, "40 g", 0, "Neem", 40)], (), {"historical_context"}),
    ("G3 before this consultation", "Kapha. Before this consultation he received Triphala 100 g.",
     [("Triphala", 0, "100 g", 0, "Triphala", 100)], (), {"historical_context"}),
    ("G3 ok: plan after a history header", "Vata. Medication history: Ashwagandha 50 g. Plan: Brahmi 30 g.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], [("Ashwagandha", 0, "HERB_HIST"), ("50 g", 0, "QTY_OTHER")], set()),
    ("G3 ok: passive participle", "Pitta. Neem 40 g, to be used at night.", [("Neem", 0, "40 g", 0, "Neem", 40)], (),
     set()),
    ("G3 ok: before the meal", "Kapha. Triphala 100 g before the meal.",
     [("Triphala", 0, "100 g", 0, "Triphala", 100)], (), set()),
    ("G3 ok: resume a past prescription", "Vata. Resume Shatavari 50 g as taken last year.",
     [("Shatavari", 0, "50 g", 0, "Shatavari", 50)], (), set()),
    # v3 G4 -- longer-range negation and context
    ("G4 negated action later in the sentence", "Pitta. Haridra 30 g - allergy reported, do not give.",
     [("Haridra", 0, "30 g", 0, "Haridra", 30)], (), {"negated_context"}),
    ("G4 same herb negated in a later sentence", "Kapha. Pippali 20 g. Pippali venda ennu paranju.",
     [("Pippali", 0, "20 g", 0, "Pippali", 20)], (), {"contested_status"}),
    ("G4 anaphoric cancellation", "Vata. Bhringraj 25 g. Scratch that. Brahmi 25 g.",
     [("Bhringraj", 0, "25 g", 0, "Bhringraj", 25)], (), {"negated_context"}),
    ("G4 Manglish anaphoric cancellation", "pitham. neem 30 g kodukkanam. athu venda.",
     [("neem", 0, "30 g", 0, "Neem", 30)], (), {"negated_context"}),
    ("G4 ok: the herb after a cancellation", "Vata. Bhringraj 25 g. Scratch that. Brahmi 25 g.",
     [("Brahmi", 0, "25 g", 0, "Brahmi", 25)], (), set()),
    ("G4 ok: negated vehicle", "Pitta. Kutki 20 g, not with tea, only with water.",
     [("Kutki", 0, "20 g", 0, "Kutki", 20)], (), set()),
    ("G4 ok: negated non-herb in another sentence", "Kapha. Do not give sweets. Vasaka 50 g.",
     [("Vasaka", 0, "50 g", 0, "Vasaka", 50)], (), set()),
    ("G4 ok: pronoun with a negated non-action", "Vata. Brahmi 25 g; this does not cause drowsiness.",
     [("Brahmi", 0, "25 g", 0, "Brahmi", 25)], (), set()),
    ("G2 ok: single item with a conditional and 'or so'", "Pitta. Amalaki 100 g or so if needed.",
     [("Amalaki", 0, "100 g", 0, "Amalaki", 100)], (), set()),
    ("G3 ok: passive participle in the same clause", "Pitta. Neem 40 g to be used at night.",
     [("Neem", 0, "40 g", 0, "Neem", 40)], (), set()),
    ("G3 before this visit (no past verb)", "Kapha. Triphala 100 g before this visit; now Haritaki 40 g.",
     [("Triphala", 0, "100 g", 0, "Triphala", 100)], (), {"historical_context"}),
    ("G4 ok: negated action aimed at a following herb", "Pitta. Neem 40 g and do not give Haridra.",
     [("Neem", 0, "40 g", 0, "Neem", 40)], [("Haridra", 0, "HERB_NEG")], set()),
    ("G4 ok: administration instruction in the next sentence", "Pitta. Haridra 30 g. Do not give it with milk.",
     [("Haridra", 0, "30 g", 0, "Haridra", 30)], (), set()),
]

WARNING_CASES = [
    # (label, note, facts, labels, expect warning?)
    ("R5 empty output, stated dose", "Kapha. Pippali 25 g.", [], (), True),
    ("R5 unknown plant with a gram dose", "Neem 40 g. Lodhra 20 g.", [("Neem", 0, "40 g", 0, "Neem", 40)], (), True),
    ("R5 ok: kg weight change", "Weight loss 2 kg. Neem 40 g.", [("Neem", 0, "40 g", 0, "Neem", 40)], (), False),
    ("R5 ok: superseded amount labelled", "Give Punarnava 40 g, no, make it 45 g.",
     [("Punarnava", 0, "45 g", 0, "Punarnava", 45)], [("40 g", 0, "QTY_OLD")], False),
    ("R5 ok: amount of a stopped herb", "Discontinue Kutki 10 g. Neem 40 g.", [("Neem", 0, "40 g", 0, "Neem", 40)],
     [("Kutki", 0, "HERB_NEG")], False),
]


class ContextCheckTests(unittest.TestCase):
    def test_adversarial_cases(self):
        for label, note, facts, labels, expected in CASES:
            with self.subTest(label):
                codes, _ = run(note, facts, labels)
                if expected:
                    self.assertTrue(expected <= codes, f"{label}: expected {expected}, got {codes}")
                else:
                    self.assertEqual(codes, set(), f"{label}: legitimate note rejected with {codes}")

    def test_unaccounted_quantity_is_a_warning_only(self):
        for label, note, facts, labels, warn in WARNING_CASES:
            with self.subTest(label):
                codes, warnings = run(note, facts, labels)
                self.assertEqual(codes, set(), "R5 must never reject")
                self.assertEqual("unaccounted_quantity" in warnings, warn)

    def test_accepts_a_clinicalparse_and_does_not_modify_it(self):
        note = "Vata. Arjuna 30 g or 45 g."
        spans, parsed, report = build(note, [("Arjuna", 0, "30 g", 0, "Arjuna", 30)])
        model = ClinicalParse(dosha_imbalance="", prescribed_herbs=parsed["prescribed_herbs"])
        dump = model.model_dump()
        out = check_context(note, spans, model, report)
        self.assertEqual(model.model_dump(), dump)
        self.assertIn("alternative_quantities", {v["code"] for v in out["violations"]})
        self.assertTrue(all(v["rule"] in ("R1", "R2", "R3", "R4") for v in out["violations"]))

    def test_independent_of_the_assembler(self):
        tree = ast.parse((ROOT / "extractor" / "verify_context.py").read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                   {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertFalse({m for m in imported if m and "assemble" in m})
        self.assertLessEqual({m for m in imported if m and m.startswith("extractor")},
                             {"extractor.labels", "extractor.normalize", "extractor.units"})

    def test_every_lexicon_entry_has_an_allowed_source(self):
        for name, table in verify_context.LEXICONS.items():
            for word, source in table.items():
                with self.subTest(f"{name}:{word}"):
                    self.assertTrue(source.startswith(("S1-train", "closed-class")), source)
                    self.assertNotIn("ood", source.lower())

    def test_notes_are_new(self):
        def norm(text):
            return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())
        ours = {norm(c[1]) for c in CASES + WARNING_CASES}
        files = [ROOT / "eval" / "benchmarks" / "s1_ood" / "s1_ood_v1.json",
                 ROOT / "eval" / "benchmarks" / "s1_ood_v2" / "s1_ood_v2.json"] + \
                [ROOT / "eval" / "benchmarks" / "s1" / f"s1_{s}.json" for s in ("train", "dev", "test")]
        for path in files:  # S1 test is read only for this overlap comparison
            existing = {norm(c["notes"]) for c in json.loads(path.read_text(encoding="utf-8"))["cases"]}
            self.assertFalse(ours & existing, path.name)


if __name__ == "__main__":
    unittest.main()
