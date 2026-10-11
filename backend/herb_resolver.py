class UnknownHerbError(Exception):
    pass

# Vocabulary sourcing note (Sep 2026): Malayalam entries below were added
# from https://indianmedicinalplants.info/malayalamnames.html and
# cross-checked against the botanical names already in db/seed.py (they
# matched exactly: Withania somnifera, Bacopa monnieri, Commiphora wightii,
# Asparagus racemosus, Curcuma longa, Terminalia arjuna). Dashamoola and
# Triphala are compound formulas (10 roots / 3 fruits respectively), not
# single plants, so they don't appear on that page as their own entries --
# their aliases below are unchanged from before.
#
# Deliberately NOT bulk-imported here: the open-source "herb-database"
# JSON dataset (github.com/sciencewithsaucee-sudo/herb-database, CC BY 4.0,
# 360 plants with Sanskrit synonyms) -- it doesn't cover Malayalam/vernacular
# names anyway, and more importantly, a wrong synonym in this dictionary
# doesn't just fail loudly (UnknownHerbError), it can silently map the
# WRONG raw name to the WRONG herb the clinic actually dispenses. Any
# future additions here, from that dataset or elsewhere, should be
# spot-checked against an authoritative source (e.g. the Ayurvedic
# Pharmacopoeia of India) and ideally reviewed by the doctor once, not
# merged in bulk from a crowd-sourced list.
#
# Formulary expansion note (Sep 2026): grew from 8 to 20 herbs. New
# Malayalam entries were cross-checked against SMPB Kerala's herbal
# database (smpbkerala.in/herbal-data/...), which ties Malayalam script
# directly to the botanical binomial for each species -- same bar as the
# original sourcing above. Two herbs (Shankhpushpi, Kutki) deliberately
# have NO Malayalam alias yet: the research pass either found no reliable
# Malayalam name at all (Shankhpushpi -- and worse, "Shankhupushpam" is
# sometimes used in real Kerala practice for a DIFFERENT plant entirely,
# Evolvulus alsinoides or Clitoria ternatea, so guessing here risks the
# exact wrong-herb-silently-matched failure this file exists to prevent)
# or found only a low-confidence candidate that looked copied from a
# Tamil source rather than a genuine distinct Malayalam term (Kutki, a
# Himalayan herb with no strong native Kerala vernacular tradition). Add a
# Malayalam alias for either only once a doctor or pharmacist confirms it.
#
# Also as part of this pass: Triphala's alias list used to include the
# botanical names of its three constituent fruits (Emblica officinalis,
# Terminalia chebula, Terminalia bellirica). That was fine while none of
# those three were stocked on their own -- but now that Amalaki, Haritaki,
# and Bibhitaki are each stocked individually (below), leaving those
# aliases pointed at Triphala would mean a doctor writing "Terminalia
# chebula 5g" -- clearly meaning the single herb Haritaki -- gets silently
# resolved to the three-fruit compound formula instead. Moved to their own
# entries; Triphala now only matches its own name.
HERB_ALIASES = {
    # Ashwagandha
    'ashwagandha': 'Ashwagandha',
    'withania somnifera': 'Ashwagandha',
    'ashwagandha root': 'Ashwagandha',
    'അമുക്കുരം': 'Ashwagandha',
    # അശ്വഗന്ധ is the direct Malayalam transliteration of "Ashwagandha"
    # (as opposed to അമുക്കുരം, the older traditional Malayalam name for
    # the same plant, Withania somnifera) -- added after live testing
    # showed Gemini being asked about this exact word, told "unknown" by
    # check_herb_alias, and falling back to guessing the English name from
    # its own knowledge instead of stopping. Closing the alias gap removes
    # the need for that guess. Source: same cross-check as the other
    # Malayalam entries here (indianmedicinalplants.info + seed.py's
    # botanical name, Withania somnifera).
    'അശ്വഗന്ധ': 'Ashwagandha',

    # Brahmi
    'brahmi': 'Brahmi',
    'bacopa monnieri': 'Brahmi',
    'ബ്രഹ്മി': 'Brahmi',

    # Dashamoola
    'dashamoola': 'Dashamoola',
    'dasamula': 'Dashamoola',
    '10 roots': 'Dashamoola',
    'dasamula (10 roots)': 'Dashamoola',
    'ten roots formula': 'Dashamoola',

    # Guggulu
    'guggulu': 'Guggulu',
    'commiphora wightii': 'Guggulu',
    'ഗുൽഗുലു': 'Guggulu',

    # Shatavari
    'shatavari': 'Shatavari',
    'asparagus racemosus': 'Shatavari',
    'ശതാവരി': 'Shatavari',

    # Triphala -- see the formulary expansion note above for why this no
    # longer includes its three constituent fruits' botanical names.
    'triphala': 'Triphala',

    # Arjuna
    'arjuna': 'Arjuna',
    'terminalia arjuna': 'Arjuna',
    'നീർമരുത്': 'Arjuna',

    # Haridra
    'haridra': 'Haridra',
    'curcuma longa': 'Haridra',
    'turmeric': 'Haridra',
    'മഞ്ഞൾ': 'Haridra',

    # Neem
    'neem': 'Neem',
    'azadirachta indica': 'Neem',
    'അര്യവേപ്പ്': 'Neem',

    # Yashtimadhu (Licorice)
    'yashtimadhu': 'Yashtimadhu',
    'licorice': 'Yashtimadhu',
    'liquorice': 'Yashtimadhu',
    'glycyrrhiza glabra': 'Yashtimadhu',
    # Both spellings below are used in Malayalam; ayushvedah's own
    # vernacular table lists an identical term under Tamil too, so this is
    # likely a word shared across both languages rather than unique to
    # Malayalam -- still valid to accept here either way.
    'അതിമധുരം': 'Yashtimadhu',
    'ഇരട്ടിമധുരം': 'Yashtimadhu',

    # Punarnava
    'punarnava': 'Punarnava',
    'boerhavia diffusa': 'Punarnava',
    'തഴുതാമ': 'Punarnava',

    # Bhringraj
    'bhringraj': 'Bhringraj',
    'bhringaraj': 'Bhringraj',
    'eclipta alba': 'Bhringraj',
    'eclipta prostrata': 'Bhringraj',
    # Multiple spelling variants genuinely coexist in Kerala usage; both
    # below are independently sourced, not a guess at one from the other.
    'കയ്യോന്നി': 'Bhringraj',
    'കയ്യുണ്ണി': 'Bhringraj',

    # Manjistha
    'manjistha': 'Manjistha',
    'rubia cordifolia': 'Manjistha',
    'മഞ്ചട്ടി': 'Manjistha',

    # Vasaka
    'vasaka': 'Vasaka',
    'adhatoda vasica': 'Vasaka',
    'justicia adhatoda': 'Vasaka',
    'ആടലോടകം': 'Vasaka',

    # Shankhpushpi -- deliberately no Malayalam alias. See the formulary
    # expansion note above: no reliable Malayalam name was found, and the
    # closest-sounding term in real use ("Shankhupushpam") can refer to a
    # different plant altogether in Kerala practice.
    'shankhpushpi': 'Shankhpushpi',
    'convolvulus pluricaulis': 'Shankhpushpi',

    # Kutki -- deliberately no Malayalam alias yet. See the formulary
    # expansion note above: the one candidate found looked copied from a
    # Tamil source rather than verified as genuinely Malayalam.
    'kutki': 'Kutki',
    'picrorhiza kurroa': 'Kutki',

    # Pippali
    'pippali': 'Pippali',
    'long pepper': 'Pippali',
    'piper longum': 'Pippali',
    'തിപ്പലി': 'Pippali',

    # Haritaki -- previously (incorrectly) aliased under Triphala; see the
    # formulary expansion note above for why it's its own herb now.
    'haritaki': 'Haritaki',
    'terminalia chebula': 'Haritaki',
    'കടുക്ക': 'Haritaki',

    # Amalaki -- previously (incorrectly) aliased under Triphala as
    # "Emblica officinalis"; see the formulary expansion note above.
    'amalaki': 'Amalaki',
    'amla': 'Amalaki',
    'indian gooseberry': 'Amalaki',
    'phyllanthus emblica': 'Amalaki',
    'emblica officinalis': 'Amalaki',
    'നെല്ലി': 'Amalaki',

    # Bibhitaki -- previously (incorrectly) aliased under Triphala; see
    # the formulary expansion note above.
    'bibhitaki': 'Bibhitaki',
    'terminalia bellirica': 'Bibhitaki',
    'താന്നി': 'Bibhitaki',
}


def normalize_herb(raw_name: str) -> str:
    cleaned = raw_name.strip().lower()

    if cleaned in HERB_ALIASES:
        return HERB_ALIASES[cleaned]

    raise UnknownHerbError(f"Herb '{raw_name}' is not in inventory.")


def merge_herb_lines(herbs) -> list[dict]:
    """The single herb-merging step behind /prescribe/confirm: resolves
    every name through normalize_herb() and sums the grams of lines that
    resolve to the same canonical herb (e.g. "ashwagandha 5" and "Withania
    somnifera 7" become one "Ashwagandha 12"). Lines keep the order in which
    each canonical herb first appears. Grams are summed exactly as given;
    bounds are checked by the caller. Raises UnknownHerbError for a name
    that doesn't resolve.

    Used by inventory_service.deduct_and_prescribe (the committed lines),
    the merged-total check in /prescribe/confirm, audit_store's
    _canonicalize_herbs and guardrails.build_confirm_candidate, so all four
    see the same merged lines."""
    merged: dict[str, int] = {}
    for herb in herbs:
        name = normalize_herb(herb["name"])
        merged[name] = merged.get(name, 0) + herb["grams"]
    return [{"name": name, "grams": grams} for name, grams in merged.items()]
