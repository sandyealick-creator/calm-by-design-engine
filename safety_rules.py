"""
Calm by Design - deterministic safety backstop
================================================
Two independent, narrow, high-recall keyword/pattern checks:

  check_safety()            - direct current first-person self-harm/suicide
                               language ("direct_self_harm", "imminent_danger").
  check_medical_emergency()  - clear current first-person acute medical
                               emergency language (chest pain, can't breathe,
                               stroke signs, severe bleeding, anaphylaxis,
                               seizure, loss of consciousness).

Neither is a clinical screener or a diagnostic engine, and neither is
presented to participants as one. Both run on EVERY submission alongside
Gemini's own classification (routing uses whichever signal is strongest per
category), and are the only signals available when Gemini is unavailable or
returns an invalid response. The two checks are intentionally independent -
a journal entry can trigger one, both, or neither, and medical-emergency
routing must never be inferred from self-harm language or vice versa.

They deliberately stay narrow: curated phrase lists, a same-sentence
negation window, and a same-sentence first-person-subject check, so they do
not fire on negated statements, third-person or historical mentions, or
quotations of someone else's words. They are backstops, not a replacement
for Gemini's more nuanced classification.
"""

import re

FIRST_PERSON = re.compile(r"\b(i|i'm|im|i've|ive|i'd|id|i'll|ill|my)\b", re.I)

NEGATION = re.compile(
    r"\b(not|never|n't|no longer|don't|dont|didn't|didnt|wasn't|wasnt|"
    r"isn't|isnt|won't|wont|wouldn't|wouldnt|stopped|used to|no longer)\b",
    re.I,
)

# Direct, current-tense, first-person self-harm/suicide phrases.
DIRECT_PHRASES = [
    r"kill(ing)? myself",
    r"end(ing)? my life",
    r"hurt(ing)? myself",
    r"take my (own )?life",
    r"i(?:\s+am|['’]m|m)\s+(?:feeling\s+)?suicidal",
    r"i\s+feel\s+suicidal",
    r"want to die",
    r"wish i (was|were) dead",
    r"suicide is the (only )?(answer|option|way)",
    r"planning to (kill myself|end it|end my life)",
]

# Vaguer first-person distress phrases -> ambiguous, not direct.
AMBIGUOUS_PHRASES = [
    r"don't want to (be here|exist) anymore",
    r"can't go on",
    r"want it all to stop",
    r"better off (dead|without me)",
    r"no reason to (live|keep going)",
]

# Urgency markers that, combined with a direct phrase in the same sentence,
# indicate imminent danger rather than a general direct statement.
URGENCY_MARKERS = re.compile(
    r"\b(right now|tonight|today|already|about to|have a plan|"
    r"going to do it|this is it)\b",
    re.I,
)

# "my <relation>" almost always introduces someone else, not the participant
# ("my dad had chest pain") - unlike "my chest/arm/etc" which is a normal
# first-person body reference. Used only by check_medical_emergency(), since
# its phrases (unlike the self-harm phrases above) don't already embed a
# first-person object like "myself".
THIRD_PARTY_AFTER_MY = re.compile(
    r"\bmy (dad|mom|mother|father|husband|wife|partner|friend|sister|brother|"
    r"son|daughter|grandmother|grandfather|grandma|grandpa|coworker|boss|"
    r"neighbor|doctor|therapist|coach)\b",
    re.I,
)

# Past-tense framing shared by both backstops. The contextual helper below
# evaluates these markers relative to the matched phrase so unrelated history
# does not suppress a separate, current statement in the same sentence.
HISTORICAL_MARKERS = re.compile(
    r"\b(last year|last month|last week|years ago|months ago|weeks ago|"
    r"used to|in the past|previously|turned out to be (nothing|fine|okay)|"
    r"resolved|no longer (an issue|a problem))\b",
    re.I,
)

# Clear post-statement resolution. This stays intentionally narrow: vague
# improvement does not cancel a current safety statement, while explicit
# language that the condition stopped or that the participant is safe/fine
# now prevents a historical mention from being treated as current.
RESOLVED_AFTER = re.compile(
    r"\b(?:but|and)\b.{0,120}\b(?:"
    r"(?:it|that|the feeling|the pain|the bleeding) (?:has )?"
    r"(?:stopped|passed|resolved|ended|gone away)|"
    r"i (?:do not|don't|dont) (?:feel that way|want to die|want to hurt myself|"
    r"want to kill myself) (?:now|anymore)|"
    r"i (?:am|feel) (?:safe|fine|okay|better) now|"
    r"i am in a (?:much )?better place now)\b",
    re.I,
)

