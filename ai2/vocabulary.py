"""
The herb vocabulary AI-2 recognises: the application's own alias table, imported from ONE place.

AI-2 does not keep a copy: the clinic's formulary aliases (English, Manglish, Malayalam) are defined once in
backend.herb_resolver (a plain-Python module with no imports of its own). Every AI-2 module reads HERB_ALIASES
through this module, so a different vocabulary source can be substituted here without touching the pipeline.
"""
from backend.herb_resolver import HERB_ALIASES

__all__ = ["HERB_ALIASES"]
