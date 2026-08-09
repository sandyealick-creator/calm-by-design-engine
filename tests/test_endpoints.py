import re

import pytest

import main

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def extract_csrf(html_bytes):
    match = CSRF_RE.search(html_bytes.decode())
    assert match, "csrf_token field not found in response"
    return match.group(1)


def enroll(client, email="new@example.com", phone="+15550001111", sms=True):
    csrf = extract_csrf(client.get("/enroll").data)
    return client.post("/enroll", data={
        "csrf_token": csrf, "first_name": "Jane", "last_name": "Doe",
        "email": email, "phone": phone,
        **({"sms_consent": "on"} if sms else {}),
    })


def test_new_enrollment_creates_client_and_sets_cookie(client, fake_airtable):
    resp = enroll(client)
    assert resp.status_code == 200
    assert b"check-in" in resp.data.lower()
    clients = fake_airtable.find_all(main.T_CLIENTS)
    assert len(clients) == 1
    assert clients[0]["fields"][main.F_CLIENT["email"]] == "new@example.com"
    set_cookie_headers = resp.headers.get_all("Set-Cookie")
    assert any(h.startswith("cbd_token=") for h in set_cookie_headers)


def test_existing_email_enrollment_uses_generic_recovery_without_authenticating(
    client, fake_airtable, make_client_record
):
    existing = make_client_record(email="dupe@example.com")
    _raw_token, _expires = main.issue_access_token(existing["id"])
    original = fake_airtable.get(main.T_CLIENTS, existing["id"])["fields"]

    # Simulate a different browser that knows only the participant's email.
    client.delete_cookie("cbd_token")
    client.delete_cookie("cbd_csrf")
    resp = enroll(client, email="dupe@example.com", phone="+15559998888", sms=False)

    assert resp.status_code == 200
    assert b"If that email is enrolled" in resp.data
    assert not any(
        header.startswith("cbd_token=") for header in resp.headers.get_all("Set-Cookie")
    )
    assert len(fake_airtable.find_all(main.T_CLIENTS)) == 1
    assert len(fake_airtable.find_all(main.T_RECOVERY)) == 1

    unchanged = fake_airtable.get(main.T_CLIENTS, existing["id"])["fields"]
    assert unchanged[main.F_CLIENT["access_token_hash"]] == original[main.F_CLIENT["access_token_hash"]]
    assert unchanged[main.F_CLIENT["access_token_issued_at"]] == original[main.F_CLIENT["access_token_issued_at"]]
    assert unchanged[main.F_CLIENT["access_token_expires_at"]] == original[main.F_CLIENT["access_token_expires_at"]]


def test_existing_email_enrollment_cannot_overwrite_profile_or_consents(
    client, fake_airtable, make_client_record
):
    existing = make_client_record(
        email="protected@example.com",
        name="Original Participant",
        **{
            main.F_CLIENT["phone"]: "+15550001111",
            main.F_CLIENT["sms_consent"]: True,
            main.F_CLIENT["marketing_consent"]: True,
        },
    )

    client.delete_cookie("cbd_token")
    client.delete_cookie("cbd_csrf")
    enroll(client, email="protected@example.com", phone="+15559998888", sms=False)

    unchanged = fake_airtable.get(main.T_CLIENTS, existing["id"])["fields"]
    assert unchanged[main.F_CLIENT["name"]] == "Original Participant"
    assert unchanged[main.F_CLIENT["phone"]] == "+15550001111"
    assert unchanged[main.F_CLIENT["sms_consent"]] is True
    assert unchanged[main.F_CLIENT["marketing_consent"]] is True


def test_enrollment_without_sms_consent_still_succeeds(client, fake_airtable):
    resp = enroll(client, email="nosms@example.com", sms=False)
    assert resp.status_code == 200
    rec = fake_airtable.find(main.T_CLIENTS, lambda f: f.get(main.F_CLIENT["email"]) == "nosms@example.com")
    assert rec["fields"][main.F_CLIENT["sms_consent"]] is False


def test_checkin_requires_session_redirects_to_recover(client, fake_airtable):
    resp = client.get("/checkin")
    assert resp.status_code == 302
    assert "/recover" in resp.headers["Location"]


