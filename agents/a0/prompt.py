"""
A0's prompt assembly (A0 architecture report, sections 5, 6, 10).

A new file, deliberately not shared with or derived from agents/copilot.py
-- "The duplication between copilot.py and agents/a0/prompt.py is the
price of a valid experiment, and it is cheap" (report section 10).
copilot.py stays byte-identical so it remains a valid, unmodified control
arm; editing it to share a prompt constant with this file would stop the
A/B design in section 11 from measuring two independent implementations.

Only PII-free, by-construction context ever enters these prompts (section
6's "may enter the prompt" list): the tokenized notes text (already
cleared by backend.pii_sanitizer.assert_no_pii_leak before
agents/a0/orchestrator.py ever calls this), the clinic's formulary, and
this clinic's current stock. Nothing here ever receives patient identity,
a PII token mapping, session/user identifiers, or a preview_id.
"""
from agents.a0.context import ClinicalContext

_OUTPUT_SHAPE = (
    '{"dosha_imbalance": "<string>", "prescribed_herbs": '
    '[{"name": "<canonical clinic herb name>", "grams": <integer>}]}'
)


def build_prompt(notes_text: str, context: ClinicalContext) -> str:
    """Assembles the single prompt A0 sends on its first reasoning call.

    Current stock is embedded directly, not fetched via a tool (see
    agents/a0/tools.py's docstring for why) -- it's just numbers in the
    prompt, so it can never be swayed by the model's own reasoning, and
    the state machine's own rule (report section 7) already requires that
    a low or insufficient stock reading must never change the proposal.
    """
    stock_lines = "\n".join(
        f"    - {herb}: {grams}g in stock"
        for herb, grams in sorted(context.stock_by_herb.items())
    ) or "    (no stock data on file for this clinic)"

    return f"""
    You are an expert Ayurvedic clinical assistant AI.
    Extract the following structured information from the clinical consultation notes provided below:
    1. Dosha imbalance (e.g., Vata, Pitta, Kapha, or combinations like Vata-Pitta)
    2. Prescribed herbs and their quantities. All quantities MUST be converted to strictly integer grams.

    Language Support:
    The notes might be written in Malayalam, Manglish (Malayalam written in English script), or English.
    Translate and interpret the context accurately.

    Herb Matching constraint:
    We ONLY stock the following herbs in our inventory:
    {', '.join(context.formulary)}

    Current stock levels for this clinic (informational only):
{stock_lines}

    Instructions for herbs:
    - Before including any herb in your final answer, call check_herb_alias with the name exactly as it appears
      in the notes, and use the canonical_name it returns instead of guessing a mapping yourself.
    - Call check_herb_alias for a given herb AT MOST TWICE: once with the name as it appears in the notes, and,
      only if that call returns known: false, at most one more time with a different plausible spelling or
      transliteration of the SAME herb. Do not keep retrying beyond that -- every call costs real API quota.
    - CRITICAL: never include a herb in your final answer under a name of your own guessing. Only use a
      canonical_name that some call to check_herb_alias actually returned with known: true. If every attempt
      for a herb returns known: false, that herb is not something this clinic stocks under any name you tried --
      leave it out of prescribed_herbs entirely rather than including your best guess. A doctor reviewing a
      shorter-than-expected list will notice and can add it back manually; a wrong herb slipping through because
      it sounded plausible is the failure mode this whole tool exists to prevent.
    - If a herb is mentioned in two languages/scripts in the same note (e.g. an English name and its Malayalam
      name together), that is still one herb: check whichever form you see first, and only try the other form if
      the first attempt returns known: false.
    - The stock levels above are informational only -- a low or insufficient reading must NOT change your
      answer. Report the prescription exactly as the doctor described it. Enforcing stock limits happens later,
      as a separate step a human confirms; that is not your job here.
    - Include all prescribed herbs.
    - Ensure quantities are strictly integers representing grams.

    Privacy notice:
    The patient's identity has already been anonymized before reaching you. Any text
    matching the pattern [[PAT_xxxxxxxx]] is an opaque placeholder standing in for a
    real name, phone number, or ID -- it is NOT clinical information. Ignore such
    placeholders for the purposes of dosha/herb extraction; do not attempt to guess,
    translate, or reconstruct what they stand for, and do not include them in your
    output.

    Output format:
    Once you are done using tools to verify herb names, respond with ONLY a single valid JSON object, and
    nothing else (no markdown fences, no commentary before or after it), matching exactly this shape:
    {_OUTPUT_SHAPE}

    Clinical Notes:
    \"\"\"
    {notes_text}
    \"\"\"
    """


def build_repair_prompt(invalid_text: str, error: str) -> str:
    """The one allowed repair turn (report section 7: MAX_REASONING_CALLS=2
    -- one initial, one repair on malformed JSON). Sent as a follow-up
    turn in the same conversation, so the model still has the original
    notes and any tool results in context; it does not re-send the
    clinical notes or re-state the formulary."""
    return f"""
    Your previous reply could not be parsed as the required JSON object.

    Your reply was:
    \"\"\"
    {invalid_text}
    \"\"\"

    The parsing error was: {error}

    Reply again with ONLY a single valid JSON object, no markdown fences, no commentary,
    matching exactly this shape:
    {_OUTPUT_SHAPE}
    """
