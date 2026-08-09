# HANDOFF.md

Factual description of the Calm by Design assessment engine as it exists after this session's testing. Written for a coding agent (Codex or otherwise) picking up this repository with no prior context. Contains no secrets, no real participant data, and no environment-variable values.

## 1. Project purpose and deadline

Calm by Design is a nervous system regulation program for White Raven Holistic. This repository is the "assessment engine": a Flask service that takes a participant's daily check-in (symptom ratings + journal entry), classifies it with Gemini, applies deterministic routing rules, and returns an immediate on-screen response while writing an audit trail to Airtable. Built for the **Build with Gemini XPRIZE hackathon, deadline August 17, 2026**.

## 2. Architecture and data flow

Single Flask service (`main.py`), no separate frontend build. Designed for Google Cloud Run; the current version is not deployed. Two entry paths share one core:

```
Participant browser                          GoHighLevel (existing, separate)
  /enroll -> Client record (Airtable)           GHL form -> POST /assess --+
  /access#t=... -> same-origin POST -> session cookie                     |
  /checkin -> ----------------------------------------------------------->|
                                                                           v
                                                    process_checkin() (shared core, main.py)
                                                         |
                                                         +--> Daily Log (Airtable)
                                                         +--> Gemini structured classification
                                                         |      + deterministic keyword backstops
                                                         |      (safety_rules.py), run on every submission
                                                         +--> support score + tier (routing_config.py)
                                                         +--> response route decision
                                                         |      +--> AI Assessment record (Airtable)
                                                         +--> curriculum state machine
                                                         |      +--> Client update + State Transition
                                                         +--> Safety Route / Medical Emergency Route
                                                                +--> Crisis Alert record (Airtable)
                                                                +--> existing GHL Crisis Alert workflow
                                                                     (polls Airtable, not called directly)
```

GHL's existing "Crisis Alert Notification" and "Safety Buffer Routing" workflows poll Airtable directly for new records (Airtable's own "New Record Created" trigger) rather than being called by this app. `GHL_ROUTING_WEBHOOK` / `GHL_CRISIS_WEBHOOK` are optional outbound calls this app makes if those env vars are set; as of the last check, they are unset in production, so Airtable polling is the real delivery path.

## 3. Enrollment, identity, check-in, recovery flow

- **Enrollment** (`GET/POST /enroll`): first name, last name, email, phone required. SMS consent (operational, non-marketing) and marketing consent are two independent optional checkboxes; neither is required to enroll. Submitting an existing email does not update the Client, rotate its access token, or authenticate the browser; it creates the same generic recovery request as `/recover`. A genuinely new enrollment receives a random access token stored only as a hash and set directly in a Secure, HttpOnly cookie, then redirects to the clean `/checkin` path. The raw token is never rendered into HTML or placed in the redirect URL. Separately delivered bearer links, if used, must use `/access#t=...` and the fragment-to-POST redemption page.
- **Identity**: no name/email/phone/consent re-entry on return visits. Identity is resolved via an HttpOnly, Secure, SameSite=Lax session cookie holding the raw access token; the server only ever stores its SHA-256 hash. Bearer links place the token in a URL fragment that a minimal same-origin page removes before POSTing it in the request body. Query-token redemption is rejected.
- **Check-in** (`GET/POST /checkin`): requires a valid session (redirects to `/recover` otherwise). Four integer 1-10 ratings (physical symptoms, anxiety, energy, sleep) plus a journal entry of at most 5,000 characters. A server-generated canonical UUIDv4 `submission_id` (hidden form field, regenerated each page load) makes retries/double-clicks idempotent without blocking a second genuine same-day entry. Replay lookup is scoped to and independently verified against the authenticated Client relationship.
- **Recovery** (`GET/POST /recover`, `GET/POST /recover-access`): always shows the same generic message regardless of whether the submitted email is enrolled. If matched, creates a short-lived (30 minute), single-use fragment link (stored as both a SHA-256 hash for verification and, separately, the full link in plaintext in a `Recovery Link` field so the future GHL workflow can deliver it). The browser removes the fragment and POSTs the token in the request body. Successful redemption rotates the participant's durable access token and clears the `Recovery Link` field.

## 4. Support score and thresholds