def test_returning_participant_checkin_prefilled(client, fake_airtable):
    enroll(client, email="returning@example.com")
    resp = client.get("/checkin")
    assert resp.status_code == 200
    assert b"Jane" in resp.data
    # No name/email/phone/consent fields on the returning check-in form
    assert b'name="email"' not in resp.data
    assert b'name="phone"' not in resp.data


def test_checkin_rejects_out_of_range_rating(client, fake_airtable, mock_gemini):
    enroll(client, email="rating@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-bad",
        "physical_symptoms": "11", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "test entry",
    })
    assert resp.status_code == 200
    assert b"whole number from 1 to 10" in resp.data
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0


def test_checkin_happy_path_saves_and_shows_result(client, fake_airtable, mock_gemini):
    enroll(client, email="happy@example.com")
    mock_gemini.set(sentiment="positive", progress_signal=True)
    csrf = extract_csrf(client.get("/checkin").data)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-happy",
        "physical_symptoms": "2", "anxiety": "2", "energy": "9", "sleep": "9",
        "journal": "Great day.",
    })
    assert resp.status_code == 200
    assert b"progress" in resp.data.lower()
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1


def test_checkin_csrf_missing_rejected(client, fake_airtable, mock_gemini):
    enroll(client, email="csrf@example.com")
    client.get("/checkin")  # establishes a csrf cookie, but we won't send the matching field
    resp = client.post("/checkin", data={
        "submission_id": "sub-csrf", "physical_symptoms": "5", "anxiety": "5",
        "energy": "5", "sleep": "5", "journal": "no csrf field",
    })
    assert resp.status_code == 400
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0


def test_checkin_verify_invalid_token_shows_generic_message(client, fake_airtable):
    resp = client.get("/checkin/verify?t=not-a-real-token")
    assert resp.status_code == 400
    assert b"no longer valid" in resp.data.lower()


def test_checkin_link_is_stripped_from_url_after_verify(client, fake_airtable):
    enroll(client, email="link@example.com")
    rec = fake_airtable.find_all(main.T_CLIENTS)[0]
    raw_token, _expires = main.issue_access_token(rec["id"])
    client.delete_cookie("cbd_token")

    resp = client.get(f"/checkin/verify?t={raw_token}")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/checkin"  # no token in the redirect target


def test_recover_generic_message_regardless_of_match(client, fake_airtable):
    csrf1 = extract_csrf(client.get("/recover").data)
    resp_known = client.post("/recover", data={"csrf_token": csrf1, "email": "unknown@example.com"})
    csrf2 = extract_csrf(client.get("/recover").data)
    resp_unknown = client.post("/recover", data={"csrf_token": csrf2, "email": "still-unknown@example.com"})
    assert resp_known.status_code == resp_unknown.status_code == 200
    assert b"on its way" in resp_known.data
    assert resp_known.data == resp_unknown.data


def test_recover_creates_token_only_for_matched_email(client, fake_airtable):
    enroll(client, email="match@example.com")
    client.delete_cookie("cbd_token")
    csrf = extract_csrf(client.get("/recover").data)
    client.post("/recover", data={"csrf_token": csrf, "email": "match@example.com"})

    reqs = fake_airtable.find_all(main.T_RECOVERY)
    assert len(reqs) == 1
    assert reqs[0]["fields"].get(main.F_RECOVERY["token_hash"])


def test_recover_confirm_full_round_trip(client, fake_airtable):
    enroll(client, email="confirm@example.com")
    client_rec = fake_airtable.find_all(main.T_CLIENTS)[0]
    client.delete_cookie("cbd_token")

    raw_recovery_token = main.new_random_token()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fake_airtable.create(main.T_RECOVERY, {
        main.F_RECOVERY["request_id"]: "confirm@example.com-x",
        main.F_RECOVERY["client"]: [client_rec["id"]],
        main.F_RECOVERY["token_hash"]: main.hash_token(raw_recovery_token),
        main.F_RECOVERY["requested_at"]: now.isoformat(),
        main.F_RECOVERY["expires_at"]: (now + timedelta(minutes=30)).isoformat(),
    })

    resp = client.get(f"/recover/confirm?rt={raw_recovery_token}")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/checkin"

    # Second use of the same recovery link must fail (single-use).
    resp2 = client.get(f"/recover/confirm?rt={raw_recovery_token}")
    assert resp2.status_code == 400


