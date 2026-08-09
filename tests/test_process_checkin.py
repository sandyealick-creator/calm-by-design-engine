import datetime as dt

import pytest

import main
from conftest import submission_id_for
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
    if submission_id.startswith("sub-"):
        submission_id = submission_id_for(submission_id)
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


def test_replay_is_scoped_to_authenticated_participant(
    fake_airtable, mock_gemini, make_client_record
):
    participant_a = make_client_record(email="a@example.com", name="Participant A")
    participant_b = make_client_record(email="b@example.com", name="Participant B")
    replay_id = submission_id_for("cross-participant-replay")

    mock_gemini.set(sentiment="distressed", distress_signal=True, summary="private b summary")
    result_b = main.process_checkin(
        participant_b, STEADY["physical"], STEADY["anxiety"], STEADY["energy"],
        STEADY["sleep"], "Private B journal", replay_id, "Flask Web")
    assert result_b["summary"] == "private b summary"

    mock_gemini.set(sentiment="neutral", summary="participant a summary")
    result_a = main.process_checkin(
        participant_a, STEADY["physical"], STEADY["anxiety"], STEADY["energy"],
        STEADY["sleep"], "Participant A journal", replay_id, "Flask Web")

    assert result_a["summary"] == "participant a summary"
    assert "private b" not in result_a["summary"]
    logs = fake_airtable.find_all(main.T_LOGS)
    assert len(logs) == 2
    assert {tuple(r["fields"][main.F_LOG["client"]]) for r in logs} == {
        (participant_a["id"],), (participant_b["id"],),
    }


def test_replay_rejects_unexpected_client_link_after_scoped_lookup(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    participant_a = make_client_record(email="a-link@example.com")
    participant_b = make_client_record(email="b-link@example.com")
    replay_id = submission_id_for("unexpected-client-link")
    foreign_log = fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: replay_id,
        main.F_LOG["client"]: [participant_b["id"]],
    })
    monkeypatch.setattr(main, "find_log_by_submission_id", lambda *_: foreign_log)
    mock_gemini.set(summary="owned result")

    result = main.process_checkin(
        participant_a, STEADY["physical"], STEADY["anxiety"], STEADY["energy"],
        STEADY["sleep"], "Owned journal", replay_id, "Flask Web")

    assert result["summary"] == "owned result"
    owned_logs = [
        r for r in fake_airtable.find_all(main.T_LOGS)
        if participant_a["id"] in (r["fields"].get(main.F_LOG["client"]) or [])
    ]
    assert len(owned_logs) == 1


def test_replay_rejects_assessment_linked_to_another_participant(
    fake_airtable, mock_gemini, make_client_record
):
    participant_a = make_client_record(email="a-assessment@example.com")
    participant_b = make_client_record(email="b-assessment@example.com")
    replay_id = submission_id_for("foreign-assessment-link")
    owned_log = fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: replay_id,
        main.F_LOG["client"]: [participant_a["id"]],
    })
    fake_airtable.create(main.T_ASSESS, {
        main.F_ASSESS["daily_log"]: [owned_log["id"]],
        main.F_ASSESS["client"]: [participant_b["id"]],
        main.F_ASSESS["reasoning"]: "private participant b summary",
        main.F_ASSESS["response_route"]: main.ROUTE_LABELS[ROUTE_SAFETY],
    })

    result = main.process_checkin(
        participant_a, STEADY["physical"], STEADY["anxiety"], STEADY["energy"],
        STEADY["sleep"], "Participant A ordinary journal", replay_id, "Flask Web")

    assert result["processing_incomplete"] is True
    assert result["summary"] == ""
    assert "private participant b" not in str(result).lower()
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1


def test_submission_id_validation_rejects_airtable_special_characters_before_lookup(
    monkeypatch,
):
    monkeypatch.setattr(
        main.requests, "get",
        lambda *args, **kwargs: pytest.fail("Malformed ID must not reach Airtable"),
    )
    with pytest.raises(ValueError, match="invalid submission_id"):
        main.find_log_by_submission_id("x'\\) OR(TRUE())", "rec-client")


def test_airtable_string_literal_escapes_quotes_and_backslashes():
    assert main._airtable_string_literal("a'b\\c") == "a\\'b\\\\c"


