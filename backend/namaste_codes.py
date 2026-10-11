"""
NAMASTE code hook -- reserved, not yet populated. This is a lookup table,
not a feature.

Context (researched, not assumed): India's Ministry of AYUSH runs NAMASTE
(National AYUSH Morbidity and Standardized Terminologies Electronic
Portal), a standardized vocabulary for Ayurveda/Siddha/Unani conditions,
with active 2025-2026 work on mapping it to the WHO's ICD-11 Traditional
Medicine Module 2 via FHIR, tied to the national Ayushman Bharat Digital
Mission. It is NOT yet a compliance requirement for a clinic this size --
one 2026 paper on the integration work describes it as "an emerging
infrastructure need rather than an existing requirement" -- but the
direction of travel is clear, and retrofitting a terminology mapping after
a clinic already has months of real records is far more painful than
reserving the space for it now, while there's nothing yet to migrate.

Every value below is None on purpose. Filling one in needs the same
discipline HERB_ALIASES (backend/herb_resolver.py) already follows: a real
NAMASTE code sourced from the actual portal, or the Ayurvedic Pharmacopoeia
of India's own cross-reference to it, for THIS herb specifically -- never
guessed, never bulk-imported. A wrong code here is worse than no code: it
would misrepresent this clinic's records to any future system (a
government portal, an insurer, another clinic's EMR) that trusts it.

Nothing in this codebase reads from this table yet. It exists so that when
someone does the real sourcing work, adding a code is a one-line data
entry, not a schema change or a migration.
"""
from typing import Optional

# canonical herb name (as returned by herb_resolver.normalize_herb) -> NAMASTE code.
# Keys match backend/herb_resolver.py's canonical names exactly -- if a herb
# is ever renamed there, rename it here too.
NAMASTE_CODES: dict[str, Optional[str]] = {
    "Ashwagandha": None,
    "Brahmi": None,
    "Dashamoola": None,
    "Guggulu": None,
    "Shatavari": None,
    "Triphala": None,
    "Arjuna": None,
    "Haridra": None,
    # Added when the formulary grew from 8 to 20 herbs (Sep 2026) -- same
    # rule applies: None until a real code is sourced for THIS herb.
    "Neem": None,
    "Yashtimadhu": None,
    "Punarnava": None,
    "Bhringraj": None,
    "Manjistha": None,
    "Vasaka": None,
    "Shankhpushpi": None,
    "Kutki": None,
    "Pippali": None,
    "Haritaki": None,
    "Amalaki": None,
    "Bibhitaki": None,
}


def get_namaste_code(canonical_herb_name: str) -> Optional[str]:
    """Returns the sourced NAMASTE code for a canonical herb name, or None
    if it hasn't been sourced yet -- which, as of writing, is true for
    every herb in this clinic's formulary. Unknown herb names also return
    None rather than raising, since this is informational metadata, not a
    validation boundary (that's normalize_herb's job, not this module's)."""
    return NAMASTE_CODES.get(canonical_herb_name)