def test_assess_webhook_requires_secret(client, fake_airtable):
    resp = client.post("/assess", json={"journal_text": "x", "email": "a@example.com",
                                        "sleep": 5, "energy": 5, "anxiety": 5,
                                        "physical_symptoms": 5})
    assert resp.status_code == 401


def test_assess_webhook_invalid_scores(client, fake_airtable):
    resp = client.post("/assess", headers={"X-Webhook-Secret": "test-webhook-secret"},
                       json={"journal_text": "x", "email": "a@example.com",
                             "sleep": 99, "energy": 5, "anxiety": 5, "physical_symptoms": 5})
    assert resp.status_code == 400


def test_assess_webhook_client_not_found(client, fake_airtable):
    resp = client.post("/assess", headers={"X-Webhook-Secret": "test-webhook-secret"},
                       json={"journal_text": "x", "email": "nobody@example.com",
                             "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5})
    assert resp.status_code == 404


def test_assess_webhook_happy_path(client, fake_airtable, mock_gemini, make_client_record):
    make_client_record(email="ghl@example.com")
    mock_gemini.set(sentiment="neutral")
    resp = client.post("/assess", headers={"X-Webhook-Secret": "test-webhook-secret"},
                       json={"journal_text": "GHL entry", "email": "ghl@example.com",
                             "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "ok"
    assert set(payload) == {
        "status", "response_route", "response_route_label", "score", "tier",
        "tier_label", "element_name", "summary", "trigger_reasons",
        "fallback_mode", "medical_emergency_triggered", "safety_footer",
    }


def test_assess_webhook_persistence_failure_preserves_previous_error_contract(
    client, fake_airtable, make_client_record, monkeypatch
):
    make_client_record(email="ghl-outage@example.com")
    original_create = main.at_create

    def fail_daily_log(table, fields):
        if table == main.T_LOGS:
            raise ConnectionError("private GHL persistence detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_daily_log)
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Gemini must not run after Daily Log failure"),
    )
    resp = client.post(
        "/assess",
        headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": "I want to die.", "email": "ghl-outage@example.com",
            "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5,
            "submission_id": "sub-ghl-outage",
        },
    )

    assert resp.status_code == 500
    assert resp.get_json() == {"error": "internal error"}
    assert b"private GHL persistence detail" not in resp.data


@pytest.mark.parametrize(
    "failure_point",
    ["assessment_create", "crisis_link", "processed_update"],
)
def test_assess_later_persistence_failure_preserves_generic_error_contract(
    client, fake_airtable, mock_gemini, make_client_record, monkeypatch, caplog,
    failure_point,
):
    participant_email = f"assess-{failure_point}@example.com"
    journal = "I want to die."
    sensitive_token = "test-sensitive-access-token"
    sensitive_record_id = "rec-sensitive-record-id"
    raw_exception = (
        f"private later persistence detail Jane Doe {participant_email} "
        f"{journal} {sensitive_token} {sensitive_record_id}"
    )
    make_client_record(email=participant_email, **{
        main.F_CLIENT["access_token_hash"]: sensitive_token,
    })
    original_create = main.at_create
    original_update = main.at_update

    def maybe_fail_create(table, fields):
        if failure_point == "assessment_create" and table == main.T_ASSESS:
            raise ConnectionError(raw_exception)
        return original_create(table, fields)

    def maybe_fail_update(table, record_id, fields):
        if failure_point == "crisis_link" and table == main.T_CRISIS:
            raise ConnectionError(f"{raw_exception} {record_id}")
        if (failure_point == "processed_update" and table == main.T_LOGS
                and fields.get(main.F_LOG["processed"])):
            raise ConnectionError(f"{raw_exception} {record_id}")
        return original_update(table, record_id, fields)

    monkeypatch.setattr(main, "at_create", maybe_fail_create)
    monkeypatch.setattr(main, "at_update", maybe_fail_update)
    resp = client.post(
        "/assess",
        headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": journal, "email": participant_email,
            "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5,
            "submission_id": f"sub-assess-{failure_point}",
        },
    )

    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload == {"error": "internal error"}
    assert "status" not in payload
    for internal_key in (
        "checkin_saved", "crisis_alert_created", "owner_notification_confirmed",
        "later_processing_failed", "http_status",
    ):
        assert internal_key not in payload
    response_text = resp.data.decode()
    log_output = caplog.text
    for sensitive_value in (
        "Jane Doe", participant_email, journal, sensitive_token,
        sensitive_record_id, raw_exception,
    ):
        assert sensitive_value not in response_text
        assert sensitive_value not in log_output


def test_airtable_unavailable_shows_generic_error(client, fake_airtable, monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("Airtable is down")

    monkeypatch.setattr(main, "at_create", boom)
    csrf = extract_csrf(client.get("/enroll").data)
    resp = client.post("/enroll", data={
        "csrf_token": csrf, "first_name": "Jane", "last_name": "Doe",
        "email": "outage@example.com", "phone": "+15550001111",
    })
    assert resp.status_code == 500
    assert b"went wrong" in resp.data.lower()
    assert b"ConnectionError" not in resp.data
    assert b"Airtable is down" not in resp.data


def test_no_store_header_on_participant_pages(client, fake_airtable):
    resp = client.get("/enroll")
    assert resp.headers.get("Cache-Control") == "no-store"


def test_medical_emergency_result_page_shows_911_not_988(client, fake_airtable, mock_gemini):
    enroll(client, email="medical@example.com")
    mock_gemini.set(sentiment="distressed", medical_emergency_signal=True)
    csrf = extract_csrf(client.get("/checkin").data)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-medical-endpoint",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "Chest pain started an hour ago, my left arm feels strange.",
    })
    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "911" in body
    assert "medical emergency" in body
    assert "988" not in body
    assert "your check-in was saved" in body
    assert "we recorded an alert for white raven holistic to review" in body
    assert "cannot confirm that a notification was delivered or seen" in body
    assert "we've also let white raven holistic know" not in body
    assert "if it helps while you reach out" not in body


def test_simultaneous_medical_and_safety_result_page_shows_both(client, fake_airtable, mock_gemini):
    enroll(client, email="both@example.com")
    mock_gemini.set(safety_signal="direct_self_harm", medical_emergency_signal=True)
    csrf = extract_csrf(client.get("/checkin").data)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-both-endpoint",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I took a bunch of pills because I want to die and now I can't breathe.",
    })
    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "911" in body
    assert "988" in body
    assert body.index("this may also be a medical emergency") < body.index("your safety matters right now")
    assert "your check-in was saved" in body
    assert "we recorded an alert for white raven holistic to review" in body
    assert "cannot confirm that a notification was delivered or seen" in body
    assert "we've also let white raven holistic know" not in body


