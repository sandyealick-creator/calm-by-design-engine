import re

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


def test_duplicate_email_updates_instead_of_duplicating(client, fake_airtable):
    enroll(client, email="dupe@example.com", phone="+15550001111")
    # Second enrollment attempt with the same email but a different phone/session
    client.delete_cookie("cbd_token")
    client.delete_cookie("cbd_csrf")
    enroll(client, email="dupe@example.com", phone="+15559998888")

    clients = fake_airtable.find_all(main.T_CLIENTS)
    assert len(clients) == 1
    assert clients[0]["fields"][main.F_CLIENT["phone"]] == "+15559998888"


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
    assert resp.get_json()["status"] == "ok"


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