def test_production_replay_formula_is_client_scoped_and_escaped(monkeypatch):
    replay_id = submission_id_for("scoped-production-formula")
    client_id = "rec'client\\value"
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"records": [{
                "id": "rec-log",
                "fields": {main.F_LOG["client"]: [client_id]},
            }]}

    def fake_get(*args, **kwargs):
        captured.update(kwargs["params"])
        return Response()

    monkeypatch.setattr(main.requests, "get", fake_get)
    result = main.find_log_by_submission_id(replay_id, client_id)

    assert result["id"] == "rec-log"
    formula = captured["filterByFormula"]
    assert formula.startswith("AND(")
    assert replay_id in formula
    assert "rec\\'client\\\\value" in formula
    assert "ARRAYJOIN({Client})" in formula


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


def test_gemini_fallback_log_excludes_identity_content_and_raw_exception(
    fake_airtable, mock_gemini, make_client_record, caplog
):
    journal = "private fallback journal content"
    raw_exception = "private fallback exception detail"
    client_rec = make_client_record(
        email="fallback-private@example.com", name="Private Participant")
    mock_gemini.fail(RuntimeError(raw_exception))

    _checkin(client_rec, STEADY, journal, "sub-fallback-private-log")

    output = caplog.text
    assert "gemini_fallback" in output
    for prohibited in (
        "Private Participant", "fallback-private@example.com", journal,
        raw_exception, client_rec["id"],
    ):
        assert prohibited not in output


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
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["owner_notification_confirmed"] is False

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
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["owner_notification_confirmed"] is False

    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert main.F_ASSESS["owner_alert_status"] not in assess["fields"]


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
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["owner_notification_confirmed"] is False

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
    assert result2["checkin_saved"] is True
    assert result2["crisis_alert_created"] is True
    assert result2["owner_notification_confirmed"] is False


@pytest.mark.parametrize(
    "journal,expected_route,medical_triggered",
    [
        ("I want to die.", ROUTE_SAFETY, False),
        ("I have chest pain right now.", ROUTE_MEDICAL_EMERGENCY, True),
        ("I want to die and I can't breathe.", ROUTE_SAFETY, True),
    ],
)
def test_replay_log_lookup_failure_preserves_emergency_with_unknown_state(
    fake_airtable, make_client_record, monkeypatch, caplog,
    journal, expected_route, medical_triggered,
):
    sensitive_token = "test-sensitive-access-token"
    client_rec = make_client_record(**{
        main.F_CLIENT["access_token_hash"]: sensitive_token,
    })
    raw_exception = (
        f"private first replay lookup detail Jane Doe jane@example.com "
        f"{journal} {sensitive_token} {client_rec['id']}"
    )

    monkeypatch.setattr(
        main, "find_log_by_submission_id",
        lambda submission_id, client_record_id: (_ for _ in ()).throw(
            ConnectionError(raw_exception)),
    )
    monkeypatch.setattr(
        main, "at_create",
        lambda *args, **kwargs: pytest.fail("Replay lookup failure must not create records"),
    )
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Replay lookup failure must not call Gemini"),
    )
    monkeypatch.setattr(
        main.requests, "post",
        lambda *args, **kwargs: pytest.fail("Replay lookup failure must not call GHL"),
    )

    result = _checkin(client_rec, STEADY, journal, "sub-replay-log-lookup-failure")

    assert result["response_route"] == expected_route
    assert result["medical_emergency_triggered"] is medical_triggered
    assert result["checkin_saved"] is None
    assert result["crisis_alert_created"] is None
    assert result["owner_notification_confirmed"] is False
    assert result["http_status"] == 503
    assert result["later_processing_failed"] is False
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0
    log_output = caplog.text
    assert "Jane Doe" not in log_output
    assert "jane@example.com" not in log_output
    assert journal not in log_output
    assert sensitive_token not in log_output
    assert client_rec["id"] not in log_output
    assert raw_exception not in log_output