def test_self_harm_success_shows_truthful_saved_and_alert_wording(
    client, fake_airtable, mock_gemini
):
    enroll(client, email="self-harm-success@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-self-harm-success",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    })

    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "988" in body
    assert "911" in body
    assert body.index("988") < body.index("if you are in immediate danger")
    assert "your check-in was saved" in body
    assert "we recorded an alert for white raven holistic to review" in body
    assert "cannot confirm that a notification was delivered or seen" in body
    assert "we've also let white raven holistic know" not in body
    assert "white raven holistic was notified" not in body


@pytest.mark.parametrize(
    "journal,expect_988,expect_combined",
    [
        ("I want to die.", True, False),
        ("I have chest pain right now.", False, False),
        ("I want to die and I can't breathe.", True, True),
    ],
)
def test_identity_lookup_failure_preserves_deterministic_emergency_resources(
    client, fake_airtable, mock_gemini, monkeypatch, caplog,
    journal, expect_988, expect_combined,
):
    enroll(client, email="identity-outage@example.com")
    csrf = extract_csrf(client.get("/checkin").data)

    monkeypatch.setattr(
        main, "get_current_client",
        lambda: (_ for _ in ()).throw(ConnectionError("private identity lookup detail")),
    )
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Gemini must not run for identity outage fallback"),
    )

    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-identity-outage",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": journal,
    })

    assert resp.status_code == 503
    body = resp.data.decode().lower()
    assert "911" in body
    assert ("988" in body) is expect_988
    if expect_combined:
        assert body.index("this may also be a medical emergency") < body.index("your safety matters right now")
    assert "we could not save this check-in" in body
    assert "we could not record an alert for white raven holistic" in body
    assert "no notification is confirmed" in body
    assert "we've also let white raven holistic know" not in body
    assert "identity-outage@example.com" not in body
    assert "jane doe" not in body
    assert "private identity lookup detail" not in body
    assert "private identity lookup detail" not in caplog.text
    assert "identity-outage@example.com" not in caplog.text
    assert "Jane Doe" not in caplog.text
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 0


