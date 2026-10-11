"""
Trained extractor for AyurCore v0.1 (AI-1, block T1).

T1 is a token-level sequence tagger: it marks herb mentions with their role,
quantities, and dosha words in a free-text note. Turning those spans into a
ClinicalParse (normalisation, herb-quantity linking) and verifying it are
later blocks (T2, T3).

SYNTHETIC ONLY. Trained and selected on S1 train/dev (eval/benchmarks/s1);
s1_test.json is never read here. Nothing here is a clinical result.
"""
