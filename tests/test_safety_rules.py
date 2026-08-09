from safety_rules import check_medical_emergency, check_safety, is_safety_triggering


def test_direct_current_first_person():
    signal, _ = check_safety("I want to kill myself, I don't know what else to do.")
    assert signal == "direct_self_harm"
    assert is_safety_triggering(signal)


def test_imminent_danger_with_urgency_marker():
    signal, _ = check_safety("I want to kill myself and I have a plan, I'm going to do it tonight.")
    assert signal == "imminent_danger"
    assert is_safety_triggering(signal)


def test_negation_does_not_trigger():
    signal, _ = check_safety("I am not suicidal, I just needed to vent about today.")
    assert signal == "none"
    assert not is_safety_triggering(signal)


def test_third_person_does_not_trigger():
    signal, _ = check_safety("My coworker told me she wanted to kill herself last week.")
    assert signal == "none"


def test_historical_past_tense_with_negation_does_not_trigger():
    signal, _ = check_safety("Years ago I don't want to kill myself anymore, I'm in a better place now.")
    assert signal == "none"


def test_ambiguous_hopelessness():
    signal, _ = check_safety("I can't go on like this, everything feels like too much.")
    assert signal == "ambiguous"
    assert not is_safety_triggering(signal)


def test_no_signal_on_neutral_text():
    signal, _ = check_safety("Work was busy today and I went for a walk after dinner.")
    assert signal == "none"


def test_empty_journal():
    signal, _ = check_safety("")
    assert signal == "none"


def test_quoted_phrase_is_skipped():
    signal, _ = check_safety('The lyric goes "I want to kill myself" and it has been stuck in my head all day.')
    assert signal == "none"


# -- medical emergency (independent of self-harm safety) --------------------

def test_chest_pain_triggers_medical_emergency():
    triggered, phrase = check_medical_emergency(
        "Chest pain started an hour ago and my left arm feels strange.")
    assert triggered is True
    assert "chest pain" in phrase.lower()


def test_cant_breathe_triggers_medical_emergency():
    triggered, _ = check_medical_emergency("I can't breathe and it's getting worse.")
    assert triggered is True


def test_ordinary_physical_discomfort_does_not_trigger_medical_emergency():
    triggered, _ = check_medical_emergency(
        "My back hurts from sitting all day and I have a mild headache.")
    assert triggered is False


def test_historical_medical_language_does_not_trigger():
    triggered, _ = check_medical_emergency("I had chest pain last year but it turned out to be nothing.")
    assert triggered is False


def test_third_person_medical_language_does_not_trigger():
    triggered, _ = check_medical_emergency("My dad had chest pain and had to go to the ER last week.")
    assert triggered is False


def test_negated_medical_language_does_not_trigger():
    triggered, _ = check_medical_emergency("I don't have chest pain, just some general tightness from stress.")
    assert triggered is False


def test_medical_and_self_harm_language_are_independent():
    """A journal entry can trigger both, independently - neither implies the other."""
    text = "I took a bunch of pills because I want to die and now I can't breathe."
    medical_triggered, _ = check_medical_emergency(text)
    safety_signal, _ = check_safety(text)
    assert medical_triggered is True
    assert is_safety_triggering(safety_signal)


def test_self_harm_alone_does_not_trigger_medical_emergency():
    triggered, _ = check_medical_emergency("I want to kill myself, I don't know what else to do.")
    assert triggered is False


def test_medical_emergency_alone_does_not_trigger_self_harm_signal():
    signal, _ = check_safety("Chest pain started an hour ago and my left arm feels strange.")
    assert signal == "none"