def test_identity_lookup_failure_still_requires_valid_csrf(
    client, fake_airtable, mock_gemini, monkeypatch
):
    enroll(client, email="identity-csrf@example.com")
    monkeypatch.setattr(
        main, "get_current_client",
        lambda: (_ for _ in ()).throw(ConnectionError("private identity lookup detail")),
    )
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Gemini must not run when CSRF is invalid"),
    )

    resp = client.post("/checkin", data={
        "submission_id": "sub-identity-csrf",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    })
    assert resp.status_code == 400
    assert b"988" not in resp.data


@pytest.mark.parametrize(
    "journal,expect_988,expect_combined",
    [
        ("I want to die.", True, False),
        ("I have chest pain right now.", False, False),
        ("I want to die and I can't breathe.", True, True),
    ],
)
def test_first_replay_lookup_failure_renders_unknown_states_without_side_effects(
    client, fake_airtable, mock_gemini, monkeypatch, caplog,
    journal, expect_988, expect_combined,
):
    enroll(client, email="first-replay-outage@example.com")
    csrf = extract_csrf(client.get("/checkin").data)

    monkeypatch.setattr(
        main, "find_log_by_submission_id",
        lambda submission_id: (_ for _ in ()).throw(
            ConnectionError("private first replay endpoint detail")),
    )
    monkeypatch.setattr(
        main, "at_create",
        lambda *args, **kwargs: pytest.fail("First replay failure must not write Airtable"),
    )
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("First replay failure must not call Gemini"),
    )
    monkeypatch.setattr(
        main.requests, "post",
        lambda *args, **kwargs: pytest.fail("First replay failure must not call GHL"),
    )

    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-first-replay-outage",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": journal,
    })

    assert resp.status_code == 503
    body = resp.data.decode().lower()
    assert "911" in body
    assert ("988" in body) is expect_988
    if expect_combined:
        assert body.index("this may also be a medical emergency") < body.index("your safety matters right now")
    assert "we could not confirm whether this check-in was saved" in body
    assert "we could not confirm whether an alert was recorded" in body
    assert "no notification, delivery, or review is confirmed" in body
    assert "your check-in was saved" not in body
    assert "we could not save this check-in" not in body
    assert "we recorded an alert for white raven holistic" not in body
    assert "we could not record an alert for white raven holistic" not in body
    assert "first-replay-outage@example.com" not in body
    assert "jane doe" not in body
    assert "private first replay endpoint detail" not in body
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 0
    log_output = caplog.text
    assert "first-replay-outage@example.com" not in log_output
    assert "Jane Doe" not in log_output
    assert journal not in log_output
    assert "private first replay endpoint detail" not in log_output


