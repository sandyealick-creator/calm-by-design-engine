# Calm by Design — Manual Test Checklist

Everything not marked "done locally" below needs a live Cloud Run deployment (or `python main.py` locally against real Airtable/Gemini) with real `GEMINI_API_KEY` / `AIRTABLE_API_KEY` / `WEBHOOK_SECRET` / `SESSION_SECRET` set, and must be run against **test data only**. Before starting: enroll every test participant with **Test Record** checked on the Clients table, and use email addresses you control — never a real participant's contact info. **Never trigger the safety route or medical-emergency route against a real phone number or email you don't own.**

The historical baseline before Phase 2A added its dependency-compatibility test was 265 mocked tests. Phase 2A recorded 266 passing tests. Phase 2F recorded 311 collected and 311 passed in an isolated temporary environment: the 266-test application baseline plus 45 deployment-state validator tests, with zero skipped or missing tests and zero guarded outbound attempts. The guard covers only the patched Python APIs documented in README and HANDOFF; it is not a universal network guarantee or operating-system sandbox. This is mocked local evidence only and claims no live Gemini, Airtable, GHL, delivery, browser, container, Cloud Run, or deployment validation.

## Status legend

- ✅ **Done locally** — verified this session via `pytest` (Airtable/Gemini mocked) and/or `run_golden.py` (real Gemini, no Airtable writes). Logic-level verification only.
- 🔲 **Needs live Airtable** — requires running the app (locally or on Cloud Run) against your real CBD Core base, which writes real records. Not done this session per your instruction not to make additional live Airtable changes without approval — even locally, since `main.py` always talks to the live base (there's no sandbox-base toggle).
- 🔲 **Needs Cloud Run** — requires the deployed public URL specifically (e.g. testing from an external phone, or anything GHL needs to call back to).
- 🔲 **Needs GHL workflow** — requires the new "Send Check-in Link" workflow (see below) to exist before it can be tested.
- 🔲 **Needs manual browser/phone** — requires a human clicking through, not automatable.

## Before you start

- [ ] 🔲 Needs live Airtable/Cloud Run. `SESSION_SECRET` and `WEBHOOK_SECRET` are set to long random strings, distinct from each other, not committed anywhere.
- [ ] 🔲 Needs Cloud Run. Confirm the deployed Python runtime version is supported by every dependency declared in `requirements.txt`, then run the mocked suite and `pip check` in that same runtime before live verification.
- [ ] 🔲 Needs GHL workflow (not yet created). Create the new GHL workflow **"Send Check-in Link"**: Trigger = Airtable "New Record Created" on the `Recovery Requests` table, filtered to records where `Recovery Link` is not empty → Find Contact by email → Send Email containing the `Recovery Link` field value directly. That field holds the full, single-use, 30-minute fragment link (`/recover-access#rt=...`); the browser removes the fragment before same-origin POST redemption, and the Airtable field is cleared once used.
- [x] ✅ Done live with test-only records. The published "Daily Check-In - Assessment" GHL workflow authenticated to `/assess` and completed the expected Airtable/Gemini processing. Support Score `14` with a `Steady` tier and route was observed. All synthetic test records were deleted; no repeat compatibility test is required.
- [ ] 🔲 Needs live Airtable/Cloud Run. The existing "Crisis Alert Notification" and "Safety Buffer Routing" GHL workflows are still active and untouched.

## Controlled candidate-release gates

Every item in this section requires its own reviewed Cloud authorization. The
local deployment-state fixtures do not satisfy these live gates.

- [ ] 🔲 Needs Cloud Build/Artifact Registry. Confirm the evidence-only explicit build config was generated outside the submitted source context, its exact bytes and SHA-256 were captured, and its single Docker step consumes `_SOURCE_SHA`, `_SOURCE_TREE`, and `_CANDIDATE_IMAGE` under explicit `MUST_MATCH` with no `ALLOW_LOOSE`. Preserve raw REST Build bytes before validation. Separately confirm the returned Build has the verified project alias, authorized service account, one resolved Docker step and top-level image, exact matching source provenance, exact substitutions, and either explicit `MUST_MATCH` or its default-zero omission backed by the validated config. Require one matching `results.images[]` BuiltImage with a canonical digest, exact Package-version resource ending in that digest, valid optional OCI media type, and `pushTiming` fully contained within the Build interval when present. Construct one exact non-paginated Artifact Registry `DockerImages.Get` from the validated tag and Build digest; its verified project alias, exact bare source-SHA tag component, URI, and digest must all match before deriving the deployment image. Keep the independently validated source-archive identity separate from Cloud source-object provenance.
- [ ] 🔲 Needs Cloud Run. Confirm the exact candidate revision name, exactly one `Ready=True` condition, and one canonical digest-qualified image reference bound to the approved registry, project, repository, image, and immutable digest.
- [ ] 🔲 Needs Cloud Run. Prove the candidate is absent from production traffic or explicitly receives zero, is untagged, and the approved baseline fixed revision still receives 100%.
- [ ] 🔲 Needs Cloud Run. For a `--no-traffic` candidate, require `latestCreatedRevisionName` to equal the candidate and its own revision evidence to report exactly one `Ready=True`. Permit `latestReadyRevisionName` only as either the fixed baseline or that independently validated candidate; prove production exposure from the complete fixed, untagged traffic map, with baseline at 100% and candidate at zero.
- [ ] 🔲 Needs Cloud Run. Record the complete gcloud-emitted pre/post traffic maps and prove that any floating-`LATEST` to fixed-baseline change is the only map transformation while the resolved effective serving allocation remains identical. Treat these as projected CLI output, not transport-level HTTP bytes.
- [ ] 🔲 Needs Cloud Run. Compare the exact schema-validated safe runtime projection before and after deployment: a nonempty container list, environment names and secret references (never plaintext values), service account, CPU/memory, concurrency/timeout, distinct service-level and revision-level scaling, ingress/authentication including `INGRESS_TRAFFIC_NONE`, execution environment, one direct VPC interface when selected, startup/liveness/readiness probe metadata, one exposed container port when selected, and volumes. A singleton container may omit `name`; compare it by its sole position and do not treat name presence or absence as drift. Require a valid present singleton name. Multiple containers require explicit, valid, unique names and are compared by name independent of order. Probe-header values and other unselected values are not compared and must not be claimed preserved by this evidence alone.
- [ ] 🔲 Needs Cloud Run/Secret Manager metadata. From one active-project metadata response, bind the exact authorized project ID and project number. Establish exactly one `SESSION_SECRET` Secret Manager reference with an exact positive numeric version, strictly rebind the saved result to the current project/region/service before constructing any metadata URL, accept only either member of the verified project pair while retaining the observed resource identity, reject stale scope, every other textual or numeric project, and aliases such as `latest`, then prove that version exists and is enabled without accessing its payload.
- [ ] 🔲 Needs Cloud Run. Before traffic movement, require a complete fixed-revision percentage map totaling 100, preserve the complete tag map, and reject any reliance on floating `LATEST`.
- [ ] 🔲 Needs Cloud Run. Predetermine the observation duration and measurable failure thresholds before each separately authorized traffic movement.
- [ ] 🔲 Needs Cloud Run. Validate an executable rollback plan targeting the exact known-good fixed revision and digest, complete percentages and tags, exact precondition map, and exact post-rollback verification before moving any traffic.

## A. Enrollment and identity

- [x] ✅ Done locally. **New enrollment** creates one Clients row, does not require SMS/marketing consent to complete (`test_new_enrollment_creates_client_and_uses_clean_cookie_redirect`, `test_enrollment_without_sms_consent_still_succeeds`).
- [x] ✅ Done locally. **Consent handling**: SMS and marketing consent save independently, no cross-contamination (`test_enrollment_without_sms_consent_still_succeeds` plus the schema keeps the two fields separate).
- [x] ✅ Done locally. **Existing email protection**: a clean browser cannot overwrite the participant profile or consent fields, rotate the durable access token, or receive an authenticated cookie; the submission uses the generic recovery process instead (`test_existing_email_enrollment_uses_generic_recovery_without_authenticating`, `test_existing_email_enrollment_cannot_overwrite_profile_or_consents`).
- [x] ✅ Done locally. **Returning participant**: session cookie skips name/email/phone/consent fields on `/checkin` (`test_returning_participant_checkin_prefilled`).
- [x] ✅ Done locally. **Identity recovery, logic**: generic message regardless of match, real link only created for a matched email, single-use enforcement, full round trip (`test_recover_generic_message_regardless_of_match`, `test_recover_creates_token_only_for_matched_email`, `test_recover_confirm_full_round_trip`).
- [ ] 🔲 Needs GHL workflow + live Airtable. **Identity recovery, email delivery**: confirm the actual email arrives and the link logs you back in, in a real inbox.
- [ ] 🔲 Needs manual browser/phone. **Lost link / new device recovery path**: repeat from a second device/browser to confirm cross-device recovery works end-to-end.
- [ ] 🔲 Needs Cloud Run. Confirm request logs do not contain access or recovery bearer tokens during `/access#t=...` and `/recover-access#rt=...` redemption. Fragments should never reach the HTTP request line; do not enroll participants if deployed logging contradicts this expectation.
- [ ] 🔲 Needs Cloud Run. Before enabling recovery delivery, confirm generated recovery links use the intended public HTTPS origin and are not influenced by an unexpected Host or proxy scheme. The application currently derives the origin from `request.url_root`.

## B–E. Daily check-in, scoring, routing

- [x] ✅ Done locally. **Score boundaries** 11/12, 15/16, 23/24 (`test_score_boundary_11_vs_12_routes_differently` plus `test_routing_config.py`'s full boundary suite).
- [x] ✅ Done locally. **Positive journal entry** → Positive/Progress, no grounding practice pushed (`test_positive_progress_advances_curriculum`).
- [x] ✅ Done locally. **Neutral entry** → Steady.
- [x] ✅ Done locally. **Distressed entry below 16** escalates to Grounding Support (`test_distress_at_lower_score_escalates_to_grounding`).
- [x] ✅ Done locally. **High score, no self-harm language** → Heightened Support, not Safety Route (`test_high_score_alone_is_not_safety_route`).
- [x] ✅ Done locally. **All five elements** selectable and recorded, anti-repeat logic works (`test_all_five_elements_selectable`, `test_anti_repeat_avoids_last_used_element`).
- [x] ✅ Done locally. **Duplicate submission** (double-click/retry) creates exactly one record (`test_idempotent_replay_same_submission_id_no_duplicate`).
- [x] ✅ Done locally. **Two legitimate same-day check-ins** save independently and can land in different tiers (`test_two_legitimate_same_day_checkins_are_independent`).
- [ ] 🔲 Needs live Airtable. Confirm the on-screen result **and** the real Airtable `AI Assessments` row match (field-ID correctness against the actual base, not the test fake).
- [ ] 🔲 Needs manual browser/phone. **Mobile layout**: run enrollment and check-in on an actual phone/emulator. No horizontal scrolling, inputs usable, 988/911/911-medical links tappable.
- [ ] 🔲 Needs Cloud Run. Confirm `request.remote_addr` provides a useful participant/GHL boundary behind Cloud Run before relying on submission rate limits. Do not enable trust of `X-Forwarded-For` without an explicitly verified proxy-hop configuration.

## Safety and medical-emergency language (use only against your own test contact info)

- [x] ✅ Done locally, keyword backstop level. Historical/negated self-harm language does not trigger (`test_historical_past_tense_with_negation_does_not_trigger`, `test_negation_does_not_trigger`).
- [x] ✅ Done locally. Ambiguous self-harm language → `ambiguous`, not the Safety Route by itself (`test_ambiguous_hopelessness`, `test_ambiguous_self_harm_language_does_not_trigger_either_override`).
- [x] ✅ Done locally. Direct current self-harm language → Safety Route, Crisis Alert created, 988/911 shown (`test_direct_self_harm_language_still_routes_to_safety`, `test_gemini_safety_signal_triggers_safety_route`, `test_keyword_rule_triggers_safety_route_even_when_gemini_says_none`).
- [x] ✅ Done locally. Imminent-danger language → Safety Route, `imminent_danger` (`test_imminent_self_harm_danger_still_routes_to_safety`).
- [x] ✅ Done locally. High score alone never triggers the Safety Route (`test_high_score_alone_is_not_safety_route`).
- [x] ✅ Done locally, keyword backstop level. **Ordinary physical discomfort** does not trigger the medical-emergency override (`test_ordinary_physical_discomfort_does_not_trigger_medical_emergency`, `test_ordinary_physical_discomfort_is_not_medical_emergency`).
- [x] ✅ Done locally. **Clear medical-emergency language** (chest pain, can't breathe) → Medical Emergency Route: 911 guidance shown, no 988, no grounding practice as the response (`test_chest_pain_triggers_medical_emergency`, `test_clear_medical_emergency_language_routes_to_medical_emergency`, `test_medical_emergency_result_page_shows_911_not_988`).
- [x] ✅ Done locally. **Historical/third-person medical language** does not trigger (`test_historical_medical_language_does_not_trigger`, `test_third_person_medical_language_does_not_trigger`).
- [x] ✅ Done locally. **Simultaneous medical-emergency + self-harm language** → Safety Route wins for routing/curriculum, but the result page shows both the 911 medical block and the 988 safety block (`test_simultaneous_medical_emergency_and_self_harm`, `test_simultaneous_medical_and_safety_result_page_shows_both`).
- [x] ✅ Done locally. Medical-emergency and self-harm signals are independent in both directions, neither implies the other (`test_medical_and_self_harm_language_are_independent`, `test_self_harm_alone_does_not_trigger_medical_emergency`, `test_medical_emergency_alone_does_not_trigger_self_harm_signal`).
- [x] ✅ Done locally against the real Gemini API (`run_golden.py`, 19/20 passed — see final report for the one disagreement, which does not affect real routing).
- [ ] 🔲 Needs live Airtable. Confirm the real Crisis Alerts row's `Alert ID` and `Reasoning` correctly show the `SELF_HARM` / `MEDICAL_EMERGENCY` / `SELF_HARM_AND_MEDICAL_EMERGENCY` category prefix (logic verified locally; real-base field write not yet verified).

## Failure modes

- [x] ✅ Done locally. **Malformed Gemini response** → deterministic fallback, `Fallback Mode` set, participant still gets a result (`test_malformed_gemini_response_triggers_fallback`).
- [x] ✅ Done locally. **Gemini unavailable** → same fallback behavior; keyword safety backstop still catches direct self-harm language without Gemini (`test_gemini_unavailable_falls_back_and_keyword_rule_still_catches_safety`, `test_gemini_unavailable_still_routes_by_score`).
- [x] ✅ Done locally. **Airtable unavailable** → participant sees the generic error page, no stack trace or raw error text (`test_airtable_unavailable_shows_generic_error`).
- [x] ✅ Done locally, by design. **GoHighLevel unavailable** → the optional `GHL_ROUTING_WEBHOOK`/`GHL_CRISIS_WEBHOOK` calls are guarded by `if hook:`, so an unset/unreachable webhook cannot block a check-in from saving (unit-tested indirectly; no live GHL call is made in the test suite at all).

## After this checklist passes

Only then should the new Flask enrollment/check-in flow be treated as the primary demo path; keep the existing GHL form available as a fallback until it does.