CURRENT_AFTER_HISTORY = re.compile(
    r"\b(?:but|however|yet)\b.{0,30}\b(?:now|today|tonight|right now)\b|"
    r"(?:\b(?:and|but|however|yet)\b|[,;]).{0,50}\bi\s+"
    r"(?:want|plan|intend|feel|am)\b",
    re.I,
)

PAST_TENSE_BEFORE_MATCH = re.compile(
    r"\b(?:wanted|wanting|tried|planned|considered|had|experienced|felt|"
    r"was having|were having)\s+(?:to\s+)?$",
    re.I,
)

# Reporting contexts that make first-person words part of someone else's
# statement rather than the participant's own current statement.
THIRD_PARTY_REPORT = re.compile(
    r"\b(?:my (?:dad|mom|mother|father|husband|wife|partner|friend|sister|"
    r"brother|son|daughter|grandmother|grandfather|grandma|grandpa|coworker|"
    r"boss|neighbor|doctor|therapist|coach)|he|she|they)\b.{0,50}\b"
    r"(?:said|says|told|texted|wrote|reported|thinks?|believes?)\b|"
    r"\b(?:i\s+)?heard\s+(?:my (?:dad|mom|mother|father|husband|wife|partner|"
    r"friend|sister|brother|son|daughter|grandmother|grandfather|grandma|"
    r"grandpa|coworker|boss|neighbor|doctor|therapist|coach)|him|her|them)\s+"
    r"(?:say|saying)\b",
    re.I,
)

# A reporting frame only governs the clause that follows it. Contrast words,
# semicolons, and explicit "and/then I" transitions begin a new clause whose
# first-person statement must be evaluated independently.
REPORT_SCOPE_BOUNDARY = re.compile(
    r";|\b(?:but|however|yet|whereas)\b|\b(?:and|then)\b(?=\s+i\b)",
    re.I,
)

# Clear, unambiguous acute medical emergency language - classic ER red-flag
# presentations only. Deliberately narrow: this is a safety backstop, not a
# symptom checker or diagnostic tool, and must not attempt to interpret
# ordinary aches, pains, or wellness-scale physical symptoms as emergencies.
MEDICAL_EMERGENCY_PHRASES = [
    r"chest pain",
    r"chest (feels|is) tight",
    r"can'?t breathe",
    r"can'?t catch my breath",
    r"trouble breathing",
    r"difficulty breathing",
    r"having a stroke",
    r"face is drooping",
    r"slurred speech",
    r"sudden numbness (down |in )?(one side|my (left|right) side)",
    r"severe bleeding",
    r"bleeding (that )?(won'?t|wont) stop",
    r"throat is closing",
    r"having a seizure",
    r"having an allergic reaction",
    r"passing out",
    r"losing consciousness",
    r"accidentally overdosed",
    r"took too many .*? by accident",
]

_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")

DIRECT_RE = re.compile("|".join(DIRECT_PHRASES), re.I)
AMBIGUOUS_RE = re.compile("|".join(AMBIGUOUS_PHRASES), re.I)
MEDICAL_EMERGENCY_RE = re.compile("|".join(MEDICAL_EMERGENCY_PHRASES), re.I)


def _is_quoted(sentence, match_start):
    """Best-effort: skip matches that fall inside quotation marks, since
    those are more likely someone else's words being quoted."""
    before = sentence[:match_start]
    ascii_single_quotes = re.findall(r"(?<!\w)'|'(?!\w)", before)
    curly_single_closes = re.findall(r"(?<!\w)’|’(?!\w)", before)
    return (
        before.count('"') % 2 == 1
        or before.count("“") > before.count("”")
        or len(ascii_single_quotes) % 2 == 1
        or before.count("‘") > len(curly_single_closes)
    )


def _context_prefix(sentence, match_start):
    """Return only the current clause's text before a candidate match."""
    prefix = sentence[:match_start]
    boundaries = list(REPORT_SCOPE_BOUNDARY.finditer(prefix))
    return prefix[boundaries[-1].end():] if boundaries else prefix


def _is_third_party_report(sentence, match_start):
    """Return true when the matched words occur inside a reporting frame
    whose speaker/subject is explicitly someone other than the participant."""
    return bool(THIRD_PARTY_REPORT.search(_context_prefix(sentence, match_start)))