@pytest.mark.parametrize(
    "journal,expected_route,medical_triggered",
    [
        ("I want to die.", ROUTE_SAFETY, False),
        ("I have chest pain right now.", ROUTE_MEDICAL_EMERGENCY, True),
        ("I want to die and I can't breathe.", ROUTE_SAFETY, True),
    ],
)
def test_replay_assessment_lookup_failure_confirms_saved_but_not_alert_state(
    fake_airtable, make_client_record, monkeypatch, caplog,
    journal, expected_route, medical_triggered,
):
    sensitive_token = "test-sensitive-access-token"
    client_rec = make_client_record(**{
        main.F_CLIENT["access_token_hash"]: sensitive_token,
    })
    fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: submission_id_for(
            "sub-replay-assessment-lookup-failure"),
        main.F_LOG["client"]: [client_rec["id"]],
    })
    raw_exception = (
        f"private second replay lookup detail Jane Doe jane@example.com "
        f"{journal} {sensitive_token} {client_rec['id']}"
    )

    monkeypatch.setattr(
        main, "find_assessment_by_log_id",
        lambda log_id: (_ for _ in ()).throw(
            ConnectionError(raw_exception)),
    )
    monkeypatch.setattr(
        main, "at_create",
        lambda *args, **kwargs: pytest.fail("Assessment lookup failure must not create records"),
    )
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Assessment lookup failure must not call Gemini"),
    )
    monkeypatch.setattr(
        main.requests, "post",
        lambda *args, **kwargs: pytest.fail("Assessment lookup failure must not call GHL"),
    )

    result = _checkin(
        client_rec, STEADY, journal,
        "sub-replay-assessment-lookup-failure",
    )

    assert result["response_route"] == expected_route
    assert result["medical_emergency_triggered"] is medical_triggered
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is None
    assert result["owner_notification_confirmed"] is False
    assert result["http_status"] == 503
    assert result["later_processing_failed"] is False
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0
    log_output = caplog.text
    assert "Jane Doe" not in log_output
    assert "jane@example.com" not in log_output
    assert journal not in log_output
    assert sensitive_token not in log_output
    assert client_rec["id"] not in log_output
    assert raw_exception not in log_output


@pytest.mark.parametrize(
    "owner_status,crisis_alert_value,expected_alert_state",
    [
        ("sent", "Yes", True),
        ("failed", "Yes", None),
        (None, "Yes", True),
        (None, "No", False),
    ],
)
def test_replay_reconstructs_only_supported_alert_record_state(
    owner_status, crisis_alert_value, expected_alert_state
):
    fields = {
        main.F_ASSESS["response_route"]: main.ROUTE_LABELS[ROUTE_SAFETY],
        main.F_ASSESS["score_tier"]: main.TIER_LABELS["STEADY"],
        main.F_ASSESS["support_score"]: 12,
        main.F_ASSESS["trigger_reasons"]: "[]",
        main.F_ASSESS["fallback_mode"]: False,
        main.F_ASSESS["crisis_alert"]: crisis_alert_value,
    }
    if owner_status is not None:
        fields[main.F_ASSESS["owner_alert_status"]] = owner_status

    result = main._result_from_assessment({"fields": fields})

    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is expected_alert_state
    assert result["owner_notification_confirmed"] is False


@pytest.mark.parametrize(
    "journal,expected_route,medical_triggered",
    [
        ("I want to die.", ROUTE_SAFETY, False),
        ("I have chest pain right now.", ROUTE_MEDICAL_EMERGENCY, True),
        ("I want to die and I can't breathe.", ROUTE_SAFETY, True),
    ],
)
def test_first_daily_log_failure_preserves_each_deterministic_emergency(
    fake_airtable, make_client_record, monkeypatch, journal, expected_route,
    medical_triggered,
):
    client_rec = make_client_record()
    original_create = main.at_create

    def fail_daily_log(table, fields):
        if table == main.T_LOGS:
            raise ConnectionError("private Airtable detail")
        return original_create(table, fields)

    def gemini_must_not_run(*args, **kwargs):
        raise AssertionError("Gemini must not run for an early deterministic outage fallback")

    monkeypatch.setattr(main, "at_create", fail_daily_log)
    monkeypatch.setattr(main, "run_assessment", gemini_must_not_run)

    result = _checkin(client_rec, STEADY, journal, "sub-log-failure")

    assert result["response_route"] == expected_route
    assert result["medical_emergency_triggered"] is medical_triggered
    assert result["checkin_saved"] is False
    assert result["crisis_alert_created"] is True
    assert result["owner_notification_confirmed"] is False
    assert result["http_status"] == 503
    assert result["later_processing_failed"] is False
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 1
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 0


