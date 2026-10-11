#!/usr/bin/env python3
"""
S1-OOD v3 one-shot final safety evaluation of the T1 -> T2 -> T3 v3 extractor.

    ./venv/bin/python -m extractor.evaluate_ood_v3 --final

Same procedure, metrics and one-shot lock as extractor/evaluate_ood_v2.py, which is
not modified: this module loads a private copy of it and points the copy at the v3
dataset tool (scripts/s1_ood_v3_dataset.py) and output directory
(artifacts/models/ood_v3_eval). The official v3 file needs --final, must still
verify against its frozen manifest, and is refused once results exist.
SYNTHETIC TECHNICAL RESULT, NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("evaluate_ood_v3_base", ROOT / "extractor" / "evaluate_ood_v2.py")
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

DATASET_TOOL_PATH = ROOT / "scripts" / "s1_ood_v3_dataset.py"
DEFAULT_OUT = ROOT / "artifacts" / "models" / "ood_v3_eval"
HASHED_SOURCES = _base.HASHED_SOURCES + ("extractor/evaluate_ood_v3.py", "scripts/s1_ood_v3_dataset.py",
                                         "scripts/s1_ood_v2_dataset.py", "extractor/evaluate_ood_v2.py")
_base.DATASET_TOOL_PATH = DATASET_TOOL_PATH
_base.DEFAULT_OUT = DEFAULT_OUT
_base.HASHED_SOURCES = HASHED_SOURCES

OODv3EvaluationRefused = _base.OODv2EvaluationRefused
classify_rejection = _base.classify_rejection
_check_dataset_v2 = _base.check_dataset


def _check_dataset_v3(*args, **kwargs):
    """The copied v2 refusal messages name "S1-OOD v2"; here they name v3 (same checks, same exception)."""
    try:
        return _check_dataset_v2(*args, **kwargs)
    except OODv3EvaluationRefused as exc:
        raise OODv3EvaluationRefused(str(exc).replace("S1-OOD v2", "S1-OOD v3")) from None


_base.check_dataset = _check_dataset_v3  # used by the copy's run() and main()


def run(dataset_path=None, model_path=None, out_dir=DEFAULT_OUT, final: bool = False) -> dict:
    return _base.run(dataset_path, model_path, out_dir, final=final)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--out" not in argv:
        argv += ["--out", str(DEFAULT_OUT)]
    return _base.main(argv)


if __name__ == "__main__":
    sys.exit(main())
