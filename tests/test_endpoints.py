import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main
from conftest import submission_id_for

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


def test_new_enrollment_creates_client_and_uses_clean_cookie_redirect(
    client, fake_airtable
):
    resp = enroll(client)
    assert resp.status_code == 302
    clients = fake_airtable.find_all(main.T_CLIENTS)
    assert len(clients) == 1
    assert clients[0]["fields"][main.F_CLIENT["email"]] == "new@example.com"
    assert resp.headers["Location"] == "/checkin"
    assert "?t=" not in resp.headers["Location"]
    cookie = next(
        h for h in resp.headers.get_all("Set-Cookie") if h.startswith("cbd_token="))
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    raw_cookie_token = cookie.split(";", 1)[0].split("=", 1)[1]
    assert raw_cookie_token not in resp.headers["Location"]
    assert raw_cookie_token not in resp.data.decode()
    assert client.get("/checkin").status_code == 200


def test_generated_access_link_uses_fragment_only():
    token = main.new_random_token()
    link = main.access_fragment_path(token)
    assert link == f"/access#t={token}"
    assert "?t=" not in link


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
    assert b"If recovery delivery is available" in resp.data
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
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/checkin"
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-bad"),
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-happy"),
        "physical_symptoms": "2", "anxiety": "2", "energy": "9", "sleep": "9",
        "journal": "Great day.",
    })
    assert resp.status_code == 200
    assert b"progress" in resp.data.lower()
    assert main.SAFETY_FOOTER.encode() in resp.data
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1


def test_participant_cannot_replay_another_participants_route_or_alert_state(
    client, fake_airtable, mock_gemini, make_client_record
):
    enroll(client, email="browser-a@example.com")
    participant_a = fake_airtable.find(
        main.T_CLIENTS,
        lambda f: f.get(main.F_CLIENT["email"]) == "browser-a@example.com",
    )
    participant_b = make_client_record(email="browser-b@example.com")
    replay_id = submission_id_for("browser-cross-participant")
    main.process_checkin(
        participant_b, 3, 3, 8, 8, "I want to die.", replay_id, "Flask Web")

    mock_gemini.set(sentiment="neutral", summary="participant a result")
    csrf = extract_csrf(client.get("/checkin").data)
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": replay_id,
        "physical_symptoms": "3", "anxiety": "3", "energy": "8", "sleep": "8",
        "journal": "An ordinary steady day.",
    })

    assert resp.status_code == 500
    body = resp.data.decode().lower()
    assert "something went wrong" in body
    assert "your safety matters right now" not in body
    assert 'href="tel:988"' not in body
    assert 'href="sms:988"' not in body
    assert "we recorded an alert" not in body
    logs = fake_airtable.find_all(main.T_LOGS)
    assert len(logs) == 1
    assert logs[0]["fields"][main.F_LOG["client"]] == [participant_b["id"]]
    assert participant_a["id"] not in logs[0]["fields"][main.F_LOG["client"]]


def test_checkin_csrf_missing_rejected(client, fake_airtable, mock_gemini):
    enroll(client, email="csrf@example.com")
    client.get("/checkin")  # establishes a csrf cookie, but we won't send the matching field
    resp = client.post("/checkin", data={
        "submission_id": submission_id_for("sub-csrf"), "physical_symptoms": "5", "anxiety": "5",
        "energy": "5", "sleep": "5", "journal": "no csrf field",
    })
    assert resp.status_code == 400
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0


def test_checkin_rejects_malformed_submission_id_before_processing(
    client, fake_airtable, monkeypatch
):
    enroll(client, email="malformed-id@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    monkeypatch.setattr(
        main, "process_checkin",
        lambda *args, **kwargs: pytest.fail("Malformed ID must not be processed"),
    )
    monkeypatch.setattr(
        main, "get_current_client",
        lambda: pytest.fail("Malformed ID must not look up participant identity"),
    )
    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": "x' OR TRUE()",
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "ordinary entry",
    })
    assert resp.status_code == 400
    assert b"form expired" in resp.data.lower()
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0


