# Calm by Design — Assessment Engine

AI-native adaptive routing service for the **Calm by Design** nervous system regulation program (White Raven Holistic). Built for the Build with Gemini XPRIZE, May–Aug 2026.

**What it does:** lets a participant enroll once, then check in daily (symptom ratings + journal) through a branded web page. A deterministic, non-clinical support score plus a Gemini structured classification (sentiment, distress/progress signals, safety signal) decide one of five response routes — Positive/Progress, Steady, Grounding Support, Heightened Support, or the Safety Route — and write the check-in and routing audit trail to Airtable when persistence is available. Direct self-harm or imminent-danger language is checked by a narrow deterministic backstop before external processing, and Gemini also classifies submissions on the normal dependency-available path. A triggering result shows immediate on-screen 988/911 guidance and makes a best-effort Crisis Alert record when persistence is available, independent of the score. Record creation does not prove notification delivery or human review. The original GoHighLevel form + webhook path (`/assess`) continues to use the same underlying assessment logic.

```
Participant browser                              GoHighLevel (existing, unchanged)
  /enroll ──▶ Client record (Airtable)              GHL form ──▶ POST /assess ──┐
  /access#t=... ──▶ same-origin POST ──▶ session cookie                        │
  /checkin ──▶──────────────────────────────────────────────────────────────────┤
                                                                                 ▼
                                                            process_checkin() (shared core)
                                                                 │
                                                                 ├──▶ Daily Log (Airtable)
                                                                 │
                                                                 ├──▶ Gemini structured output
                                                                 │      + deterministic safety rule
                                                                 │      (runs on every submission)
                                                                 │
                                                                 ├──▶ support score + tier
                                                                 │      (routing_config.py, central config)
                                                                 │
                                                                 ├──▶ response route (5-way) ──▶ AI Assessment
                                                                 │                                 (Airtable)
                                                                 ├──▶ curriculum state machine
                                                                 │      ──▶ Client update + State Transition
                                                                 │
                                                                 └──▶ Safety Route ──▶ Crisis Alert (Airtable)
                                                                        ──▶ existing GHL Crisis Alert workflow
```

GHL's Crisis Alert Notification and Safety Buffer Routing workflows continue to poll Airtable directly for new Crisis Alert / AI Assessment records exactly as before — that delivery mechanism is unchanged by this revision. `GHL_ROUTING_WEBHOOK`/`GHL_CRISIS_WEBHOOK` remain optional outbound calls the app makes if those env vars are set; they are not the primary delivery path.

## Status

- [x] Airtable base ("CBD Core") built and populated, extended with participant/consent, scoring, and routing audit fields
- [x] Gemini assessment engine (`main.py`) rewritten around a deterministic support score + 5-route model, with a shared core used by both the new web pages and the existing GHL webhook
- [x] Participant-facing enrollment, check-in, recovery, and result/safety pages (`templates/`)
- [ ] Deploy and verify this version on Google Cloud Run (`cbd-assess`, `us-east1`)
- [ ] Complete controlled end-to-end verification with test-only records before participant enrollment
- [x] Credentials rotated after being briefly exposed in a shared doc during setup
- [x] GitHub repo created and pushed *(this repo)*
- [x] Existing GHL "Daily Check-In - Assessment" webhook workflow retained against `/assess`; controlled testing must verify its payload against the new length, rate, and optional submission-ID validation
- [ ] New GHL "Send Check-in Link" workflow (Recovery Requests polling) — manual, see `MANUAL_TEST_CHECKLIST.md`
- [x] Golden test set run and results archived (`golden_results.json`)
- [ ] Devpost submission finalized

## Local setup (for reference — do not commit real credentials)

Credentials are supplied to Cloud Run via `--set-env-vars` / `--update-env-vars` at deploy time, or via a local `env.yaml` that is excluded from version control (see `.gitignore`). Required variables:

- `GEMINI_API_KEY` — from [aistudio.google.com](https://aistudio.google.com)
- `AIRTABLE_API_KEY` — Airtable personal access token, scoped to the CBD Core base only (`data.records:read`, `data.records:write`)
- `WEBHOOK_SECRET` — a long random string; GHL must send this in the `X-Webhook-Secret` header on every request
- `SESSION_SECRET` — a long random string used to generate CSRF tokens for the participant-facing pages; never in source
- `GEMINI_MODEL` — the Gemini model string in use

## Deploy

```
gcloud run deploy cbd-assess \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --set-env-vars "GEMINI_API_KEY=...,AIRTABLE_API_KEY=...,WEBHOOK_SECRET=...,SESSION_SECRET=..."
```

The participant enrollment, recovery, token-redemption, and check-in routes are
intentionally public at the Cloud Run layer and enforce their own cookie, CSRF,
token, and rate-limit controls. Only the machine-facing `/assess` route requires
the `X-Webhook-Secret`. Controlled deployment testing must verify Cloud Run
request-log behavior, proxy addressing, cookies, and all required environment
variables before participant enrollment.

## Test

```
pip install -r requirements.txt
pytest                      # unit/endpoint tests, Airtable and Gemini mocked
export GEMINI_API_KEY=your_key
python run_golden.py        # scenario eval against the live Gemini API
```

`golden_set.json` contains test journals covering every response route, score boundary values, positive/neutral/distressed entries, and negated/historical/third-person/direct/imminent safety language. Results are evidence for judges. See `MANUAL_TEST_CHECKLIST.md` for the manual pass covering mobile layout and real GHL/Airtable round-trips against test records.

## Where the evidence lives (for the submission)

- **AI executes key decisions:** AI Assessments + State Transitions tables (Airtable) + Cloud Run logs
- **Telemetry:** Cloud Console → Cloud Run → cbd-assess → Logs (completed ordinary Gemini assessment paths log latency, tokens, tier, route, and model; emergency short-circuits and incomplete persistence paths do not necessarily emit that telemetry, and logs must never contain journal text, email, phone, or tokens)
- **Costs:** Cloud Console Billing + Airtable/GHL invoices → cost ledger

## Safety design

- The support score is a deterministic, non-clinical routing signal (4-40) — never a diagnosis, medical assessment, suicide-risk score, or validated instrument.
- Safety detection runs on every submission: Gemini's structured `safety_signal` plus a narrow deterministic keyword rule (`safety_rules.py`), combined so the strongest signal wins. The keyword rule is the only safety signal available if Gemini is unavailable or returns an invalid response (`Fallback Mode` is recorded).
- The Safety Route is independent of the score — a high score alone never classifies someone as suicidal, and it always shows 988/911 resources first, states plainly that the app is not emergency care and is not monitored in real time, and offers a grounding element only as an additional option afterward, never a replacement.
- This software supports a wellness curriculum; it does not diagnose or treat any condition. See `MANUAL_TEST_CHECKLIST.md` for what remains unsuitable for real clinical/crisis use.
