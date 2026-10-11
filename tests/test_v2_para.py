"""
PARA v1 (scripts/v2_para.py): integrity of the Block 4 paraphrase benchmark and of its frozen accept split.
The pipeline is never run on the accept split here.
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("v2_para_t", ROOT / "scripts" / "v2_para.py")
para = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(para)
FINGERPRINT = "29901a16541f33c4be9016d1cb7a89427909c6c04b6f762652a2435fe2a9237a"
ACCEPT_FINGERPRINT = "6d6345a593edbb9eabf9d09495b3a543c08dcf3719d5bbb04670664e9b33ea08"


class ParaIntegrityTests(unittest.TestCase):
    def test_frozen(self):
        self.assertEqual(para.verify(), [])
        self.assertEqual(para.fingerprint(para.load()), FINGERPRINT)
        self.assertEqual(para.fingerprint(para.load("accept")), ACCEPT_FINGERPRINT)
        self.assertEqual(para.main(["build"]), 1)

    def test_split_by_group_and_coverage(self):
        cases = para.load()
        groups = {}
        for c in cases:
            groups.setdefault(c["group"], set()).add(c["split"])
        self.assertTrue(all(len(s) == 1 for s in groups.values()))
        for split in ("dev", "accept"):
            part = [c for c in cases if c["split"] == split]
            self.assertEqual({c["language"] for c in part}, {"en", "manglish", "ml"}, split)
            self.assertEqual({c["gold"]["kind"] for c in part}, {"query", "ambiguous", "abstain"}, split)
            self.assertEqual({c["class"] for c in part}, {c["class"] for c in cases}, split)

    def test_overlap_checks_detect_copies(self):
        cases = para.load()
        dev_q = next(c for c in cases if c["split"] == "dev")["question"]
        bad = [dict(c) for c in cases]
        first_accept = next(i for i, c in enumerate(bad) if c["split"] == "accept")
        bad[first_accept]["question"] = dev_q
        self.assertTrue(any("near-duplicates dev" in p or "appears" in p for p in para.validate({"cases": bad})))


if __name__ == "__main__":
    unittest.main()