def test_journal_length_is_bounded_before_assessment_or_persistence(
    client, fake_airtable, monkeypatch
):
    enroll(client, email="journal-limit@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    too_long = "x" * (main.MAX_JOURNAL_LENGTH + 1)
    monkeypatch.setattr(
        main, "process_checkin",
        lambda *args, **kwargs: pytest.fail("Oversized journal must not be processed"),
    )
    monkeypatch.setattr(
        main, "get_current_client",
        lambda: pytest.fail("Oversized journal must precede identity lookup"),
    )
    checkin_resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": submission_id_for("journal-limit"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": too_long,
    })
    assert checkin_resp.status_code == 200
    assert str(main.MAX_JOURNAL_LENGTH).encode() in checkin_resp.data

    monkeypatch.setattr(
        main, "find_client",
        lambda *args, **kwargs: pytest.fail("Oversized journal must precede lookup"),
    )
    assess_resp = client.post(
        "/assess", headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": too_long, "email": "journal-limit@example.com",
            "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5,
        },
    )
    assert assess_resp.status_code == 400
    assert assess_resp.get_json() == {"error": "journal_text too long"}
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0


def test_checkin_verify_invalid_token_shows_generic_message(client, fake_airtable):
    resp = client.get("/checkin/verify?t=not-a-real-token")
    assert resp.status_code == 400
    assert b"no longer valid" in resp.data.lower()


def test_access_fragment_page_and_post_body_redemption(client, fake_airtable):
    enroll(client, email="link@example.com")
    rec = fake_airtable.find_all(main.T_CLIENTS)[0]
    raw_token, _expires = main.issue_access_token(rec["id"])
    client.delete_cookie("cbd_token")

    page = client.get(f"/access#t={raw_token}")
    assert page.status_code == 200
    assert raw_token.encode() not in page.data
    assert b"/static/redemption.js" in page.data
    assert b"http://" not in page.data and b"https://" not in page.data
    assert page.headers["Cache-Control"] == "no-store"
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert page.headers["Content-Security-Policy"] == main.REDEMPTION_CSP

    resp = client.post("/access", data={"token": raw_token})
    assert resp.status_code == 204
    assert raw_token.encode() not in resp.data
    cookies = resp.headers.get_all("Set-Cookie")
    access_cookie = next(h for h in cookies if h.startswith("cbd_token="))
    assert "Secure" in access_cookie
    assert "HttpOnly" in access_cookie
    assert "SameSite=Lax" in access_cookie
    assert client.get("/checkin").status_code == 200


def test_access_query_token_is_rejected_without_authentication(client, fake_airtable):
    enroll(client, email="query-rejected@example.com")
    rec = fake_airtable.find_all(main.T_CLIENTS)[0]
    raw_token, _expires = main.issue_access_token(rec["id"])
    client.delete_cookie("cbd_token")

    resp = client.get(f"/checkin/verify?t={raw_token}")
    assert resp.status_code == 400
    assert not any(
        h.startswith("cbd_token=") for h in resp.headers.get_all("Set-Cookie"))