`routing_config.py`:

```python
support_score = physical_symptoms + anxiety + (11 - energy) + (11 - sleep)  # range 4-40

SCORE_TIERS:  score <= 11  -> POSITIVE_REGULATED
              score <= 15  -> STEADY
              score <= 23  -> GROUNDING_SUPPORT
              score <= 40  -> HEIGHTENED_SUPPORT
```

Explicitly non-clinical: never described as a diagnosis, medical assessment, or validated instrument anywhere in code or copy.

## 5. Wellness response routes (five, unchanged in this session)

Computed by `routing_config.route(tier, safety_signal_triggered, distress_signal, progress_signal)`:

1. **Positive/Progress** - low score tier + Gemini's `progress_signal` true.
2. **Steady** - low score tier, no progress signal.
3. **Grounding Support** - score tier 16-23, OR distress detected at a lower tier.
4. **Heightened Support** - score tier 24-40.
5. **Safety Route** - independent of score; wins whenever self-harm/suicide language is detected (see below).

Grounding/Heightened routes select one of five elements (Earth, Air, Fire, Water, Spirit; content in `elements_content.py`), avoiding repeating the participant's last-used element when another fits (`next_element()`).

## 6. Medical-emergency override (new this session)

A **separate safety override**, not a sixth wellness tier - `routing_config.route()` itself is unchanged. `process_checkin()` checks for a medical-emergency signal before calling `route()`:

- Detected independently via Gemini's `medical_emergency_signal` (boolean, added to the structured schema) **and** a deterministic keyword backstop (`safety_rules.check_medical_emergency()`) that runs on every submission, not just as a Gemini-outage fallback. Narrow phrase list: chest pain, can't breathe, stroke signs, severe bleeding, anaphylaxis, active seizure, loss of consciousness. Excludes negated, historical/past-tense, third-person ("my dad had..."), and quoted language.
- **Routing:** if a medical-emergency signal is present and self-harm/suicide language is *not*, the route is `MEDICAL_EMERGENCY_ROUTE`: on-screen 911/emergency-care guidance, **no 988 mention anywhere on that page** (including the small-print safety footer, which is suppressed specifically on this route since it otherwise mentions 988), and **no grounding/element practice at all** (not even as a secondary option - movement/breathing practices are treated as inappropriate advice mid-emergency). No curriculum movement.
- If self-harm language **is also** present, `SAFETY_ROUTE` wins for routing and curriculum purposes (unchanged self-harm behavior), but the medical signal is still recorded and the result page shows **both** the 911 medical block and the 988 self-harm block.
- A Crisis Alert record is created for both the medical-only and the combined case (reusing the existing Crisis Alerts table), with the alert ID and reasoning prefixed `MEDICAL_EMERGENCY`, `SELF_HARM`, or `SELF_HARM_AND_MEDICAL_EMERGENCY` so a human reading the alert can tell which occurred. Owner notification is explicitly framed as supplemental, not guaranteed or real-time monitored, in both code comments and participant-facing copy.
- The system prompt (`system_prompt.txt`) instructs Gemini not to diagnose, name a condition, or interpret ordinary physical symptoms as emergencies - only to flag clear, current, first-person emergency language.

## 7. Suicide/self-harm safety override (unchanged in this session)

`safety_rules.check_safety()` plus Gemini's `safety_signal` (`none`/`ambiguous`/`direct_self_harm`/`imminent_danger`), combined so the strongest of the two wins; `Safety Trigger Source` records which one fired (`gemini`/`keyword_rule`/`both`). Runs on every submission. Distinguishes negation, third person, historical/resolved, and quoted language from direct current first-person intent. A high support score alone never triggers this route.

## 8. Curriculum progression and timing safeguards

`Clients.Current State` cycles between `On Track`, `Safety Buffer`, and `Completed` (10 weeks). `curriculum_action()` maps a response route to `ADVANCE` / `HOLD` / `ROUTE_TO_BUFFER` / `SAFETY` (the last covers both Safety Route and Medical Emergency Route - neither moves the curriculum). An `ADVANCE` only actually advances the week if at least `MIN_DAYS_BETWEEN_ADVANCES` (7) days have passed since `Clients.Week Started At` was last set - this specifically prevents advancing the curriculum from a single positive entry, or multiple positive entries within the same scheduled week. Safety Buffer re-entry requires `REENTRY_THRESHOLD` (2) consecutive non-buffer-triggering days.

