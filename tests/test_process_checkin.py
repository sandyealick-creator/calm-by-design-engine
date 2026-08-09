import datetime as dt

import main
from routing_config import (
    ROUTE_GROUNDING_SUPPORT,
    ROUTE_HEIGHTENED_SUPPORT,
    ROUTE_MEDICAL_EMERGENCY,
    ROUTE_POSITIVE_PROGRESS,
    ROUTE_SAFETY,
    ROUTE_STEADY,
)

POSITIVE = dict(physical=2, anxiety=2, energy=9, sleep=9)   # score 8  -> POSITIVE_REGULATED
STEADY = dict(physical=3, anxiety=3, energy=8, sleep=8)     # score 12 -> STEADY
GROUNDING = dict(physical=5, anxiety=5, energy=6, sleep=6)  # score 20 -> GROUNDING_SUPPORT
HEIGHTENED = dict(physical=8, anxiety=8, energy=3, sleep=3)  # score 32 -> HEIGHTENED_SUPPORT


def _checkin(client_rec, scores, journal, submission_id, source="Flask Web"):
    return main.process_checkin(
        client_rec, scores["physical"], scores["anxiety"], scores["energy"],
        scores["sleep"], journal, submission_id, source,
    )


def test_positive_progress_advances_curriculum(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="positive", progress_signal=True)

    result = _checkin(client_rec, POSITIVE, "Great day, proud of myself.", "sub-1")

    assert result["response_route"] == ROUTE_POSITIVE_PROGRESS
    updated = fake_airtable.get(main.T_CLIENTS, client_rec["id"])
    assert updated["fields"][main.F_CLIENT["week"]] == 2  # advanced from week 1


def test_second_positive_entry_same_week_does_not_advance_again(fake_airtable, mock_gemini, make_client_record):
    """Max one curriculum advance per scheduled week, regardless of how many
    POSITIVE_PROGRESS check-ins happen inside that window."""
    client_rec = make_client_record()
    mock_gemini.set(sentiment="positive", progress_signal=True)

    _checkin(client_rec, POSITIVE, "Good day one.", "sub-a")
    after_first = fake_airtable.get(main.T_CLIENTS, client_rec["id"])
    assert after_first["fields"][main.F_CLIENT["week"]] == 2

    _checkin(client_rec, POSITIVE, "Good day two, same week.", "sub-b")
    after_second = fake_airtable.get(main.T_CLIENTS, client_rec["id"])
    assert after_second["fields"][main.F_CLIENT["week"]] == 2  # unchanged


def test_idempotent_replay_same_submission_id_no_duplicate(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="neutral")

    result1 = _checkin(client_rec, STEADY, "Same entry twice.", "sub-dup")
    logs_after_first = fake_airtable.find_all(main.T_LOGS)
    assert len(logs_after_first) == 1

    result2 = _checkin(client_rec, STEADY, "Same entry twice.", "sub-dup")
    logs_after_second = fake_airtable.find_all(main.T_LOGS)

    assert len(logs_after_second) == 1  # no new Daily Log created on retry
    assert result1["response_route"] == result2["response_route"]
    assert result1["score"] == result2["score"]


def test_two_legitimate_same_day_checkins_are_independent(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()

    mock_gemini.set(sentiment="positive")
    result1 = _checkin(client_rec, POSITIVE, "Morning entry, feeling good.", "sub-morning")

    mock_gemini.set(sentiment="distressed", distress_signal=True)
    result2 = _checkin(client_rec, HEIGHTENED, "Afternoon entry, things fell apart.", "sub-afternoon")

    logs = fake_airtable.find_all(main.T_LOGS)
    assert len(logs) == 2
    assert result1["response_route"] != result2["response_route"]
    assert result2["response_route"] == ROUTE_HEIGHTENED_SUPPORT


def test_high_score_alone_is_not_safety_route(fake_airtable, mock_gemini, make_client_record):
    """A maximal support score with no safety signal must land in
    HEIGHTENED_SUPPORT, never SAFETY_ROUTE - the score never implies risk by itself."""
    client_rec = make_client_record()
    mock_gemini.set(sentiment="distressed", distress_signal=True, safety_signal="none")

    result = _checkin(client_rec, HEIGHTENED, "Rough week, exhausted, everything hurts.", "sub-high")
    assert result["response_route"] == ROUTE_HEIGHTENED_SUPPORT


def test_gemini_safety_signal_triggers_safety_route(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="distressed", distress_signal=True, safety_signal="direct_self_harm")

    result = _checkin(client_rec, STEADY, "neutral text, gemini flags it", "sub-safety-gemini")
    assert result["response_route"] == ROUTE_SAFETY
    alerts = fake_airtable.find_all(main.T_CRISIS)
    assert len(alerts) == 1

    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert assess["fields"][main.F_ASSESS["safety_trigger_source"]] == "gemini"


def test_keyword_rule_triggers_safety_route_even_when_gemini_says_none(
    fake_airtable, mock_gemini, make_client_record
):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="distressed", safety_signal="none")

    result = _checkin(client_rec, STEADY, "I want to kill myself, I mean it.", "sub-safety-kw")
    assert result["response_route"] == ROUTE_SAFETY
    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert assess["fields"][main.F_ASSESS["safety_trigger_source"]] == "keyword_rule"


