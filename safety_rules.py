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

# Past-tense / resolved framing - "chest pain last year" is not a current
# emergency. Checked sentence-wide (not just a preceding window) since these
# markers commonly follow the phrase rather than precede it.
HISTORICAL_MARKERS = re.compile(
    r"\b(last year|last month|last week|years ago|months ago|weeks ago|"
    r"used to|in the past|previously|turned out to be (nothing|fine|okay)|"
    r"resolved|no longer (an issue|a problem))\b",
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
    r"took too many .* by accident",
]

_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")

DIRECT_RE = re.compile("|".join(DIRECT_PHRASES), re.I)
AMBIGUOUS_RE = re.compile("|".join(AMBIGUOUS_PHRASES), re.I)
MEDICAL_EMERGENCY_RE = re.compile("|".join(MEDICAL_EMERGENCY_PHRASES), re.I)


def _is_quoted(sentence, match_start):
    """Best-effort: skip matches that fall inside quotation marks, since
    those are more likely someone else's words being quoted."""
    before = sentence[:match_start]
    return before.count('"') % 2 == 1 or before.count("“") > before.count("”")


def _negated_before(sentence, match_start, window_words=5):
    """Negation only counts if it appears in the few words immediately
    before the matched phrase - not anywhere in the sentence. A comma-joined
    clause like "I want to kill myself, I don't know what else to do" must
    still fire; only "I don't want to kill myself" should be negated."""
    prefix_words = re.findall(r"\S+", sentence[:match_start])
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

        direct_match = DIRECT_RE.search(sentence)
        if (direct_match and not _is_quoted(sentence, direct_match.start())
                and not _negated_before(sentence, direct_match.start())):
            if URGENCY_MARKERS.search(sentence):
                return "imminent_danger", direct_match.group(0)
            best_signal, best_phrase = "direct_self_harm", direct_match.group(0)
            continue

        if best_signal == "none":
            ambiguous_match = AMBIGUOUS_RE.search(sentence)
            if (ambiguous_match and not _is_quoted(sentence, ambiguous_match.start())
                    and not _negated_before(sentence, ambiguous_match.start())):
                best_signal, best_phrase = "ambiguous", ambiguous_match.group(0)

    return best_signal, best_phrase


def is_safety_triggering(signal):
    return signal in ("direct_self_harm", "imminent_danger")


def _is_first_person_subject(sentence):
    """Stricter than the plain FIRST_PERSON check above: "my" only counts as
    first-person if it isn't immediately followed by a relation noun (which
    almost always introduces someone else, e.g. "my dad had chest pain")."""
    if re.search(r"\b(i|i'm|im|i've|ive|i'd|id|i'll|ill)\b", sentence, re.I):
        return True
    return bool(re.search(r"\bmy\b", sentence, re.I)) and not THIRD_PARTY_AFTER_MY.search(sentence)


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
        if HISTORICAL_MARKERS.search(sentence):
            continue

        match = MEDICAL_EMERGENCY_RE.search(sentence)
        if (match and not _is_quoted(sentence, match.start())
                and not _negated_before(sentence, match.start())):
            return True, match.group(0)

    return False, None