@pytest.mark.parametrize(
    "journal,expect_988,expect_combined",
    [
        ("I want to die.", True, False),
        ("I have chest pain right now.", False, False),
        ("I want to die and I can't breathe.", True, True),
    ],
)
def test_second_replay_lookup_failure_confirms_saved_and_unknown_alert(
    client, fake_airtable, mock_gemini, monkeypatch, caplog,
    journal, expect_988, expect_combined,
):
    enroll(client, email="second-replay-outage@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    client_rec = fake_airtable.find_all(main.T_CLIENTS)[0]
    fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: "sub-second-replay-outage",
        main.F_LOG["client"]: [client_rec["id"]],
    })

    monkeypatch.setattr(
        main, "find_assessment_by_log_id",
        lambda log_id: (_ for _ in ()).throw(
            ConnectionError("private second replay endpoint detail")),
    )
    monkeypatch.setattr(
        main, "at_create",
        lambda *args, **kwargs: pytest.fail("Second replay failure must not write Airtable"),
    )
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Second replay failure must not call Gemini"),
    )
    monkeypatch.setattr(
        main.requests, "post",
        lambda *args, **kwargs: pytest.fail("Second replay failure must not call GHL"),
    )

    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-second-replay-outage",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": journal,
    })

    assert resp.status_code == 503
    body = resp.data.decode().lower()
    assert "911" in body
    assert ("988" in body) is expect_988
    if expect_combined:
        assert body.index("this may also be a medical emergency") < body.index("your safety matters right now")
    assert "your check-in was saved" in body
    assert "we could not confirm whether an alert was recorded" in body
    assert "no notification, delivery, or review is confirmed" in body
    assert "we recorded an alert for white raven holistic" not in body
    assert "we could not record an alert for white raven holistic" not in body
    assert "second-replay-outage@example.com" not in body
    assert "jane doe" not in body
    assert "private second replay endpoint detail" not in body
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 0
    log_output = caplog.text
    assert "second-replay-outage@example.com" not in log_output
    assert "Jane Doe" not in log_output
    assert journal not in log_output
    assert "private second replay endpoint detail" not in log_output


def test_historical_failed_replay_uses_unknown_alert_wording(
    client, fake_airtable, mock_gemini, monkeypatch
):
    enroll(client, email="historical-failed@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    client_rec = fake_airtable.find_all(main.T_CLIENTS)[0]
    log_rec = fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: "sub-historical-failed",
        main.F_LOG["client"]: [client_rec["id"]],
    })
    fake_airtable.create(main.T_ASSESS, {
        main.F_ASSESS["daily_log"]: [log_rec["id"]],
        main.F_ASSESS["response_route"]: main.ROUTE_LABELS[main.ROUTE_SAFETY],
        main.F_ASSESS["score_tier"]: main.TIER_LABELS["STEADY"],
        main.F_ASSESS["support_score"]: 12,
        main.F_ASSESS["trigger_reasons"]: "[]",
        main.F_ASSESS["fallback_mode"]: False,
        main.F_ASSESS["crisis_alert"]: "Yes",
        main.F_ASSESS["owner_alert_status"]: "failed",
    })
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Historical replay must not call Gemini"),
    )
    monkeypatch.setattr(
        main.requests, "post",
        lambda *args, **kwargs: pytest.fail("Historical replay must not call GHL"),
    )

    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-historical-failed",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    })

    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "988" in body and "911" in body
    assert "your check-in was saved" in body
    assert "we could not confirm whether an alert was recorded" in body
    assert "no notification, delivery, or review is confirmed" in body
    assert "we recorded an alert for white raven holistic" not in body
    assert "we could not record an alert for white raven holistic" not in body
    assert "notification was delivered" not in body
    assert "white raven was informed" not in body
    assert "someone reviewed" not in body
    assert "will respond" not in body


@pytest.mark.parametrize(
    "journal,expect_988,expect_combined",
    [
        ("I want to die.", True, False),
        ("I have chest pain right now.", False, False),
        ("I want to die and I can't breathe.", True, True),
    ],
)
def test_first_daily_log_write_failure_renders_each_emergency_with_503(
    client, fake_airtable, mock_gemini, monkeypatch,
    journal, expect_988, expect_combined,
):
    enroll(client, email="log-outage@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    original_create = main.at_create

    def fail_daily_log(table, fields):
        if table == main.T_LOGS:
            raise ConnectionError("private Daily Log detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_daily_log)
    monkeypatch.setattr(
        main, "run_assessment",
        lambda *args, **kwargs: pytest.fail("Gemini must not run for early persistence fallback"),
    )

    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-log-outage",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": journal,
    })

    assert resp.status_code == 503
    body = resp.data.decode().lower()
    assert "911" in body
    assert ("988" in body) is expect_988
    if expect_combined:
        assert body.index("this may also be a medical emergency") < body.index("your safety matters right now")
    assert "we could not save this check-in" in body
    assert "we recorded an alert for white raven holistic to review" in body
    assert "cannot confirm that a notification was delivered or seen" in body
    assert "we've also let white raven holistic know" not in body
    assert "private daily log detail" not in body


