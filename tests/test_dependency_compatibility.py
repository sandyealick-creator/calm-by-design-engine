import json

import main


def test_locked_google_genai_call_shape_is_supported(monkeypatch):
    captured = {}
    expected = {
        "sentiment": "neutral",
        "progress_signal": False,
        "distress_signal": False,
        "safety_signal": "none",
        "medical_emergency_signal": False,
        "trigger_reasons": ["local compatibility test"],
        "suggested_element": "earth",
        "confidence": 0.8,
        "summary": "local compatibility test",
    }

    class Usage:
        prompt_token_count = 11
        candidates_token_count = 7

    class Response:
        text = json.dumps(expected)
        usage_metadata = Usage()

    class Models:
        def generate_content(self, *, model, contents, config):
            captured.update(model=model, contents=contents, config=config)
            return Response()

    class Client:
        def __init__(self, *, api_key):
            assert api_key == "test-gemini-key"
            self.models = Models()

    monkeypatch.setattr(main.genai, "Client", Client)
    result, _latency_ms, tokens_in, tokens_out = main.run_assessment(
        "ordinary local test",
        {"physical_symptoms": 1, "anxiety": 1, "energy": 10, "sleep": 10},
        {"current_week": 1, "current_state": "On Track", "prior_assessments": []},
    )

    assert result == expected
    assert tokens_in == 11
    assert tokens_out == 7
    assert captured["model"] == "test-gemini-model"
    assert captured["config"].response_mime_type == "application/json"
    assert captured["config"].response_schema == main.RESPONSE_SCHEMA