def test_both_signals_recorded_as_both(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(safety_signal="direct_self_harm")

    _checkin(client_rec, STEADY, "I want to kill myself.", "sub-safety-both")
    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert assess["fields"][main.F_ASSESS["safety_trigger_source"]] == "both"


def test_gemini_unavailable_falls_back_and_keyword_rule_still_catches_safety(
    fake_airtable, mock_gemini, make_client_record
):
    client_rec = make_client_record()
    mock_gemini.fail(RuntimeError("simulated Gemini outage"))

    result = _checkin(client_rec, STEADY, "I want to kill myself tonight.", "sub-fallback-safety")
    assert result["fallback_mode"] is True
    assert result["response_route"] == ROUTE_SAFETY
    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert assess["fields"][main.F_ASSESS["fallback_mode"]] is True
    assert assess["fields"][main.F_ASSESS["safety_trigger_source"]] == "keyword_rule"


def test_gemini_unavailable_still_routes_by_score(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.fail(RuntimeError("simulated Gemini outage"))

    result = _checkin(client_rec, GROUNDING, "Rough day, nothing dangerous though.", "sub-fallback-score")
    assert result["fallback_mode"] is True
    assert result["response_route"] == ROUTE_GROUNDING_SUPPORT


def test_malformed_gemini_response_triggers_fallback(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set_raw({"sentiment": "not-a-real-value"})  # missing required fields, invalid enum

    result = _checkin(client_rec, STEADY, "some entry", "sub-malformed")
    assert result["fallback_mode"] is True


def test_all_five_elements_selectable(fake_airtable, mock_gemini, make_client_record):
    for element in ("earth", "air", "fire", "water", "spirit"):
        client_rec = make_client_record(email=f"{element}@example.com")
        mock_gemini.set(sentiment="distressed", suggested_element=element)
        result = _checkin(client_rec, GROUNDING, f"entry suggesting {element}", f"sub-{element}")
        assert result["response_route"] == ROUTE_GROUNDING_SUPPORT
        assert result["element_name"] == element.capitalize()
        assert result["element"] is not None
        assert result["element"]["title"]


def test_anti_repeat_avoids_last_used_element(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record(**{main.F_CLIENT["buffer_element"]: "Earth"})
    mock_gemini.set(sentiment="distressed", suggested_element="earth")

    result = _checkin(client_rec, GROUNDING, "entry", "sub-antirepeat")
    assert result["element_name"] != "Earth"


def test_score_boundary_11_vs_12_routes_differently(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="neutral")

    result_11 = _checkin(client_rec, dict(physical=3, anxiety=3, energy=9, sleep=8),
                         "boundary test 11", "sub-b11")
    assert result_11["score"] == 11
    assert result_11["tier"] == "POSITIVE_REGULATED"

    result_12 = _checkin(client_rec, STEADY,
                         "boundary test 12", "sub-b12")
    assert result_12["score"] == 12
    assert result_12["tier"] == "STEADY"


# -- medical-emergency override -----------------------------------------

def test_ordinary_physical_discomfort_is_not_medical_emergency(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="neutral", medical_emergency_signal=False)

    result = _checkin(client_rec, STEADY, "My back is a little sore from yard work.", "sub-mild")
    assert result["response_route"] not in (ROUTE_SAFETY, ROUTE_MEDICAL_EMERGENCY)
    assert result["medical_emergency_triggered"] is False


def test_heightened_support_without_medical_language_unaffected(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="distressed", distress_signal=True, medical_emergency_signal=False)

    result = _checkin(client_rec, HEIGHTENED, "Rough week, exhausted, everything hurts.", "sub-heightened")
    assert result["response_route"] == ROUTE_HEIGHTENED_SUPPORT
    assert result["medical_emergency_triggered"] is False


def test_clear_medical_emergency_language_routes_to_medical_emergency(
    fake_airtable, mock_gemini, make_client_record
):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="distressed", medical_emergency_signal=True)

    result = _checkin(client_rec, STEADY, "Chest pain started an hour ago, my left arm feels strange.",
                      "sub-medical")
    assert result["response_route"] == ROUTE_MEDICAL_EMERGENCY
    assert result["medical_emergency_triggered"] is True
    assert result["element"] is None  # no grounding practice as the primary/only response

    alerts = fake_airtable.find_all(main.T_CRISIS)
    assert len(alerts) == 1
    assert "MEDICAL_EMERGENCY" in alerts[0]["fields"][main.F_CRISIS["alert_id"]]

    # No curriculum movement on a medical emergency.
    updated = fake_airtable.get(main.T_CLIENTS, client_rec["id"])
    assert updated["fields"][main.F_CLIENT["week"]] == client_rec["fields"][main.F_CLIENT["week"]]


def test_medical_emergency_keyword_backstop_works_without_gemini(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(medical_emergency_signal=False)  # Gemini misses it

    result = _checkin(client_rec, STEADY, "I can't breathe and it's getting worse.", "sub-medical-kw")
    assert result["response_route"] == ROUTE_MEDICAL_EMERGENCY


def test_ambiguous_self_harm_language_does_not_trigger_either_override(
    fake_airtable, mock_gemini, make_client_record
):
    client_rec = make_client_record()
    mock_gemini.set(sentiment="distressed", safety_signal="ambiguous", medical_emergency_signal=False)

    result = _checkin(client_rec, STEADY, "I can't go on like this, everything feels like too much.",
                      "sub-ambiguous")
    assert result["response_route"] not in (ROUTE_SAFETY, ROUTE_MEDICAL_EMERGENCY)
    assert result["medical_emergency_triggered"] is False


def test_direct_self_harm_language_still_routes_to_safety(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(safety_signal="direct_self_harm", medical_emergency_signal=False)

    result = _checkin(client_rec, STEADY, "I want to kill myself.", "sub-direct")
    assert result["response_route"] == ROUTE_SAFETY
    assert result["medical_emergency_triggered"] is False


def test_imminent_self_harm_danger_still_routes_to_safety(fake_airtable, mock_gemini, make_client_record):
    client_rec = make_client_record()
    mock_gemini.set(safety_signal="imminent_danger", medical_emergency_signal=False)

    result = _checkin(client_rec, STEADY, "I have a plan and I'm doing it tonight.", "sub-imminent")
    assert result["response_route"] == ROUTE_SAFETY
    assert result["medical_emergency_triggered"] is False


def test_simultaneous_medical_emergency_and_self_harm(fake_airtable, mock_gemini, make_client_record):
    """Self-harm takes precedence for routing/curriculum, but the medical
    signal is still recorded and rendered (see result.html) - 911 guidance
    must appear alongside 988, not be dropped."""
    client_rec = make_client_record()
    mock_gemini.set(safety_signal="direct_self_harm", medical_emergency_signal=True)

    result = _checkin(client_rec, STEADY, "I took a bunch of pills because I want to die and now I can't breathe.",
                      "sub-both")
    assert result["response_route"] == ROUTE_SAFETY
    assert result["medical_emergency_triggered"] is True

    alerts = fake_airtable.find_all(main.T_CRISIS)
    assert len(alerts) == 1
    assert "SELF_HARM_AND_MEDICAL_EMERGENCY" in alerts[0]["fields"][main.F_CRISIS["alert_id"]]


def test_medical_emergency_replay_preserves_medical_flag(fake_airtable, mock_gemini, make_client_record):
    """Idempotent retry of a medical-emergency submission must still show
    the medical banner, not silently downgrade to a generic result."""
    client_rec = make_client_record()
    mock_gemini.set(medical_emergency_signal=True)

    result1 = _checkin(client_rec, STEADY, "Chest pain and my arm feels strange.", "sub-medical-replay")
    result2 = _checkin(client_rec, STEADY, "Chest pain and my arm feels strange.", "sub-medical-replay")
    assert result1["medical_emergency_triggered"] is True
    assert result2["medical_emergency_triggered"] is True
    assert result1["response_route"] == result2["response_route"] == ROUTE_MEDICAL_EMERGENCY
