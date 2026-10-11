"""
AI-1 T1: S1 span labels and the trained span tagger (extractor/).

Proves: tokenisation; every S1 train/dev case aligns to gold spans exactly
(inflected herb tokens, nested quantities, shared quantities handled; anything
else raises); BIO tags round-trip; the perceptron learns and persists
losslessly; training is bit-for-bit reproducible; the frozen holdout
s1_test.json is never opened; the extractor does not import the generator.
"""
import ast
import builtins
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractor import train as t1
from extractor.features import sentence_features
from extractor.labels import TAGS, AlignmentError, bio_tags, case_spans, tags_to_spans, tokenize
from extractor.tagger import StructuredPerceptron

S1 = ROOT / "eval" / "benchmarks" / "s1"


def _cases(split):
    with open(S1 / f"s1_{split}.json", encoding="utf-8") as f:
        return json.load(f)["cases"]


TRAIN_DEV = _cases("train") + _cases("dev")
BY_ID = {c["case_id"]: c for c in TRAIN_DEV}


def _texts(case):
    return [(case["notes"][s:e], t) for s, e, t in case_spans(case)]


class TokenizeTests(unittest.TestCase):
    def test_tokens(self):
        words = lambda s: [t for t, _, _ in tokenize(s)]
        self.assertEqual(words("Neem 270g."), ["Neem", "270", "g", "."])
        self.assertEqual(words("3 x 50g"), ["3", "x", "50", "g"])
        self.assertEqual(words("1.5 kg"), ["1.5", "kg"])
        self.assertEqual(words("ബ്രഹ്മി 25 ഗ്രാം"), ["ബ്രഹ്മി", "25", "ഗ്രാം"])
        self.assertEqual(words("vata-pitta"), ["vata", "-", "pitta"])


class LabelTests(unittest.TestCase):
    def test_every_train_and_dev_case_aligns_and_round_trips(self):
        for case in TRAIN_DEV:
            with self.subTest(case=case["case_id"]):
                spans = case_spans(case)
                tokens = tokenize(case["notes"])
                self.assertEqual(tags_to_spans(tokens, bio_tags(tokens, spans)), spans)
                kinds = [t for _, _, t in spans]
                self.assertEqual(kinds.count("DOSHA"), 1)
                self.assertEqual(kinds.count("HERB_RX"), len(case["expected"]["herbs"]))

    def test_inflected_replaced_herb_is_the_whole_token(self):
        case = BY_ID["s1-negation-004"]  # "...arjunanu pakaram..." = instead of arjuna
        self.assertIn(("arjunanu", "HERB_REPL"), _texts(case))

    def test_quantity_inside_a_pack_quantity_is_not_matched(self):
        case = BY_ID["s1-mixed_hard-011"]  # "2 x 25 g" and, separately, "25 g"
        texts = _texts(case)
        self.assertIn(("2 x 25 g", "QTY"), texts)
        self.assertEqual(sum(1 for t, k in texts if t == "25 g" and k == "QTY"), 1)

    def test_shared_quantity_is_one_span_and_self_correction_marks_the_old_quantity(self):
        shared = next(c for c in TRAIN_DEV if "shared_quantity" in c["tags"])
        meta_shared = [m for m in shared["s1_meta"]["mentions"] if m.get("shared_quantity")]
        self.assertEqual(sum(1 for t, k in _texts(shared) if t == meta_shared[0]["quantity_text"] and k == "QTY"), 1)
        corrected = next(c for c in TRAIN_DEV if "self_correction" in c["tags"])
        old = next(m["superseded_quantity_text"] for m in corrected["s1_meta"]["mentions"]
                   if m.get("superseded_quantity_text"))
        self.assertIn((old, "QTY_OLD"), _texts(corrected))

    def test_unlocatable_strings_raise_instead_of_guessing(self):
        case = json.loads(json.dumps(BY_ID["s1-clean-001"] if "s1-clean-001" in BY_ID else TRAIN_DEV[0]))
        case["s1_meta"]["mentions"][0]["surface"] = "not-in-the-note"
        with self.assertRaises(AlignmentError):
            case_spans(case)