def _is_historical_or_resolved(sentence, match):
    """Recognize a narrow set of clearly non-current contexts around a
    matched phrase without letting an earlier history clause suppress a new,
    explicitly current statement later in the same sentence."""
    prefix = sentence[:match.start()]
    context_prefix = _context_prefix(sentence, match.start())
    suffix = sentence[match.end():]

    historical_before = list(HISTORICAL_MARKERS.finditer(context_prefix))
    if historical_before:
        after_marker = context_prefix[historical_before[-1].end():]
        if not CURRENT_AFTER_HISTORY.search(after_marker):
            return True

    historical_after = HISTORICAL_MARKERS.search(suffix)
    if historical_after:
        between = suffix[:historical_after.start()]
        if (len(re.findall(r"\b\w+\b", between)) <= 5
                and PAST_TENSE_BEFORE_MATCH.search(prefix)):
            return True

    return bool(RESOLVED_AFTER.search(suffix))


def _negated_before(sentence, match_start, window_words=5):
    """Negation only counts if it appears in the few words immediately
    before the matched phrase - not anywhere in the sentence. A comma-joined
    clause like "I want to kill myself, I don't know what else to do" must
    still fire; only "I don't want to kill myself" should be negated."""
    prefix_words = re.findall(r"\S+", _context_prefix(sentence, match_start))
    window = " ".join(prefix_words[-window_words:])
    return bool(NEGATION.search(window))


def check_safety(journal_text):
    """Returns (signal, matched_phrase) where signal is one of
    'none', 'ambiguous', 'direct_self_harm', 'imminent_danger'."""
    if not journal_text:
        return "none", None

    best_signal = "none"
    best_phrase = None

    for sentence in _SENTENCE_SPLIT.split(journal_text):
        if not sentence.strip():
            continue
        if not FIRST_PERSON.search(sentence):
            continue

        actionable_direct = False
        for direct_match in DIRECT_RE.finditer(sentence):
            if (_is_quoted(sentence, direct_match.start())
                    or _is_third_party_report(sentence, direct_match.start())
                    or _is_historical_or_resolved(sentence, direct_match)
                    or _negated_before(sentence, direct_match.start())):
                continue

            actionable_direct = True
            if URGENCY_MARKERS.search(sentence):
                return "imminent_danger", direct_match.group(0)
            if best_signal != "direct_self_harm":
                best_signal, best_phrase = "direct_self_harm", direct_match.group(0)

        if actionable_direct:
            continue

        if best_signal == "none":
            for ambiguous_match in AMBIGUOUS_RE.finditer(sentence):
                if (_is_quoted(sentence, ambiguous_match.start())
                        or _negated_before(sentence, ambiguous_match.start())):
                    continue
                best_signal, best_phrase = "ambiguous", ambiguous_match.group(0)
                break

    return best_signal, best_phrase


def is_safety_triggering(signal):
    return signal in ("direct_self_harm", "imminent_danger")


def _is_first_person_subject(sentence):
    """Stricter than the plain FIRST_PERSON check above: "my" only counts as
    first-person if it isn't immediately followed by a relation noun (which
    almost always introduces someone else, e.g. "my dad had chest pain")."""
    if re.search(r"\b(i|i'm|im|i've|ive|i'd|id|i'll|ill)\b", sentence, re.I):
        return True
    return any(
        not THIRD_PARTY_AFTER_MY.match(sentence, match.start())
        for match in re.finditer(r"\bmy\b", sentence, re.I)
    )


def check_medical_emergency(journal_text):
    """Returns (triggered: bool, matched_phrase). Independent of
    check_safety() - a journal entry can trigger this, check_safety(), both,
    or neither. This intentionally does not attempt to grade severity or
    identify a condition; it only flags whether clear emergency language is
    present, which is all the participant-facing override needs."""
    if not journal_text:
        return False, None

    for sentence in _SENTENCE_SPLIT.split(journal_text):
        if not sentence.strip():
            continue
        if not _is_first_person_subject(sentence):
            continue
        for match in MEDICAL_EMERGENCY_RE.finditer(sentence):
            if (_is_quoted(sentence, match.start())
                    or _is_third_party_report(sentence, match.start())
                    or _is_historical_or_resolved(sentence, match)
                    or _negated_before(sentence, match.start())):
                continue
            return True, match.group(0)

    return False, None