## 9. Participant token, cookie, recovery-link, expiration, revocation

- **Access token**: `secrets.token_urlsafe(32)`, never stored raw - only its SHA-256 hash, on `Clients.Access Token Hash`, with `Access Token Issued At` / `Access Token Expires At` (90-day TTL). The cookie (`cbd_token`, HttpOnly/Secure/SameSite=Lax) holds the raw token directly. Saved access links use `/access#t=...`; the fragment is removed with `history.replaceState` before a same-origin POST-body exchange. Query-token redemption is rejected. Controlled Cloud Run verification must still confirm the deployed request-log behavior.
- **Revocation**: requesting a new link via `/recover` rotates (overwrites) the stored hash, immediately invalidating the previous token - no separate revoke action exists or is needed.
- **Recovery token**: separate, single-use, 30-minute TTL, stored hashed on the `Recovery Requests` table, plus a plaintext `Recovery Link` field (the full URL) that the pending GHL workflow reads to email it - see field notes below for why this one field is deliberately plaintext.
- **CSRF token**: a per-page-load random value, set as a matching cookie and hidden form field (double-submit pattern), generated via `SESSION_SECRET`.
- **Rate limiting**: in-process, per-IP, on `/enroll` and `/recover` (10/hour each); resets on instance restart and doesn't share state across multiple Cloud Run instances - a documented tradeoff for current traffic levels, not a production guarantee.

## 10. Airtable (base "CBD Core", `app15O2dXYrCeVKb4`)

Existing tables, all preserved: **Clients**, **Daily Logs**, **AI Assessments**, **Curriculum Modules**, **Buffer Protocols**, **State Transitions**, **Crisis Alerts**.

Fields added this build cycle (all additive, created via the Airtable MCP with explicit approval in the prior session):
- **Clients**: `SMS Consent (Operational)`, `Marketing Consent`, `Access Token Hash`, `Access Token Issued At`, `Access Token Expires At`, `Week Started At`, `Test Record`.
- **Daily Logs**: `Submission ID`.
- **AI Assessments**: `Support Score`, `Score Tier`, `Sentiment`, `Progress Signal`, `Distress Signal`, `Safety Signal`, `Trigger Reasons`, `Suggested Element`, `Response Route`, `Safety Trigger Source`, `Fallback Mode`, `Fallback Reason`, `GHL Action`, `Owner Alert Status`, `App Version`.
- **New table Recovery Requests**: `Request ID`, `Client`, `Recovery Token Hash`, `Requested At`, `Expires At`, `Used At`, `Recovery Link`.

`NS State` and `Stress Signatures` on AI Assessments are legacy from the pre-correction Gemini schema and are no longer populated (left in place, not deleted, per additive-only instruction).

**Not yet added (deferred, pending your approval - see Known Limitations):** dedicated `Medical Emergency Signal`, `Medical Emergency Reason`, and `Medical Trigger Source` fields on AI Assessments, and an `Alert Category` field on Crisis Alerts. The medical-emergency correction added this session works without them - the category and reasoning are currently written into the existing `Trigger Reasons` field (as a text marker) and into the `Crisis Alerts.Reasoning`/`Alert ID` fields (as a bracketed prefix) instead, specifically so no additional live Airtable schema changes were made without approval this session.

## 11. GHL workflows

- **Active workflows, pending controlled compatibility verification**: "Daily Check-In - Assessment" (form -> webhook -> `/assess`), "Crisis Alert Notification" (polls Crisis Alerts), "Safety Buffer Routing" (polls AI Assessments). `/assess` now applies a journal limit, rate limit, and strict UUIDv4 validation when `submission_id` is supplied; normal shared-core routing is unchanged.
- **Still requires manual creation**: "Send Check-in Link" - trigger on `Recovery Requests` new records where `Recovery Link` is not empty, find contact by email, email that link. Not yet created.

## 12. Major files