class TaggerTests(unittest.TestCase):
    def _toy(self):
        data = []
        for herb, qty in (("Neem", "20"), ("Brahmi", "35"), ("Kutki", "50"), ("Neem", "75")):
            tokens = tokenize(f"Give {herb} {qty} g. Stop Guggulu.")
            tags = ["O", "B-HERB_RX", "B-QTY", "I-QTY", "O", "O", "B-HERB_NEG", "O"]
            data.append((sentence_features(tokens), tags))
        return data

    def test_learns_a_toy_task_and_persists_losslessly(self):
        data = self._toy()
        model = StructuredPerceptron(TAGS)
        model.fit(data, epochs=5, seed=1)
        for feats, gold in data:
            self.assertEqual(model.predict(feats), gold)
        reloaded = StructuredPerceptron.from_json(json.loads(json.dumps(model.to_json())))
        unseen = sentence_features(tokenize("Give Kutki 20 g. Stop Guggulu."))
        self.assertEqual(reloaded.predict(unseen), model.predict(unseen))


class TrainingTests(unittest.TestCase):
    def test_training_is_reproducible_never_opens_the_holdout_and_reports(self):
        opened = []
        real_open = builtins.open

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("builtins.open", spy):
            r1 = t1.train(pathlib.Path(tmp) / "a")
            r2 = t1.train(pathlib.Path(tmp) / "b")
            m1 = json.loads((pathlib.Path(tmp) / "a" / "model.json").read_text(encoding="utf-8"))
            m2 = json.loads((pathlib.Path(tmp) / "b" / "model.json").read_text(encoding="utf-8"))
            reloaded = t1.load_model(pathlib.Path(tmp) / "a" / "model.json")
        self.assertFalse([p for p in opened if "s1_test" in p])
        self.assertEqual(r1["weights_sha256"], r2["weights_sha256"])
        self.assertEqual((m1["weights"], m1["transitions"]), (m2["weights"], m2["transitions"]))
        prov = r1["provenance"]
        self.assertEqual(prov["alignment_errors"], {"train": [], "dev": []})
        self.assertEqual((prov["train_cases_used"], prov["dev_cases_used"]), (360, 120))
        self.assertEqual(prov["s1_dataset_fingerprint"],
                         json.loads((S1 / "manifest.json").read_text())["dataset_fingerprint_sha256"])
        self.assertIn("NOT CLINICAL VALIDATION", r1["classification"])
        for key in ("spans_micro", "spans_by_type", "token_accuracy", "cases_exact_by_hard_tag"):
            self.assertIn(key, r1["dev"])
        self.assertEqual(len(r1["v1_ood_check"]["cases"]), 12)
        dev_examples, _ = t1.build_examples(t1.load_cases(t1.DEV_FILE))
        model = StructuredPerceptron.from_json(m1)
        self.assertTrue(all(model.predict(ex["features"]) == reloaded.predict(ex["features"]) for ex in dev_examples))

    def test_the_holdout_is_refused_before_it_is_opened(self):
        with mock.patch("builtins.open", side_effect=AssertionError("opened")) as fake_open:
            with self.assertRaises(t1.HoldoutError):
                t1.load_cases(S1 / "s1_test.json")
        fake_open.assert_not_called()

    def test_extractor_does_not_import_the_generator_or_name_the_holdout(self):
        for path in sorted((ROOT / "extractor").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            modules |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            self.assertFalse([m for m in modules if "s1_generator" in m], path.name)
            if path.name != "train.py":  # train.py names it only to refuse it
                docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                              if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                              and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
                strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                           and isinstance(n.value, str) and id(n) not in docstrings]
                self.assertFalse([s for s in strings if "s1_test" in s], path.name)


if __name__ == "__main__":
    unittest.main()