def test_crisis_alert_failure_does_not_replace_emergency_result(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    original_create = main.at_create

    def fail_crisis_alert(table, fields):
        if table == main.T_CRISIS:
            raise ConnectionError("private Crisis Alert detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_crisis_alert)
    result = _checkin(client_rec, STEADY, "I want to die.", "sub-alert-failure")

    assert result["response_route"] == ROUTE_SAFETY
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is False
    assert result["owner_notification_confirmed"] is False
    assert result["http_status"] == 200
    assert result["later_processing_failed"] is False
    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert assess["fields"][main.F_ASSESS["crisis_alert"]] == "No"
    assert main.F_ASSESS["owner_alert_status"] not in assess["fields"]


def test_daily_log_and_crisis_alert_failure_preserve_emergency_result(
    fake_airtable, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    original_create = main.at_create

    def fail_log_and_alert(table, fields):
        if table in (main.T_LOGS, main.T_CRISIS):
            raise ConnectionError("private persistence detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_log_and_alert)
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Gemini must not run during early outage fallback"),
    )

    result = _checkin(client_rec, STEADY, "I want to die.", "sub-both-fail")
    assert result["response_route"] == ROUTE_SAFETY
    assert result["checkin_saved"] is False
    assert result["crisis_alert_created"] is False
    assert result["http_status"] == 503
    assert result["later_processing_failed"] is False


def test_ai_assessment_failure_keeps_saved_emergency_result_unprocessed(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    original_create = main.at_create

    def fail_assessment(table, fields):
        if table == main.T_ASSESS:
            raise ConnectionError("private assessment detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_assessment)
    result = _checkin(client_rec, STEADY, "I want to die.", "sub-assess-failure")

    assert result["response_route"] == ROUTE_SAFETY
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["http_status"] == 200
    assert result["later_processing_failed"] is True
    log_record = fake_airtable.find_all(main.T_LOGS)[0]
    assert main.F_LOG["processed"] not in log_record["fields"]


def test_retry_after_assessment_failure_never_duplicates_emergency_side_effects(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    replay_id = "sub-assessment-failure-retry"
    original_create = main.at_create
    webhook_calls = []
    monkeypatch.setenv("GHL_CRISIS_WEBHOOK", "https://example.invalid/crisis")

    class AcceptedResponse:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        main.requests, "post",
        lambda *args, **kwargs: webhook_calls.append((args, kwargs)) or AcceptedResponse(),
    )

    def fail_assessment(table, fields):
        if table == main.T_ASSESS:
            raise ConnectionError("private assessment detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_assessment)

    first = _checkin(client_rec, STEADY, "I want to die.", replay_id)
    retry = _checkin(client_rec, STEADY, "I want to die.", replay_id)

    assert first["later_processing_failed"] is True
    assert retry["response_route"] == ROUTE_SAFETY
    assert retry["processing_incomplete"] is True
    assert retry["checkin_saved"] is True
    assert retry["crisis_alert_created"] is None
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 1
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 0
    assert len(webhook_calls) == 1
    crisis = fake_airtable.find_all(main.T_CRISIS)[0]
    assert main.F_CRISIS["assessment"] not in crisis["fields"]


def test_non_emergency_partial_replay_does_not_duplicate_daily_log(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    replay_id = "sub-ordinary-assessment-failure-retry"
    original_create = main.at_create

    def fail_assessment(table, fields):
        if table == main.T_ASSESS:
            raise ConnectionError("private assessment detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_assessment)
    with pytest.raises(ConnectionError):
        _checkin(client_rec, STEADY, "An ordinary steady day.", replay_id)

    retry = _checkin(client_rec, STEADY, "An ordinary steady day.", replay_id)

    assert retry["response_route"] is None
    assert retry["processing_incomplete"] is True
    assert retry["checkin_saved"] is True
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 0


def test_crisis_alert_link_failure_keeps_emergency_result(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    original_update = main.at_update

    def fail_crisis_link(table, record_id, fields):
        if table == main.T_CRISIS:
            raise ConnectionError("private link detail")
        return original_update(table, record_id, fields)

    monkeypatch.setattr(main, "at_update", fail_crisis_link)
    result = _checkin(client_rec, STEADY, "I want to die.", "sub-link-failure")

    assert result["response_route"] == ROUTE_SAFETY
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["http_status"] == 200
    assert result["later_processing_failed"] is True
    assert fake_airtable.find_all(main.T_LOGS)[0]["fields"][main.F_LOG["processed"]] is True


def test_daily_log_processed_update_failure_keeps_emergency_result(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    original_update = main.at_update

    def fail_processed_update(table, record_id, fields):
        if table == main.T_LOGS and fields.get(main.F_LOG["processed"]):
            raise ConnectionError("private processed detail")
        return original_update(table, record_id, fields)

    monkeypatch.setattr(main, "at_update", fail_processed_update)
    result = _checkin(client_rec, STEADY, "I want to die.", "sub-processed-failure")

    assert result["response_route"] == ROUTE_SAFETY
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["http_status"] == 200
    assert result["later_processing_failed"] is True


@pytest.mark.parametrize("non_success", [False, True])
def test_crisis_webhook_failure_does_not_claim_delivery_or_log_identity(
    fake_airtable, mock_gemini, make_client_record, monkeypatch, caplog, non_success
):
    client_rec = make_client_record()
    monkeypatch.setenv("GHL_CRISIS_WEBHOOK", "https://example.invalid/crisis")

    if non_success:
        class NonSuccessResponse:
            def raise_for_status(self):
                raise RuntimeError("private webhook response detail")

        monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: NonSuccessResponse())
    else:
        def webhook_exception(*args, **kwargs):
            raise ConnectionError("private webhook connection detail")

        monkeypatch.setattr(main.requests, "post", webhook_exception)

    result = _checkin(client_rec, STEADY, "I want to die.", "sub-webhook-failure")

    assert result["response_route"] == ROUTE_SAFETY
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["owner_notification_confirmed"] is False
    assert result["later_processing_failed"] is False
    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert assess["fields"][main.F_ASSESS["ghl_action"]] == ""
    assert main.F_ASSESS["owner_alert_status"] not in assess["fields"]
    log_output = caplog.text
    assert "Jane Doe" not in log_output
    assert "jane@example.com" not in log_output
    assert "I want to die" not in log_output
    assert "private webhook" not in log_output


def test_successful_optional_crisis_webhook_keeps_preexisting_action_value(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    monkeypatch.setenv("GHL_CRISIS_WEBHOOK", "https://example.invalid/crisis")
    calls = []

    class SuccessResponse:
        def raise_for_status(self):
            return None

    def successful_webhook(*args, **kwargs):
        calls.append((args, kwargs))
        return SuccessResponse()

    monkeypatch.setattr(main.requests, "post", successful_webhook)
    result = _checkin(client_rec, STEADY, "I want to die.", "sub-webhook-success")

    assert len(calls) == 1
    assert result["checkin_saved"] is True
    assert result["crisis_alert_created"] is True
    assert result["owner_notification_confirmed"] is False
    assert result["later_processing_failed"] is False
    assess = fake_airtable.find_all(main.T_ASSESS)[-1]
    assert assess["fields"][main.F_ASSESS["ghl_action"]] == "crisis_webhook_sent"
    assert main.F_ASSESS["owner_alert_status"] not in assess["fields"]


def test_ordinary_wellness_success_still_calls_gemini_and_saves_normally(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    original_run_assessment = main.run_assessment
    calls = []

    def tracked_assessment(*args, **kwargs):
        calls.append(True)
        return original_run_assessment(*args, **kwargs)

    monkeypatch.setattr(main, "run_assessment", tracked_assessment)
    result = _checkin(client_rec, STEADY, "An ordinary steady day.", "sub-ordinary-success")

    assert calls == [True]
    assert result["response_route"] == ROUTE_STEADY
    assert result["checkin_saved"] is True
    assert result["http_status"] == 200
    assert result["later_processing_failed"] is False
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 1


def test_ordinary_success_log_excludes_participant_identity_and_content(
    fake_airtable, mock_gemini, make_client_record, caplog
):
    journal = "private ordinary journal content"
    client_rec = make_client_record(
        email="ordinary-private@example.com", name="Private Participant")

    _checkin(client_rec, STEADY, journal, "sub-ordinary-private-log")

    output = caplog.text
    assert '"event": "assessment"' in output
    for prohibited in (
        "Private Participant", "ordinary-private@example.com", journal,
        client_rec["id"], submission_id_for("sub-ordinary-private-log"),
    ):
        assert prohibited not in output


def test_ordinary_initial_daily_log_failure_preserves_generic_exception_behavior(
    fake_airtable, mock_gemini, make_client_record, monkeypatch
):
    client_rec = make_client_record()
    original_create = main.at_create

    def fail_daily_log(table, fields):
        if table == main.T_LOGS:
            raise ConnectionError("private ordinary outage detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_daily_log)
    with pytest.raises(ConnectionError, match="private ordinary outage detail"):
        _checkin(client_rec, STEADY, "An ordinary steady day.", "sub-ordinary-failure")