| File | Purpose |
|---|---|
| `main.py` | Flask app: all routes, the shared `process_checkin()` core, token/session/CSRF, Airtable I/O, curriculum state machine, error handling. |
| `routing_config.py` | Support score formula, tiers, the five wellness routes' `route()` function, curriculum constants. Medical-emergency route constant lives here but the override logic itself lives in `main.py`. |
| `safety_rules.py` | Deterministic keyword backstops: `check_safety()` (self-harm) and `check_medical_emergency()` (medical), both independent, both run on every submission. |
| `elements_content.py` | Earth/Air/Fire/Water/Spirit practice content and anti-repeat element selection. |
| `system_prompt.txt` | Gemini system instruction; defines the structured output schema's intent field by field. |
| `templates/*.html` | Participant-facing pages: enroll, checkin, recover, result (all five routes + medical emergency + combined case), link_invalid, error, base layout. |
| `golden_set.json` / `run_golden.py` | Scenario-based eval harness against the real Gemini API (no Airtable writes). |
| `tests/` | pytest suite, Airtable and Gemini mocked (`tests/conftest.py`'s `FakeAirtable` and `mock_gemini` fixtures). |
| `MANUAL_TEST_CHECKLIST.md` | What still needs live Airtable/Cloud Run/GHL/a human, and what's already been verified locally. |
| `env.yaml` | Local real credentials, gitignored, never committed. |

## 13. Local installation and setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values; never commit .env
```

## 14. Running Flask locally

```
export GEMINI_API_KEY=... AIRTABLE_API_KEY=... WEBHOOK_SECRET=... SESSION_SECRET=...
python main.py
```
Serves on `http://0.0.0.0:8080` by default. **Note:** there is no sandbox/test Airtable base toggle - running this locally against real credentials writes to the live CBD Core base. Get explicit approval before doing this against production data; use `Test Record`-flagged entries.

## 15. Running pytest

```
pip install -r requirements.txt   # includes pytest
pytest
```
No real credentials needed - `tests/conftest.py` sets dummy env vars and mocks all Airtable/Gemini calls.

## 16. Running the Gemini golden set

```
export GEMINI_API_KEY=...   # real key; this makes real, billed API calls
python run_golden.py
```
Writes `golden_results.json`. Does not touch Airtable.

## 17. Cloud Run deployment procedure

```
gcloud run deploy cbd-assess \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --set-env-vars "GEMINI_API_KEY=...,AIRTABLE_API_KEY=...,WEBHOOK_SECRET=...,SESSION_SECRET=..."
```
`--allow-unauthenticated` is safe because `/assess` still requires a valid `X-Webhook-Secret` header and the participant-facing routes are meant to be public. **Do not run this without the project owner's explicit go-ahead** - it is a live deploy.

## 18. Required environment variables (names only, see `.env.example` for placeholders)

`GEMINI_API_KEY`, `AIRTABLE_API_KEY`, `WEBHOOK_SECRET`, `SESSION_SECRET` (all required), `GEMINI_MODEL`, `GHL_ROUTING_WEBHOOK`, `GHL_CRISIS_WEBHOOK`, `PORT` (all optional).

## 19. Verified behavior (this session)

- **pytest: 230/230 passing** against mocked Airtable and Gemini in an isolated temporary environment, with outbound sockets blocked. No live application service or participant data was accessed.
- **`run_golden.py` against the real Gemini API: 19/20 passing.** The one disagreement (case G07: Gemini set `distress_signal: false` where the case expected `true`, on a physical-flare entry where the participant explicitly wrote "emotionally I'm actually okay") does not change real routing for that case - the numeric score alone already places it in Heightened Support regardless of that boolean. Worth revisiting the case's expectation, not a code defect.
- Medical-emergency and self-harm signals confirmed independent of each other in both directions, at both the keyword-backstop level and the combined-routing level.
- Generic error page confirmed for a simulated Airtable outage; no credential or stack trace shown to the participant.

## 20. Unverified behavior

- Real Airtable field writes against the live base (pytest uses a fake; field-ID correctness against the actual base has not been re-verified since the fields were created).
- Real GHL email delivery for check-in links and recovery links (the new workflow doesn't exist yet).
- Mobile/browser rendering (no browser was driven this session).
- Cross-device recovery.
- Cloud Run deployment of this version (not deployed this session).
- Cloud Run request-log verification for the fragment-to-POST bearer redemption flow and direct remote-address rate-limit behavior.
- The `Recovery Link` field being cleared via an empty-string PATCH against the real Airtable API (only verified against the test fake).

## 21. Known limitations, risks, bugs, deferred improvements

- **Medical-emergency audit fields not yet in Airtable schema** (see section 10) - currently piggybacking on `Trigger Reasons` text and `Crisis Alerts` reasoning prefixes. Cleaner long-term: add dedicated fields, pending approval.
- **Dead code**: the Safety Route's optional secondary grounding-element display in `result.html` (`{% if element %}` under the `SAFETY_ROUTE` branch) never actually renders, because `process_checkin()` never populates `element` for `ROUTE_SAFETY` (only for Grounding/Heightened). Pre-existing from the prior session's build, not touched here since it was out of scope for the medical-emergency correction.
- **In-process rate limiting and CSRF state** don't survive instance restarts or scale-out to multiple Cloud Run instances. Fine at current traffic levels; not a production guarantee.
- **Partial replay recovery is deliberately manual.** If an owned Daily Log exists but no provably owned AI Assessment can be found, the app reports that the check-in was saved but processing is incomplete and performs no new writes, Gemini call, Crisis Alert, or webhook. Repairing or completing that record requires an operator-reviewed workflow; the app does not speculate about live Airtable relationships.
- **Recovery redemption is only sequentially single-use.** The current read-then-update flow rejects a second use after the first completes, but it is not an atomic compare-and-set across concurrent Cloud Run instances. Do not claim concurrency-safe single use without an atomic datastore operation and a dedicated concurrency test.
- **Recovery-link origin uses `request.url_root`.** Controlled deployment verification must confirm the effective public Host and scheme before recovery delivery is enabled. A canonical public-origin configuration has not been introduced in this checkpoint.
- **Gemini request timeout remains platform/SDK-dependent.** Airtable and optional GHL calls have explicit timeouts, but an explicit `google-genai` timeout was not added because no installed SDK was available to inspect and `requirements.txt` does not pin a concrete compatible API version. Verify the supported timeout option before deployment rather than guessing.
- **`GHL_ROUTING_WEBHOOK` / `GHL_CRISIS_WEBHOOK`** remain unset/unwired in production per the prior session's findings - all real delivery currently depends on GHL's own Airtable-polling workflows.
- **`safety_rules.py`'s `FIRST_PERSON` regex** (used by `check_safety()`, the self-harm checker) treats bare "my" as first-person without the third-party-relation exclusion that `check_medical_emergency()` now has. Self-harm phrases mostly avoid this because they embed "myself"/"my life" as objects, but it's a latent inconsistency between the two checkers worth aligning later.
- **Golden set case G07** disagreement noted above - consider a soft-check pattern (like the existing `expected_element` soft check) for `distress_signal` rather than a hard pass/fail.

## 22. What remains unsuitable for real clinical or crisis use

The support score and both Gemini-based classifications are non-clinical routing signals, not diagnoses or validated instruments. The deterministic keyword backstops (self-harm and medical-emergency) are narrow, high-recall approximations designed to catch clear language, not clinical screeners, and can still miss indirect or unusual phrasing. Neither the Safety Route nor the Medical Emergency Route is monitored in real time. Owner alerts are supplemental, depend on GHL delivery (not fully wired yet), and are never guaranteed or real-time. This system must never be presented to participants, or treated internally, as emergency care or a substitute for a clinician.

## 23. Safest recommended first task for Codex

Do not deploy, touch GHL, or change live Airtable schema as a first task. The safest, well-scoped first task is: **after getting the project owner's explicit approval**, add the deferred Airtable fields listed in section 10 and 21 (`Medical Emergency Signal`, `Medical Emergency Reason`, `Medical Trigger Source` on AI Assessments; `Alert Category` on Crisis Alerts), then update `main.py` to write to them instead of the current `Trigger Reasons`/reasoning-prefix workaround, following the exact pattern already used for the other AI Assessments fields added this session. It's additive-only, has a clear existing pattern to copy, is fully covered by the existing pytest fixtures (`fake_airtable`, `mock_gemini`), and directly resolves a documented known limitation without touching any routing logic.