def test_crisis_alert_creation_failure_renders_saved_emergency_truthfully(
    client, fake_airtable, mock_gemini, monkeypatch
):
    enroll(client, email="alert-outage@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    original_create = main.at_create

    def fail_crisis_alert(table, fields):
        if table == main.T_CRISIS:
            raise ConnectionError("private Crisis Alert detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_crisis_alert)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-alert-outage",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    })

    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "988" in body and "911" in body
    assert "your check-in was saved" in body
    assert "we could not record an alert for white raven holistic" in body
    assert "no notification is confirmed" in body
    assert "private crisis alert detail" not in body
    assert "we've also let white raven holistic know" not in body


def test_daily_log_and_crisis_alert_failure_render_both_truthful_failures(
    client, fake_airtable, mock_gemini, monkeypatch
):
    enroll(client, email="both-persistence-outage@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    original_create = main.at_create

    def fail_log_and_alert(table, fields):
        if table in (main.T_LOGS, main.T_CRISIS):
            raise ConnectionError("private combined persistence detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_log_and_alert)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-both-persistence-outage",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    })

    assert resp.status_code == 503
    body = resp.data.decode().lower()
    assert "988" in body and "911" in body
    assert "we could not save this check-in" in body
    assert "we could not record an alert for white raven holistic" in body
    assert "private combined persistence detail" not in body


@pytest.mark.parametrize(
    "failure_point",
    ["assessment_create", "crisis_link", "processed_update"],
)
def test_later_persistence_failures_never_replace_emergency_page(
    client, fake_airtable, mock_gemini, monkeypatch, caplog, failure_point
):
    participant_email = f"{failure_point}@example.com"
    journal = "I want to die."
    sensitive_token = "test-sensitive-access-token"
    sensitive_record_id = "rec-sensitive-record-id"
    raw_exception = (
        f"private later persistence detail Jane Doe {participant_email} "
        f"{journal} {sensitive_token} {sensitive_record_id}"
    )
    enroll(client, email=participant_email)
    csrf = extract_csrf(client.get("/checkin").data)
    original_create = main.at_create
    original_update = main.at_update

    def maybe_fail_create(table, fields):
        if failure_point == "assessment_create" and table == main.T_ASSESS:
            raise ConnectionError(raw_exception)
        return original_create(table, fields)

    def maybe_fail_update(table, record_id, fields):
        if failure_point == "crisis_link" and table == main.T_CRISIS:
            raise ConnectionError(f"{raw_exception} {record_id}")
        if (failure_point == "processed_update" and table == main.T_LOGS
                and fields.get(main.F_LOG["processed"])):
            raise ConnectionError(f"{raw_exception} {record_id}")
        return original_update(table, record_id, fields)

    monkeypatch.setattr(main, "at_create", maybe_fail_create)
    monkeypatch.setattr(main, "at_update", maybe_fail_update)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": f"sub-{failure_point}",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": journal,
    })

    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "988" in body and "911" in body
    assert "your check-in was saved" in body
    assert "we recorded an alert for white raven holistic to review" in body
    assert "cannot confirm that a notification was delivered or seen" in body
    assert "white raven holistic was notified" not in body
    assert "the alert was reviewed" not in body
    assert "someone reviewed" not in body
    assert "will respond" not in body
    assert "will follow up" not in body
    assert "processing completed" not in body
    assert "fully processed" not in body
    assert "later_processing_failed" not in body
    log_output = caplog.text
    for sensitive_value in (
        "jane doe", participant_email, journal.lower(), sensitive_token,
        sensitive_record_id, raw_exception.lower(),
    ):
        assert sensitive_value not in body
        assert sensitive_value not in log_output.lower()


def test_ordinary_checkin_daily_log_failure_keeps_generic_error_page(
    client, fake_airtable, mock_gemini, monkeypatch
):
    enroll(client, email="ordinary-outage@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    original_create = main.at_create

    def fail_daily_log(table, fields):
        if table == main.T_LOGS:
            raise ConnectionError("private ordinary check-in detail")
        return original_create(table, fields)

    monkeypatch.setattr(main, "at_create", fail_daily_log)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "sub-ordinary-outage",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "An ordinary steady day.",
    })

    assert resp.status_code == 500
    body = resp.data.decode().lower()
    assert "went wrong" in body
    assert "private ordinary check-in detail" not in body
    assert "988" not in body