@pytest.mark.parametrize(
    "path,query_key",
    [
        ("/access", "t"), ("/access", "rt"),
        ("/recover-access", "rt"), ("/recover-access", "t"),
    ],
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_new_redemption_routes_reject_bearer_queries_generically(
    client, fake_airtable, make_client_record, path, query_key, method,
):
    raw_token = main.new_random_token()
    participant = make_client_record(email="query-bearer@example.com", **{
        main.F_CLIENT["access_token_hash"]: main.hash_token(raw_token),
        main.F_CLIENT["access_token_expires_at"]: (
            datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    })
    if path == "/recover-access":
        fake_airtable.create(main.T_RECOVERY, {
            main.F_RECOVERY["request_id"]: "query-bearer-recovery",
            main.F_RECOVERY["client"]: [participant["id"]],
            main.F_RECOVERY["token_hash"]: main.hash_token(raw_token),
            main.F_RECOVERY["requested_at"]: datetime.now(timezone.utc).isoformat(),
            main.F_RECOVERY["expires_at"]: (
                datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        })
    before = fake_airtable.find_all(main.T_RECOVERY)
    participant_before = fake_airtable.get(main.T_CLIENTS, participant["id"])
    request_method = getattr(client, method)
    kwargs = {"data": {"token": raw_token}} if method == "post" else {}
    resp = request_method(f"{path}?{query_key}={raw_token}", **kwargs)

    assert resp.status_code == 400
    assert resp.data == b""
    assert raw_token.encode() not in resp.data
    assert not any(
        header.startswith("cbd_token=")
        for header in resp.headers.get_all("Set-Cookie")
    )
    assert fake_airtable.find_all(main.T_RECOVERY) == before
    assert fake_airtable.get(main.T_CLIENTS, participant["id"]) == participant_before
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Content-Security-Policy"] == main.REDEMPTION_CSP
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_redemption_script_requires_exactly_one_expected_fragment_key():
    source = (Path(__file__).parents[1] / "static" / "redemption.js").read_text()
    assert "params.getAll(expectedKey)" in source
    assert "keys.length === 1" in source
    assert "values.length === 1" in source
    assert "keys[0] === expectedKey" in source
    assert source.index("history.replaceState") < source.index("new URLSearchParams")
    assert "window.location.search" not in source
    assert "params.get(expectedKey)" not in source


def test_redemption_script_fragment_edge_cases_execute_in_javascript():
    script_path = Path(__file__).parents[1] / "static" / "redemption.js"
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");

async function runCase(pathname, hash, search, fetchOk = true) {
  const state = {destinations: [], fetchCount: 0, cleanedPath: null};
  const context = {
    URLSearchParams,
    window: {
      location: {
        pathname,
        hash,
        search,
        replace: (destination) => state.destinations.push(destination),
      },
      history: {
        replaceState: (_state, _title, path) => { state.cleanedPath = path; },
      },
    },
    fetch: () => {
      state.fetchCount += 1;
      return Promise.resolve({ok: fetchOk});
    },
  };
  vm.runInNewContext(source, context);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  return {
    destination: state.destinations.at(-1),
    fetchCount: state.fetchCount,
    cleanedPath: state.cleanedPath,
  };
}

(async () => {
  const token = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const results = {
    duplicate: await runCase("/access", `#t=${token}&t=${token}`, ""),
    extra: await runCase("/access", `#t=${token}&rt=${token}`, ""),
    empty: await runCase("/access", "#t=", ""),
    decode: await runCase("/access", "#t=%E0%A4%A", "", false),
    queryOnly: await runCase("/access", "", `?t=${token}`),
    valid: await runCase("/access", `#t=${token}`, ""),
    validRecovery: await runCase("/recover-access", `#rt=${token}`, ""),
  };
  process.stdout.write(JSON.stringify(results));
})().catch(() => process.exit(1));
"""
    completed = subprocess.run(
        ["node", "-e", harness, str(script_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    results = json.loads(completed.stdout)

    for case in ("duplicate", "extra", "empty", "queryOnly"):
        assert results[case] == {
            "destination": "/link-invalid",
            "fetchCount": 0,
            "cleanedPath": "/access",
        }
    assert results["decode"] == {
        "destination": "/link-invalid",
        "fetchCount": 1,
        "cleanedPath": "/access",
    }
    assert results["valid"] == {
        "destination": "/checkin",
        "fetchCount": 1,
        "cleanedPath": "/access",
    }
    assert results["validRecovery"] == {
        "destination": "/checkin",
        "fetchCount": 1,
        "cleanedPath": "/recover-access",
    }


def test_invalid_and_expired_access_post_tokens_fail_without_reflection(
    client, fake_airtable, make_client_record
):
    from datetime import datetime, timedelta, timezone
    expired_token = main.new_random_token()
    make_client_record(email="expired-access@example.com", **{
        main.F_CLIENT["access_token_hash"]: main.hash_token(expired_token),
        main.F_CLIENT["access_token_expires_at"]: (
            datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    })

    invalid = client.post("/access", data={"token": "not-a-valid-token"})
    expired = client.post("/access", data={"token": expired_token})
    assert invalid.status_code == expired.status_code == 400
    assert expired_token.encode() not in expired.data
    for resp in (invalid, expired):
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Content-Security-Policy"] == main.REDEMPTION_CSP
        assert "Location" not in resp.headers
        assert not any(
            h.startswith("cbd_token=") for h in resp.headers.get_all("Set-Cookie"))


def test_recover_generic_message_regardless_of_match(client, fake_airtable):
    csrf1 = extract_csrf(client.get("/recover").data)
    resp_known = client.post("/recover", data={"csrf_token": csrf1, "email": "unknown@example.com"})
    csrf2 = extract_csrf(client.get("/recover").data)
    resp_unknown = client.post("/recover", data={"csrf_token": csrf2, "email": "still-unknown@example.com"})
    assert resp_known.status_code == resp_unknown.status_code == 200
    assert b"If recovery delivery is available" in resp_known.data
    assert b"on its way" not in resp_known.data
    assert b"Check your email" not in resp_known.data
    assert resp_known.data == resp_unknown.data


def test_recover_creates_token_only_for_matched_email(client, fake_airtable):
    enroll(client, email="match@example.com")
    client.delete_cookie("cbd_token")
    csrf = extract_csrf(client.get("/recover").data)
    client.post("/recover", data={"csrf_token": csrf, "email": "match@example.com"})

    reqs = fake_airtable.find_all(main.T_RECOVERY)
    assert len(reqs) == 1
    assert reqs[0]["fields"].get(main.F_RECOVERY["token_hash"])
    recovery_link = reqs[0]["fields"][main.F_RECOVERY["recovery_link"]]
    assert "/recover-access#rt=" in recovery_link
    assert "?rt=" not in recovery_link


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

    page = client.get(f"/recover-access#rt={raw_recovery_token}")
    assert page.status_code == 200
    assert raw_recovery_token.encode() not in page.data
    assert page.headers["Cache-Control"] == "no-store"
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert page.headers["Content-Security-Policy"] == main.REDEMPTION_CSP

    resp = client.post("/recover-access", data={"token": raw_recovery_token})
    assert resp.status_code == 204
    assert raw_recovery_token.encode() not in resp.data
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Content-Security-Policy"] == main.REDEMPTION_CSP
    cookies = resp.headers.get_all("Set-Cookie")
    access_cookie = next(h for h in cookies if h.startswith("cbd_token="))
    assert "Secure" in access_cookie
    assert "HttpOnly" in access_cookie
    assert "SameSite=Lax" in access_cookie

    # Second use of the same recovery link must fail (single-use).
    resp2 = client.post("/recover-access", data={"token": raw_recovery_token})
    assert resp2.status_code == 400


@pytest.mark.parametrize(
    "client_links",
    [
        pytest.param("missing", id="missing"),
        pytest.param(None, id="null"),
        pytest.param([], id="empty"),
        pytest.param("rec00000000000001", id="non-list"),
        pytest.param(["bad"], id="malformed"),
        pytest.param([["rec00000000000001"]], id="nested"),
        pytest.param([7], id="numeric"),
        pytest.param([{"id": "rec00000000000001"}], id="dictionary"),
        pytest.param(
            ["rec00000000000001", "rec00000000000002"], id="multiple"),
    ],
)
def test_recovery_redemption_requires_exactly_one_valid_client_link_without_mutation(
    client, fake_airtable, make_client_record, monkeypatch, client_links,
):
    participant = make_client_record(email="recovery-owner@example.com", **{
        main.F_CLIENT["access_token_hash"]: "original-access-hash",
    })
    raw_token = main.new_random_token()
    now = datetime.now(timezone.utc)
    fields = {
        main.F_RECOVERY["request_id"]: "malformed-recovery-owner",
        main.F_RECOVERY["token_hash"]: main.hash_token(raw_token),
        main.F_RECOVERY["requested_at"]: now.isoformat(),
        main.F_RECOVERY["expires_at"]: (now + timedelta(minutes=30)).isoformat(),
    }
    if client_links != "missing":
        fields[main.F_RECOVERY["client"]] = client_links
    recovery = fake_airtable.create(main.T_RECOVERY, fields)
    recovery_before = fake_airtable.get(main.T_RECOVERY, recovery["id"])
    participant_before = fake_airtable.get(main.T_CLIENTS, participant["id"])
    monkeypatch.setattr(
        main, "at_update",
        lambda *args, **kwargs: pytest.fail("Malformed recovery ownership was consumed"),
    )
    monkeypatch.setattr(
        main, "issue_access_token",
        lambda *args, **kwargs: pytest.fail("Malformed recovery ownership rotated a token"),
    )

    resp = client.post("/recover-access", data={"token": raw_token})

    assert resp.status_code == 400
    assert resp.data == b""
    assert raw_token.encode() not in resp.data
    assert fake_airtable.get(main.T_RECOVERY, recovery["id"]) == recovery_before
    assert fake_airtable.get(main.T_CLIENTS, participant["id"]) == participant_before
    assert not any(
        header.startswith("cbd_token=")
        for header in resp.headers.get_all("Set-Cookie")
    )
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Content-Security-Policy"] == main.REDEMPTION_CSP
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_recovery_query_token_and_expired_post_token_fail_generically(
    client, fake_airtable, make_client_record
):
    client_rec = make_client_record(email="expired@example.com")
    raw_token = main.new_random_token()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fake_airtable.create(main.T_RECOVERY, {
        main.F_RECOVERY["request_id"]: "expired-request",
        main.F_RECOVERY["client"]: [client_rec["id"]],
        main.F_RECOVERY["token_hash"]: main.hash_token(raw_token),
        main.F_RECOVERY["requested_at"]: now.isoformat(),
        main.F_RECOVERY["expires_at"]: (now - timedelta(minutes=1)).isoformat(),
    })

    query_resp = client.get(f"/recover/confirm?rt={raw_token}")
    post_resp = client.post("/recover-access", data={"token": raw_token})
    assert query_resp.status_code == post_resp.status_code == 400
    assert raw_token.encode() not in query_resp.data
    assert raw_token.encode() not in post_resp.data
    assert not any(
        h.startswith("cbd_token=") for h in post_resp.headers.get_all("Set-Cookie"))


def test_assess_webhook_requires_secret(client, fake_airtable):
    resp = client.post("/assess", json={"journal_text": "x", "email": "a@example.com",
                                        "sleep": 5, "energy": 5, "anxiety": 5,
                                        "physical_symptoms": 5})
    assert resp.status_code == 401


def test_client_ip_ignores_untrusted_forwarded_for():
    with main.app.test_request_context(
        "/", headers={"X-Forwarded-For": "198.51.100.10"},
        environ_base={"REMOTE_ADDR": "127.0.0.9"},
    ):
        assert main.client_ip() == "127.0.0.9"


def test_checkin_and_assess_submission_rate_limits_precede_processing(
    client, fake_airtable, monkeypatch
):
    enroll(client, email="rate-submit@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    now = time.time()
    main._rate_buckets["checkin:127.0.0.1"].extend(
        [now] * main.IP_RATE_LIMIT["checkin"][0])
    monkeypatch.setattr(
        main, "get_current_client",
        lambda: pytest.fail("Ordinary rate-limited check-in looked up identity"),
    )
    monkeypatch.setattr(
        main, "find_client",
        lambda *args, **kwargs: pytest.fail("Rate-limited assess looked up identity"),
    )
    monkeypatch.setattr(
        main, "process_checkin",
        lambda *args, **kwargs: pytest.fail("Rate-limited request must not process"),
    )
    checkin_resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": submission_id_for("rate-checkin"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "ordinary entry",
    })
    assert checkin_resp.status_code == 429

    main._rate_buckets["assess:127.0.0.1"].extend(
        [now] * main.IP_RATE_LIMIT["assess"][0])
    assess_resp = client.post(
        "/assess", headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": "ordinary entry", "email": "rate-submit@example.com",
            "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5,
        },
    )
    assert assess_resp.status_code == 429
    assert assess_resp.get_json() == {"error": "too many requests"}
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0


@pytest.mark.parametrize(
    "journal,expect_988,medical_first",
    [
        ("I want to die.", True, False),
        ("I have chest pain right now.", False, False),
        ("I want to die and I can't breathe.", True, True),
    ],
)
def test_rate_limited_checkin_preserves_local_emergency_guidance_without_external_work(
    client, fake_airtable, monkeypatch, journal, expect_988, medical_first,
):
    enroll(client, email="rate-emergency@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    now = time.time()
    main._rate_buckets["checkin:127.0.0.1"].extend(
        [now] * main.IP_RATE_LIMIT["checkin"][0])

    def forbidden(*args, **kwargs):
        pytest.fail("Rate-limited emergency request attempted external work")

    monkeypatch.setattr(main, "get_current_client", forbidden)
    monkeypatch.setattr(main, "process_checkin", forbidden)
    monkeypatch.setattr(main, "at_create", forbidden)
    monkeypatch.setattr(main, "run_assessment", forbidden)
    monkeypatch.setattr(main.requests, "post", forbidden)
    resp = client.post("/checkin", data={
        "csrf_token": csrf,
        "submission_id": submission_id_for(f"rate-{journal}"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": journal,
    })

    assert resp.status_code == 429
    body = resp.data.decode().lower()
    assert "911" in body
    assert ("988" in body) is expect_988
    if medical_first:
        assert body.index("this may also be a medical emergency") < body.index(
            "your safety matters right now")
    assert "we could not save this check-in" in body
    assert "no notification is confirmed" in body
    assert "your check-in was saved" not in body
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0


@pytest.mark.parametrize(
    "form_override,expected_status",
    [
        ({"physical_symptoms": "not-a-number"}, 200),
        ({"journal": "x" * (main.MAX_JOURNAL_LENGTH + 1)}, 200),
        ({"csrf_token": "invalid"}, 400),
    ],
)
def test_rate_limited_checkin_still_validates_input_before_local_detection(
    client, fake_airtable, monkeypatch, form_override, expected_status,
):
    enroll(client, email="rate-validation@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    now = time.time()
    main._rate_buckets["checkin:127.0.0.1"].extend(
        [now] * main.IP_RATE_LIMIT["checkin"][0])
    monkeypatch.setattr(
        main, "get_current_client",
        lambda: pytest.fail("Invalid rate-limited input reached identity lookup"),
    )
    data = {
        "csrf_token": csrf, "submission_id": submission_id_for("rate-validation"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    }
    data.update(form_override)
    resp = client.post("/checkin", data=data)
    assert resp.status_code == expected_status
    assert b"988" not in resp.data


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


def test_assess_cannot_replay_another_participants_summary_or_reasons(
    client, fake_airtable, mock_gemini, make_client_record
):
    participant_a = make_client_record(email="assess-a@example.com", name="Participant A")
    participant_b = make_client_record(email="assess-b@example.com", name="Participant B")
    replay_id = submission_id_for("assess-cross-participant")
    mock_gemini.set(
        sentiment="distressed", summary="private participant b summary",
        trigger_reasons=["private participant b reason"],
    )
    main.process_checkin(
        participant_b, 3, 3, 8, 8, "Private participant B journal",
        replay_id, "GHL Form")

    resp = client.post(
        "/assess",
        headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": "Participant A journal", "email": "assess-a@example.com",
            "sleep": 8, "energy": 8, "anxiety": 3, "physical_symptoms": 3,
            "submission_id": replay_id,
        },
    )

    assert resp.status_code == 500
    assert resp.get_json() == {"error": "internal error"}
    assert "private participant b" not in resp.data.decode().lower()
    logs = fake_airtable.find_all(main.T_LOGS)
    assert len(logs) == 1
    assert logs[0]["fields"][main.F_LOG["client"]] == [participant_b["id"]]


def test_multiply_linked_assessment_exposes_no_data_on_either_endpoint(
    client, fake_airtable, mock_gemini, make_client_record,
):
    enroll(client, email="malformed-browser@example.com")
    participant = fake_airtable.find(
        main.T_CLIENTS,
        lambda f: f.get(main.F_CLIENT["email"]) == "malformed-browser@example.com",
    )
    foreign = make_client_record(email="malformed-foreign@example.com")
    replay_id = submission_id_for("multiply-linked-assessment-endpoints")
    log_rec = fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: replay_id,
        main.F_LOG["client"]: [participant["id"]],
        main.F_LOG["journal"]: "An ordinary saved journal.",
        main.F_LOG["physical"]: 5,
        main.F_LOG["anxiety"]: 5,
        main.F_LOG["energy"]: 5,
        main.F_LOG["sleep"]: 5,
    })
    fake_airtable.create(main.T_ASSESS, {
        main.F_ASSESS["daily_log"]: [log_rec["id"]],
        main.F_ASSESS["client"]: [participant["id"], foreign["id"]],
        main.F_ASSESS["reasoning"]: "private multiply linked summary",
        main.F_ASSESS["trigger_reasons"]: '["private multiply linked reason"]',
        main.F_ASSESS["response_route"]: main.ROUTE_LABELS[main.ROUTE_SAFETY],
        main.F_ASSESS["owner_alert_status"]: "sent",
    })

    csrf = extract_csrf(client.get("/checkin").data)
    browser_resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": replay_id,
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "An ordinary retry.",
    })
    machine_resp = client.post(
        "/assess", headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": "An ordinary retry.",
            "email": "malformed-browser@example.com",
            "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5,
            "submission_id": replay_id,
        },
    )

    browser_body = browser_resp.data.decode().lower()
    assert browser_resp.status_code == 200
    assert "could not finish processing" in browser_body
    assert "private multiply linked" not in browser_body
    assert "your safety matters right now" not in browser_body
    assert main.SAFETY_FOOTER.lower() in browser_body
    assert "we recorded an alert" not in browser_body
    assert machine_resp.status_code == 500
    assert machine_resp.get_json() == {"error": "internal error"}
    assert "private multiply linked" not in machine_resp.data.decode().lower()
    assert mock_gemini.call_count() == 0


@pytest.mark.parametrize("bad_id", [
    "x' OR TRUE()",
    "x" * 10000,
    submission_id_for("uppercase-not-canonical").upper(),
])
def test_malformed_submission_ids_are_rejected_before_participant_lookup(
    client, fake_airtable, monkeypatch, bad_id
):
    monkeypatch.setattr(
        main, "find_client",
        lambda *args, **kwargs: pytest.fail("Malformed ID must not look up a participant"),
    )
    resp = client.post(
        "/assess",
        headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": "ordinary entry", "email": "any@example.com",
            "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5,
            "submission_id": bad_id,
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid submission_id"}
    assert len(fake_airtable.find_all(main.T_LOGS)) == 0


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
            "submission_id": submission_id_for("sub-ghl-outage"),
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
    sensitive_phone = "+15559876545"
    sensitive_record_id = "rec-sensitive-record-id"
    raw_exception = (
        f"private later persistence detail Jane Doe {participant_email} "
        f"{journal} {sensitive_token} {sensitive_record_id}"
    )
    make_client_record(email=participant_email, **{
        main.F_CLIENT["phone"]: sensitive_phone,
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
            "submission_id": submission_id_for(f"sub-assess-{failure_point}"),
        },
    )

    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload == {"error": "internal error"}
    assert "status" not in payload
    for internal_key in (
        "checkin_saved", "crisis_alert_created", "owner_notification_confirmed",
        "later_processing_failed", "processing_incomplete", "http_status",
    ):
        assert internal_key not in payload
    response_text = resp.data.decode()
    log_output = caplog.text
    for sensitive_value in (
        "Jane Doe", participant_email, journal, sensitive_token,
        sensitive_phone, sensitive_record_id, raw_exception,
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-medical-endpoint"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "Chest pain started an hour ago, my left arm feels strange.",
    })
    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "911" in body
    assert "medical emergency" in body
    assert "988" not in body
    assert main.SAFETY_FOOTER.lower() not in body
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-both-endpoint"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I took a bunch of pills because I want to die and now I can't breathe.",
    })
    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "911" in body
    assert "988" in body
    assert main.SAFETY_FOOTER.lower() in body
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-self-harm-success"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    })

    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert "988" in body
    assert "911" in body
    assert main.SAFETY_FOOTER.lower() in body
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-identity-outage"),
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
        "submission_id": submission_id_for("sub-identity-csrf"),
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
        lambda submission_id, client_record_id: (_ for _ in ()).throw(
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-first-replay-outage"),
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
        main.F_LOG["submission_id"]: submission_id_for("sub-second-replay-outage"),
        main.F_LOG["client"]: [client_rec["id"]],
    })

    monkeypatch.setattr(
        main, "find_assessment_for_log",
        lambda log_record: (_ for _ in ()).throw(
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-second-replay-outage"),
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
        main.F_LOG["submission_id"]: submission_id_for("sub-historical-failed"),
        main.F_LOG["client"]: [client_rec["id"]],
    })
    fake_airtable.create(main.T_ASSESS, {
        main.F_ASSESS["daily_log"]: [log_rec["id"]],
        main.F_ASSESS["client"]: [client_rec["id"]],
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-historical-failed"),
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-log-outage"),
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-alert-outage"),
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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-both-persistence-outage"),
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
    sensitive_phone = "+15559876546"
    sensitive_record_id = "rec-sensitive-record-id"
    raw_exception = (
        f"private later persistence detail Jane Doe {participant_email} "
        f"{journal} {sensitive_token} {sensitive_record_id}"
    )
    enroll(client, email=participant_email, phone=sensitive_phone)
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
        "csrf_token": csrf, "submission_id": submission_id_for(f"sub-{failure_point}"),
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
        sensitive_phone, sensitive_record_id, raw_exception.lower(),
    ):
        assert sensitive_value not in body
        assert sensitive_value not in log_output.lower()


def test_retry_after_assessment_failure_preserves_guidance_without_duplicate_side_effects(
    client, fake_airtable, mock_gemini, monkeypatch
):
    enroll(client, email="partial-retry@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    replay_id = submission_id_for("endpoint-partial-retry")
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
    form = {
        "csrf_token": csrf, "submission_id": replay_id,
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "I want to die.",
    }

    first = client.post("/checkin", data=form)
    retry = client.post("/checkin", data=form)

    assert first.status_code == retry.status_code == 200
    retry_body = retry.data.decode().lower()
    assert "988" in retry_body and "911" in retry_body
    assert "your check-in was saved" in retry_body
    assert "could not finish processing" in retry_body
    assert "could not confirm whether an alert was recorded" in retry_body
    assert "and no notification, delivery, review, or response is confirmed" in retry_body
    assert "and notification, delivery, review, or response is confirmed" not in retry_body
    assert "private assessment detail" not in retry_body
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 1
    assert len(fake_airtable.find_all(main.T_ASSESS)) == 0
    assert len(webhook_calls) == 1
    assert mock_gemini.call_count() == 1
    crisis = fake_airtable.find_all(main.T_CRISIS)[0]
    assert main.F_CRISIS["assessment"] not in crisis["fields"]


@pytest.mark.parametrize(
    "saved_journal,expect_988,expect_medical",
    [
        ("An ordinary saved journal.", False, False),
        ("I want to die.", True, False),
        ("I have chest pain right now.", False, True),
        ("I want to die and I can't breathe.", True, True),
    ],
)
def test_partial_replay_endpoint_preserves_saved_emergency_guidance_and_order(
    client, fake_airtable, mock_gemini, monkeypatch,
    saved_journal, expect_988, expect_medical,
):
    enroll(client, email="partial-saved-emergency@example.com")
    csrf = extract_csrf(client.get("/checkin").data)
    participant = fake_airtable.find_all(main.T_CLIENTS)[0]
    replay_id = submission_id_for(f"partial-endpoint-{saved_journal}")
    fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: replay_id,
        main.F_LOG["client"]: [participant["id"]],
        main.F_LOG["journal"]: saved_journal,
        main.F_LOG["physical"]: 5,
        main.F_LOG["anxiety"]: 5,
        main.F_LOG["energy"]: 5,
        main.F_LOG["sleep"]: 5,
    })
    monkeypatch.setattr(
        main, "at_create", lambda *a, **k: pytest.fail("Partial replay created a record"))
    monkeypatch.setattr(
        main, "at_update", lambda *a, **k: pytest.fail("Partial replay updated a record"))
    monkeypatch.setattr(
        main.requests, "post", lambda *a, **k: pytest.fail("Partial replay called webhook"))

    resp = client.post("/checkin", data={
        "csrf_token": csrf, "submission_id": replay_id,
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "An ordinary retry.",
    })
    assert resp.status_code == 200
    body = resp.data.decode().lower()
    assert ("your safety matters right now" in body) is expect_988
    if expect_988:
        assert "988" in body
    elif expect_medical:
        assert "988" not in body
    if expect_medical:
        medical_heading = (
            "this may also be a medical emergency" if expect_988
            else "this may be a medical emergency"
        )
        assert medical_heading in body
    assert "could not finish processing" in body
    assert "your check-in was saved" in body
    assert "and no notification, delivery, review, or response is confirmed" in body
    assert "and notification, delivery, review, or response is confirmed" not in body
    if expect_medical and not expect_988:
        assert main.SAFETY_FOOTER.lower() not in body
    else:
        assert main.SAFETY_FOOTER.lower() in body
    if expect_988 and expect_medical:
        assert body.index("this may also be a medical emergency") < body.index(
            "your safety matters right now")
    assert len(fake_airtable.find_all(main.T_LOGS)) == 1
    assert len(fake_airtable.find_all(main.T_CRISIS)) == 0
    assert mock_gemini.call_count() == 0


def test_assess_partial_replay_returns_only_generic_internal_error(
    client, fake_airtable, mock_gemini, make_client_record,
):
    participant = make_client_record(email="assess-partial@example.com")
    replay_id = submission_id_for("assess-partial")
    fake_airtable.create(main.T_LOGS, {
        main.F_LOG["submission_id"]: replay_id,
        main.F_LOG["client"]: [participant["id"]],
        main.F_LOG["journal"]: "I want to die.",
        main.F_LOG["physical"]: 5,
        main.F_LOG["anxiety"]: 5,
        main.F_LOG["energy"]: 5,
        main.F_LOG["sleep"]: 5,
    })
    resp = client.post(
        "/assess", headers={"X-Webhook-Secret": "test-webhook-secret"},
        json={
            "journal_text": "An ordinary retry.", "email": "assess-partial@example.com",
            "sleep": 5, "energy": 5, "anxiety": 5, "physical_symptoms": 5,
            "submission_id": replay_id,
        },
    )
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "internal error"}
    assert mock_gemini.call_count() == 0


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
        "csrf_token": csrf, "submission_id": submission_id_for("sub-ordinary-outage"),
        "physical_symptoms": "5", "anxiety": "5", "energy": "5", "sleep": "5",
        "journal": "An ordinary steady day.",
    })

    assert resp.status_code == 500
    body = resp.data.decode().lower()
    assert "went wrong" in body
    assert "private ordinary check-in detail" not in body
    assert "988" not in body
